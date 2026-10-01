"""
wallet_recharge.py
==================

Real money-in flow for wallet top-ups, via Razorpay. Mirrors
razorpay_payments.py's booking-payment flow, but credits the user's
wallet_balance instead of marking a booking paid.

Flow:
    1. Customer picks (or types) an amount on /wallet and hits
       Recharge. The browser navigates to
       /wallet/recharge/checkout?amount=... .
    2. That route creates a Razorpay Order server-side (needs
       RAZORPAY_KEY_ID + RAZORPAY_KEY_SECRET) and logs a "created"
       row in wallet_topups_collection, then renders Razorpay's
       Checkout.js.
    3. Razorpay hands the customer off to whichever UPI app / card /
       netbanking flow they choose. On success, its JS handler POSTs
       the payment id, order id, and a signature to
       /wallet/recharge/verify.
    4. That route recomputes the HMAC signature server-side with the
       key secret. Only when it matches - proving the payment is
       real and wasn't just made up by the customer's browser - is
       the wallet actually credited.

There is no code path that adds wallet balance without step 4
succeeding first. If RAZORPAY keys aren't configured, recharge is
refused outright rather than falling back to a fake credit.
"""

import os
import hmac
import hashlib
import logging
from datetime import datetime

from flask import request, redirect, url_for, session, flash, render_template, jsonify
from bson.objectid import ObjectId

from razorpay_payments import razorpay_configured, _create_order
from payment_ledger import log_transaction

logger = logging.getLogger(__name__)


