"""
razorpay_payments.py
=====================

Real online-payment checkout for customers, via Razorpay.

Flow:
    1. Customer picks "Pay Now (UPI)" on the booking form.
    2. Browser hits /checkout/<booking_id>, which asks Razorpay to
       create an Order (server-side, needs RAZORPAY_KEY_ID +
       RAZORPAY_KEY_SECRET) and renders Razorpay's Checkout.js with
       the UPI method pre-selected - this is what actually hands the
       customer off to whichever UPI app they choose (GPay, PhonePe,
       Paytm, etc.) to complete payment.
    3. Razorpay calls our JS success handler with a payment id, order
       id, and a signature. We POST those to /verify-razorpay-payment,
       which recomputes the HMAC signature server-side with the key
       secret - this is the step that actually proves the payment is
       real, not something the customer's browser made up.
    4. On a verified signature, the booking is marked paid, which
       immediately feeds into the driver wallet (no cash to collect
       for this order) and the admin finance dashboard.

WITHOUT REAL RAZORPAY KEYS this whole flow can't run - Razorpay
rejects the order-creation call. `razorpay_configured()` tells the
booking route whether to offer this at all; if not, the booking flow
falls back to the plain UPI-deep-link page that was already built
(pay_upi_redirect.html / /pay-upi/<id>), which needs no gateway
account but also can't auto-verify payment.
"""

import os
import hmac
import hashlib
import logging
import requests
from datetime import datetime

from flask import request, redirect, url_for, session, flash, render_template, jsonify

from payment_ledger import log_transaction

logger = logging.getLogger(__name__)

_RAZORPAY_ORDERS_URL = "https://api.razorpay.com/v1/orders"


def razorpay_configured():
    return bool(os.getenv("RAZORPAY_KEY_ID")) and bool(os.getenv("RAZORPAY_KEY_SECRET"))


def _create_order(amount_rupees, receipt):
    """Calls Razorpay's Orders API. Returns the order dict, or None on
    failure (bad keys, network issue, Razorpay account not activated,
    etc.) - callers must handle None and fall back gracefully."""
    key_id = os.getenv("RAZORPAY_KEY_ID")
    key_secret = os.getenv("RAZORPAY_KEY_SECRET")

    try:
        resp = requests.post(
            _RAZORPAY_ORDERS_URL,
            auth=(key_id, key_secret),
            json={
                "amount": int(round(amount_rupees * 100)),  # paise
                "currency": "INR",
                "receipt": receipt,
                "payment_capture": 1
            },
            timeout=10
        )
        if resp.status_code == 200:
            return resp.json()

        # Order creation failed - log the real reason (bad keys, account
        # not activated, invalid amount, etc.) so it shows up in the
        # server logs instead of failing silently for the customer.
        logger.error(
            "Razorpay order creation failed: status=%s body=%s",
            resp.status_code, resp.text
        )
        return None
    except requests.RequestException as exc:
        logger.error("Razorpay order creation raised an exception: %s", exc)
        return None