def init_wallet_recharge(app, users_collection, wallet_transactions_collection, wallet_topups_collection,
                          payment_transactions_collection=None):

    # =========================================
    # CHECKOUT PAGE - creates the Razorpay order
    # for a wallet top-up and renders Checkout.js
    # =========================================
    @app.route("/wallet/recharge/checkout")
    def wallet_recharge_checkout():

        if "user_email" not in session:
            return redirect(url_for("login"))

        try:
            amount = float(request.args.get("amount", "0"))
        except ValueError:
            amount = 0

        if amount < 1:
            flash("Please enter a valid recharge amount.", "danger")
            return redirect(url_for("user_wallet"))

        if amount > 50000:
            flash("Recharge amount can't exceed ₹50,000 in a single top-up.", "danger")
            return redirect(url_for("user_wallet"))

        if not razorpay_configured():
            flash("Online recharge isn't available right now - the payment gateway isn't configured yet.", "danger")
            return redirect(url_for("user_wallet"))

        topup = wallet_topups_collection.insert_one({
            "user_email": session["user_email"],
            "amount": amount,
            "status": "created",
            "created_at": datetime.now()
        })

        order = _create_order(amount, receipt=f"aquaflow_wallet_{topup.inserted_id}")

        if not order:
            wallet_topups_collection.update_one(
                {"_id": topup.inserted_id},
                {"$set": {"status": "order_failed"}}
            )
            flash("Couldn't start online payment right now - please try again in a moment.", "danger")
            return redirect(url_for("user_wallet"))

        wallet_topups_collection.update_one(
            {"_id": topup.inserted_id},
            {"$set": {"razorpay_order_id": order["id"]}}
        )

        return render_template(
            "wallet_recharge_checkout.html",
            topup_id=str(topup.inserted_id),
            amount=amount,
            order_id=order["id"],
            razorpay_key_id=os.getenv("RAZORPAY_KEY_ID"),
            customer_email=session["user_email"]
        )

    # =========================================
    # VERIFY PAYMENT - Razorpay's Checkout.js calls
    # this on success with payment id, order id, signature.
    # Wallet is credited here, and only here.
    # =========================================
    @app.route("/wallet/recharge/verify", methods=["POST"])
    def wallet_recharge_verify():

        if "user_email" not in session:
            return jsonify({"success": False, "message": "Not logged in"}), 401

        data = request.get_json(silent=True) or {}
        topup_id = data.get("topup_id")
        razorpay_order_id = data.get("razorpay_order_id")
        razorpay_payment_id = data.get("razorpay_payment_id")
        razorpay_signature = data.get("razorpay_signature")

        if not all([topup_id, razorpay_order_id, razorpay_payment_id, razorpay_signature]):
            return jsonify({"success": False, "message": "Missing payment details"}), 400

        try:
            topup = wallet_topups_collection.find_one({"_id": ObjectId(topup_id)})
        except Exception:
            topup = None

        if not topup or topup.get("user_email") != session["user_email"]:
            return jsonify({"success": False, "message": "Recharge request not found"}), 404

        if topup.get("status") == "paid":
            # Already credited (e.g. a retried/duplicate callback) -
            # never credit the wallet twice for the same top-up.
            return jsonify({"success": True, "already_credited": True, "amount": topup.get("amount", 0)})

        if topup.get("razorpay_order_id") != razorpay_order_id:
            return jsonify({"success": False, "message": "Order mismatch"}), 400

        key_secret = os.getenv("RAZORPAY_KEY_SECRET", "")

        # Recompute the HMAC the same way Razorpay did. A forged or
        # replayed request (e.g. someone calling this endpoint by hand
        # with a fake payment id) fails this check and credits nothing.
        expected_signature = hmac.new(
            key_secret.encode(),
            f"{razorpay_order_id}|{razorpay_payment_id}".encode(),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(expected_signature, razorpay_signature):
            wallet_topups_collection.update_one(
                {"_id": topup["_id"]},
                {"$set": {"status": "verification_failed"}}
            )
            log_transaction(
                payment_transactions_collection,
                type="wallet_topup",
                status="Verification Failed",
                amount=topup.get("amount", 0),
                user_email=session["user_email"],
                gateway="razorpay",
                razorpay_order_id=razorpay_order_id,
                razorpay_payment_id=razorpay_payment_id,
                razorpay_signature=razorpay_signature,
            )
            return jsonify({"success": False, "message": "Payment verification failed"}), 400

        amount = topup.get("amount", 0)
        payment_time = datetime.now()

        users_collection.update_one(
            {"email": session["user_email"]},
            {"$inc": {"wallet_balance": amount}}
        )

        wallet_transactions_collection.insert_one({
            "user_email": session["user_email"],
            "type": "recharge",
            "amount": amount,
            "razorpay_payment_id": razorpay_payment_id,
            "created_at": payment_time
        })

        wallet_topups_collection.update_one(
            {"_id": topup["_id"]},
            {"$set": {
                "status": "paid",
                "razorpay_payment_id": razorpay_payment_id,
                "razorpay_signature": razorpay_signature,
                "paid_at": payment_time
            }}
        )

        log_transaction(
            payment_transactions_collection,
            type="wallet_topup",
            status="Paid",
            amount=amount,
            user_email=session["user_email"],
            gateway="razorpay",
            razorpay_order_id=razorpay_order_id,
            razorpay_payment_id=razorpay_payment_id,
            razorpay_signature=razorpay_signature,
            receipt=f"aquaflow_wallet_{topup_id}",
        )

        return jsonify({"success": True, "amount": amount})

    # =========================================
    # CANCEL - customer closed the Razorpay modal
    # without paying. Just marks the attempt so it
    # shows up as abandoned, not silently missing.
    # =========================================
    @app.route("/wallet/recharge/cancel/<topup_id>", methods=["POST"])
    def wallet_recharge_cancel(topup_id):

        if "user_email" not in session:
            return jsonify({"success": False}), 401

        try:
            wallet_topups_collection.update_one(
                {
                    "_id": ObjectId(topup_id),
                    "user_email": session["user_email"],
                    "status": {"$ne": "paid"}
                },
                {"$set": {"status": "cancelled"}}
            )
        except Exception:
            pass

        return jsonify({"success": True})

    return app