def init_razorpay_payments(app, bookings_collection, settings_collection, notifications_collection=None,
                            payment_transactions_collection=None):

    # =========================================
    # CHECKOUT PAGE - creates the Razorpay order
    # and renders Checkout.js
    # =========================================

    @app.route("/checkout/<booking_id>")
    def razorpay_checkout(booking_id):

        if "user_email" not in session:
            return redirect(url_for("login"))

        try:
            from bson.objectid import ObjectId
            booking_doc = bookings_collection.find_one({"_id": ObjectId(booking_id)})
        except Exception:
            booking_doc = None

        if not booking_doc or booking_doc.get("user_email") != session["user_email"]:
            flash("Booking not found.", "danger")
            return redirect(url_for("history"))

        if booking_doc.get("status") == "Cancelled":
            flash("This booking request was cancelled and can no longer be paid for. Please create a new booking.", "danger")
            return redirect(url_for("history"))

        if not razorpay_configured():
            # No gateway account set up - fall back to the plain UPI
            # deep-link flow instead of showing a broken checkout.
            return redirect(url_for("pay_upi", booking_id=booking_id))

        if booking_doc.get("delivery_fee", 0) <= 0:
            flash("This booking is fully covered by wallet/coupon - use the Confirm Booking button instead.", "info")
            return redirect(url_for("payment_page", booking_id=booking_id))

        amount = booking_doc.get("delivery_fee", 500)

        order = _create_order(amount, receipt=f"aquaflow_{booking_id}")

        if not order:
            flash("Couldn't start online payment right now - please try Cash on Delivery, or try again in a moment.", "danger")
            return redirect(url_for("payment_page", booking_id=booking_id))

        bookings_collection.update_one(
            {"_id": ObjectId(booking_id)},
            {"$set": {"razorpay_order_id": order["id"]}}
        )

        # Which payment method the customer picked on the payment page
        # (Feature 3) - used only to preselect the right block in
        # Razorpay's checkout UI. Falls back to UPI if missing/unknown.
        preferred_method = request.args.get("method", "upi").strip().lower()
        if preferred_method not in ("upi", "card", "netbanking"):
            preferred_method = "upi"

        return render_template(
            "razorpay_checkout.html",
            booking_id=booking_id,
            amount=amount,
            order_id=order["id"],
            razorpay_key_id=os.getenv("RAZORPAY_KEY_ID"),
            customer_name=booking_doc.get("fullname", ""),
            customer_email=booking_doc.get("user_email", ""),
            preferred_method=preferred_method
        )

    # =========================================
    # VERIFY PAYMENT - Razorpay's Checkout.js
    # calls this on success with the payment id,
    # order id, and signature
    # =========================================

    @app.route("/verify-razorpay-payment", methods=["POST"])
    def verify_razorpay_payment():

        if "user_email" not in session:
            return jsonify({"success": False, "message": "Not logged in"}), 401

        data = request.get_json(silent=True) or {}
        booking_id = data.get("booking_id")
        razorpay_order_id = data.get("razorpay_order_id")
        razorpay_payment_id = data.get("razorpay_payment_id")
        razorpay_signature = data.get("razorpay_signature")

        if not all([booking_id, razorpay_order_id, razorpay_payment_id, razorpay_signature]):
            return jsonify({"success": False, "message": "Missing payment details"}), 400

        key_secret = os.getenv("RAZORPAY_KEY_SECRET", "")

        # This is the step that actually proves the payment is real:
        # recompute the HMAC the same way Razorpay did, using our
        # secret key, and check it matches what came back from the
        # browser. A forged/replayed request would fail this check.
        expected_signature = hmac.new(
            key_secret.encode(),
            f"{razorpay_order_id}|{razorpay_payment_id}".encode(),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(expected_signature, razorpay_signature):
            log_transaction(
                payment_transactions_collection,
                type="booking_payment",
                status="Verification Failed",
                amount=None,
                booking_id=booking_id,
                user_email=session.get("user_email"),
                gateway="razorpay",
                razorpay_order_id=razorpay_order_id,
                razorpay_payment_id=razorpay_payment_id,
                razorpay_signature=razorpay_signature,
            )
            return jsonify({"success": False, "message": "Payment verification failed"}), 400

        try:
            from bson.objectid import ObjectId
            booking_doc = bookings_collection.find_one({"_id": ObjectId(booking_id)})
        except Exception:
            booking_doc = None

        if not booking_doc or booking_doc.get("user_email") != session["user_email"]:
            return jsonify({"success": False, "message": "Booking not found"}), 404

        # -----------------------------------------------------------
        # SECURITY: confirm this order was actually created FOR this
        # booking. Without this check, a valid signature obtained for
        # one booking (even a cheap one the customer legitimately
        # paid for) could be replayed against ANY other booking_id the
        # same user owns - the HMAC would still verify (it's a real,
        # correctly-signed order/payment pair), but it was never
        # issued for this booking. This is the check that closes that
        # hole: the order id must match what /checkout stored on this
        # exact booking when the order was created.
        # -----------------------------------------------------------
        if booking_doc.get("razorpay_order_id") != razorpay_order_id:
            logger.warning(
                "Razorpay order/booking mismatch: booking %s expected order %s, got %s (user %s)",
                booking_id, booking_doc.get("razorpay_order_id"), razorpay_order_id, session.get("user_email")
            )
            log_transaction(
                payment_transactions_collection,
                type="booking_payment",
                status="Verification Failed",
                amount=None,
                booking_id=booking_id,
                user_email=session.get("user_email"),
                gateway="razorpay",
                razorpay_order_id=razorpay_order_id,
                razorpay_payment_id=razorpay_payment_id,
                notes="Order/booking mismatch - possible replay attempt",
            )
            return jsonify({"success": False, "message": "Payment does not match this booking"}), 400

        # IDEMPOTENCY: a double-click, a browser retry, or Razorpay
        # re-firing the same success callback would otherwise re-run
        # everything below - re-crediting nothing (booking money isn't
        # double-charged) but duplicating the ledger entry and
        # resending the "Payment Successful" notification every time.
        # If this exact booking is already marked paid, just confirm
        # success without doing any of that again.
        if booking_doc.get("payment_status") == "Paid (Online)":
            return jsonify({"success": True, "message": "Already verified"})

        # Extra guard against a race between two near-simultaneous
        # requests for the same payment slipping past the check above:
        # if this exact Razorpay payment ID is already logged as Paid
        # anywhere in the ledger, don't log it again.
        if payment_transactions_collection is not None and razorpay_payment_id:
            already_logged = payment_transactions_collection.find_one({
                "razorpay_payment_id": razorpay_payment_id,
                "status": "Paid"
            })
            if already_logged:
                return jsonify({"success": True, "message": "Already verified"})

        payment_time = datetime.now()

        bookings_collection.update_one(
            {"_id": ObjectId(booking_id)},
            {"$set": {
                "payment_status": "Paid (Online)",
                "payment_processed": True,
                "payment_method": "UPI",
                "razorpay_payment_id": razorpay_payment_id,
                "razorpay_signature": razorpay_signature,
                "payment_id": razorpay_payment_id,
                "payment_time": payment_time,
                "paid_at": payment_time,
                "status": "Pending"
            }}
        )

        if notifications_collection is not None:
            notifications_collection.insert_one({
                "user_email": session["user_email"],
                "title": "Payment Successful",
                "message": f"Your payment of ₹{booking_doc.get('delivery_fee', 0):,.0f} was successful. Your invoice is ready to view.",
                "type": "payment_successful",
                "status": "success",
                "icon": "✅",
                "read": False,
                "created_at": payment_time
            })

        log_transaction(
            payment_transactions_collection,
            type="booking_payment",
            status="Paid",
            amount=booking_doc.get("delivery_fee", 0),
            booking_id=booking_id,
            user_email=session["user_email"],
            driver_email=booking_doc.get("driver_email"),
            gateway="razorpay",
            gst=booking_doc.get("gst"),
            razorpay_order_id=razorpay_order_id,
            razorpay_payment_id=razorpay_payment_id,
            razorpay_signature=razorpay_signature,
            receipt=f"aquaflow_{booking_id}",
        )

        return jsonify({"success": True})

    return app
