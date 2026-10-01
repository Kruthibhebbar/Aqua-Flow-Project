"""
payment_admin.py
=================

Real driver -> company cash reconciliation.

The business reality this models:

    Customer pays the DRIVER in cash (or UPI to the driver's own
    number) at the doorstep. That money is not the driver's - it
    belongs to the company - so the driver has to physically send it
    on to the company's own bank/UPI account and prove they did it.
    Admin then checks the transaction reference / screenshot against
    their real bank statement and approves it.

This module owns:
    - the company's bank/UPI details (a single settings document that
      the admin can edit from the Driver Management page, and that is
      shown to every driver on their dashboard so they know exactly
      where to send the money)
    - the admin-side verification of each payment a driver submits
      (approve -> money is considered received, driver's pending
      balance drops; reject -> driver is told to resubmit)
    - small helper functions (imported by driver_dashboard.py and
      driver_management.py) that compute how much a given driver still
      owes the company right now.

The actual "driver submits a payment" endpoint lives in
driver_dashboard.py (submit_collected_payment) since it needs the
driver-login decorator and driver session context - this file only
saves the uploaded screenshot for it via `save_payment_screenshot()`.
"""

import os
import re
import uuid
from datetime import datetime, timedelta

from flask import (
    request,
    redirect,
    url_for,
    session,
    flash,
    jsonify
)

from bson.objectid import ObjectId

from payment_ledger import log_transaction, update_transaction_status


# =====================================================
# DEFAULTS
# =====================================================

_DEFAULT_BANK_DETAILS = {
    "bank_name": "Not set up yet",
    "account_holder": "Not set up yet",
    "account_number": "",
    "ifsc": "",
    "upi_id": "",
    "qr_image": "",
    "updated_at": None
}

_SETTINGS_ID = "company_bank_details"

# Driver's share of each delivery fee. The rest is platform commission.
# (₹800 booking -> driver gets ₹720, platform keeps ₹80, per the
# business's own example). Hardcoded for now; move into the settings
# doc + an admin-editable field if it needs to vary by driver/route.
DRIVER_SHARE_RATE = 0.90

# Where uploaded payment-proof screenshots and the bank QR code are
# stored on disk. This lives under /static so it can be served directly
# via url_for('static', filename=...).
_UPLOAD_SUBDIR = os.path.join("uploads", "payment_proofs")


# =====================================================
# SHARED HELPERS (imported by other Backend modules)
# =====================================================

def get_bank_details(settings_collection):
    """Return the company's bank/UPI details, or sensible defaults if
    the admin hasn't configured them yet."""
    if settings_collection is None:
        return dict(_DEFAULT_BANK_DETAILS)

    doc = settings_collection.find_one({"_id": _SETTINGS_ID})
    if not doc:
        return dict(_DEFAULT_BANK_DETAILS)

    merged = dict(_DEFAULT_BANK_DETAILS)
    merged.update({k: v for k, v in doc.items() if k != "_id"})
    return merged


def get_bank_details_masked(settings_collection):
    """Same as get_bank_details(), but with the account number masked
    to just the last 4 digits. Use this wherever bank details are
    shown to drivers - only the admin-facing edit form should ever see
    the full number."""
    details = get_bank_details(settings_collection)
    acct = details.get("account_number") or ""
    if len(acct) > 4:
        details["account_number"] = "X" * (len(acct) - 4) + acct[-4:]
    return details


def get_driver_wallet_summary(bookings_collection, driver_email):
    """
    Compute a driver's real-money position with the company:

        collected_total            - cash collected from customers on
                                      every delivered order, ever
        pending_verification_total - already submitted by the driver,
                                      waiting on admin to check the
                                      bank statement and approve
        approved_total              - admin has confirmed the money
                                      actually landed in the company
                                      account
        pending_deposit             - collected_total minus
                                      approved_total; this is the
                                      amount the driver still has to
                                      send the company right now
                                      (whether or not a submission is
                                      already awaiting verification)
    """
    delivered_orders = list(bookings_collection.find({
        "driver_email": driver_email,
        "status": "Delivered"
    }))

    def order_amount(o):
        return o.get("cash_submitted_amount") or o.get("delivery_fee", 500)

    def paid_online(o):
        return o.get("payment_status") == "Paid (Online)"

    # Orders the customer already paid for online never had cash change
    # hands at the doorstep - the driver has nothing to collect or remit
    # for these, so they're excluded from the cash-deposit math below.
    cash_orders = [o for o in delivered_orders if not paid_online(o)]

    collected_total = sum(order_amount(o) for o in cash_orders)

    pending_verification_total = sum(
        order_amount(o) for o in cash_orders
        if o.get("payment_submitted") and o.get("payment_submission_status") == "Pending Verification"
    )

    approved_total = sum(
        order_amount(o) for o in cash_orders
        if o.get("payment_submission_status") == "Approved"
    )

    pending_deposit = max(0, collected_total - approved_total)

    # =====================================================
    # DRIVER EARNINGS (money the COMPANY owes the DRIVER)
    # This is the opposite direction from the cash-deposit numbers
    # above: those track cash the driver collected and must remit in
    # full; this tracks the driver's commission on each delivery,
    # which the company pays out separately via settlement requests.
    # =====================================================
    def order_fee(o):
        return o.get("delivery_fee", 500)

    driver_earned_total = sum(
        round(order_fee(o) * DRIVER_SHARE_RATE) for o in delivered_orders
    )

    today = datetime.now().date()
    todays_earnings = sum(
        round(order_fee(o) * DRIVER_SHARE_RATE)
        for o in delivered_orders
        if isinstance(o.get("delivered_at"), datetime) and o["delivered_at"].date() == today
    )

    week_start = today - timedelta(days=today.weekday())
    weekly_earnings_total = sum(
        round(order_fee(o) * DRIVER_SHARE_RATE)
        for o in delivered_orders
        if isinstance(o.get("delivered_at"), datetime) and o["delivered_at"].date() >= week_start
    )

    month_start = today.replace(day=1)
    monthly_earnings_total = sum(
        round(order_fee(o) * DRIVER_SHARE_RATE)
        for o in delivered_orders
        if isinstance(o.get("delivered_at"), datetime) and o["delivered_at"].date() >= month_start
    )

    return {
        "collected_total": collected_total,
        "pending_verification_total": pending_verification_total,
        "approved_total": approved_total,
        "pending_deposit": pending_deposit,
        "orders_awaiting_submission": len([
            o for o in cash_orders
            if not o.get("payment_submitted") and not o.get("payment_processed")
        ]),
        "driver_share_rate": DRIVER_SHARE_RATE,
        "driver_earned_total": driver_earned_total,
        "todays_earnings": todays_earnings,
        "weekly_earnings_total": weekly_earnings_total,
        "monthly_earnings_total": monthly_earnings_total
    }


def get_driver_settlement_summary(transactions_collection, bookings_collection, driver_email):
    """How much of a driver's earned commission is still sitting with
    the company, vs already requested/settled."""
    wallet = get_driver_wallet_summary(bookings_collection, driver_email)
    earned = wallet["driver_earned_total"]

    requests = []
    if transactions_collection is not None:
        requests = list(
            transactions_collection.find({
                "driver_email": driver_email,
                "type": "settlement"
            }).sort("requested_at", -1)
        )

    completed_total = sum(r.get("amount", 0) for r in requests if r.get("status") == "Completed")
    in_flight_total = sum(
        r.get("amount", 0) for r in requests
        if r.get("status") in ("Pending", "Processing")
    )
    rejected_total = sum(r.get("amount", 0) for r in requests if r.get("status") == "Rejected")

    # CRITICAL: a driver's commission on a COD delivery is only really
    # "theirs to withdraw" once the cash they collected for that same
    # delivery has actually been submitted AND admin-approved -
    # otherwise the company hasn't received that money yet, and the
    # driver would be cashing out commission on money still sitting in
    # their own pocket. wallet["pending_deposit"] is exactly that
    # not-yet-remitted amount, so it comes straight off what's
    # withdrawable (never below zero).
    withdrawable = max(0, earned - completed_total - in_flight_total - wallet["pending_deposit"])

    return {
        "driver_earned_total": earned,
        "settled_total": completed_total,
        "pending_settlement_total": in_flight_total,
        "rejected_total": rejected_total,
        "withdrawable": withdrawable,
        "unremitted_cash_held": wallet["pending_deposit"],
        "requests": requests,
        "todays_earnings": wallet["todays_earnings"],
        "weekly_earnings_total": wallet["weekly_earnings_total"],
        "monthly_earnings_total": wallet["monthly_earnings_total"]
    }


def get_driver_pending_deposit(bookings_collection, driver_email):
    """Shortcut used by driver_management.py's driver table."""
    return get_driver_wallet_summary(bookings_collection, driver_email)["pending_deposit"]


def save_payment_screenshot(app, file_storage):
    """
    Save an uploaded payment-proof screenshot to
    static/uploads/payment_proofs/ and return the relative static path
    to store on the booking document (or None if no valid file given).
    """
    if not file_storage or not file_storage.filename:
        return None

    allowed_ext = {".png", ".jpg", ".jpeg", ".webp", ".pdf"}
    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in allowed_ext:
        return None

    static_folder = app.static_folder
    target_dir = os.path.join(static_folder, _UPLOAD_SUBDIR)
    os.makedirs(target_dir, exist_ok=True)

    filename = f"{uuid.uuid4().hex}{ext}"
    saved_path = os.path.join(target_dir, filename)
    file_storage.save(saved_path)

    if ext != ".pdf":
        try:
            from image_utils import compress_image_file
            compress_image_file(saved_path)  # PERF: shrink for faster admin page loads (safe no-op on failure)
        except Exception:
            pass

    return f"{_UPLOAD_SUBDIR}/{filename}".replace("\\", "/")


# =====================================================
# MODULE INIT (admin-facing routes)
# =====================================================

def get_ledger_overview(payment_transactions_collection):
    """
    Numbers the finance overview above can't compute, because they
    only exist in the unified ledger (payment_ledger.py), not in
    bookings/drivers: GST actually collected, all-time and today's
    withdrawal totals, and the COD-vs-online payment split.
    """
    empty = {
        "gst_collected": 0,
        "total_withdrawals": 0,
        "today_withdrawals": 0,
        "cod_total": 0,
        "online_total": 0,
        "cod_count": 0,
        "online_count": 0,
    }
    if payment_transactions_collection is None:
        return empty

    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    gst_collected = sum(
        t.get("gst") or 0
        for t in payment_transactions_collection.find(
            {"status": {"$in": ["Paid", "Settled"]}}, {"gst": 1}
        )
    )

    withdrawn = list(payment_transactions_collection.find(
        {"type": "withdrawal", "status": "Withdrawn"}, {"amount": 1, "updated_at": 1}
    ))
    total_withdrawals = sum(t.get("amount") or 0 for t in withdrawn)
    today_withdrawals = sum(
        t.get("amount") or 0 for t in withdrawn
        if isinstance(t.get("updated_at"), datetime) and t["updated_at"] >= today_start
    )

    cod_docs = list(payment_transactions_collection.find(
        {"type": "cod_collection", "status": "Settled"}, {"amount": 1}
    ))
    online_docs = list(payment_transactions_collection.find(
        {"type": "booking_payment", "status": "Paid"}, {"amount": 1}
    ))

    return {
        "gst_collected": gst_collected,
        "total_withdrawals": total_withdrawals,
        "today_withdrawals": today_withdrawals,
        "cod_total": sum(t.get("amount") or 0 for t in cod_docs),
        "online_total": sum(t.get("amount") or 0 for t in online_docs),
        "cod_count": len(cod_docs),
        "online_count": len(online_docs),
    }


def get_top_drivers(bookings_collection, drivers_collection, limit=5):
    """Top drivers by earned commission on delivered orders (same
    DRIVER_SHARE_RATE the driver wallet page uses)."""
    if drivers_collection is None:
        return []

    ranked = []
    for driver in drivers_collection.find({"approval_status": "Approved"}):
        email = driver.get("email")
        if not email:
            continue
        delivered_count = bookings_collection.count_documents({
            "driver_email": email, "status": "Delivered"
        })
        if delivered_count == 0:
            continue
        earned = sum(
            round(o.get("delivery_fee", 500) * DRIVER_SHARE_RATE)
            for o in bookings_collection.find({"driver_email": email, "status": "Delivered"})
        )
        ranked.append({
            "name": driver.get("name", email),
            "email": email,
            "deliveries": delivered_count,
            "earned": earned
        })

    ranked.sort(key=lambda d: d["earned"], reverse=True)
    return ranked[:limit]


def get_top_customers(bookings_collection, limit=5):
    """Top customers by total amount spent on delivered orders."""
    pipeline = [
        {"$match": {"status": "Delivered"}},
        {"$group": {
            "_id": "$user_email",
            "total_spent": {"$sum": {"$ifNull": ["$delivery_fee", 500]}},
            "orders": {"$sum": 1}
        }},
        {"$sort": {"total_spent": -1}},
        {"$limit": limit}
    ]
    return [
        {"email": row["_id"], "total_spent": row["total_spent"], "orders": row["orders"]}
        for row in bookings_collection.aggregate(pipeline) if row["_id"]
    ]


def get_finance_overview(bookings_collection, drivers_collection, transactions_collection=None):
    """Company-wide money position for the admin finance dashboard.

    Revenue is booked when a delivery actually completes (status ==
    Delivered) - a Pending/Approved order hasn't collected any money
    yet. Commission uses the same DRIVER_SHARE_RATE as the driver
    wallet, so "platform commission" here and "driver earnings" on
    the driver wallet page always add up to the same revenue figure.
    """
    delivered = list(bookings_collection.find({"status": "Delivered"}))

    def fee(o):
        return o.get("delivery_fee", 500)

    today = datetime.now().date()

    total_revenue = sum(fee(o) for o in delivered)
    today_revenue = sum(
        fee(o) for o in delivered
        if isinstance(o.get("delivered_at"), datetime) and o["delivered_at"].date() == today
    )

    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    weekly_revenue = sum(
        fee(o) for o in delivered
        if isinstance(o.get("delivered_at"), datetime) and o["delivered_at"].date() >= week_start
    )
    monthly_revenue = sum(
        fee(o) for o in delivered
        if isinstance(o.get("delivered_at"), datetime) and o["delivered_at"].date() >= month_start
    )

    total_commission = round(total_revenue * (1 - DRIVER_SHARE_RATE))
    today_commission = round(today_revenue * (1 - DRIVER_SHARE_RATE))

    # Cash drivers have collected from customers but not yet remitted
    # to the company (the flip side of each driver's "pending_deposit").
    pending_driver_deposits = 0
    # Commission drivers have earned but the company hasn't paid out yet.
    pending_driver_payouts = 0

    if drivers_collection is not None:
        for driver in drivers_collection.find({"approval_status": "Approved"}):
            wallet = get_driver_wallet_summary(bookings_collection, driver.get("email"))
            pending_driver_deposits += wallet["pending_deposit"]
            settlement = get_driver_settlement_summary(
                transactions_collection, bookings_collection, driver.get("email")
            )
            pending_driver_payouts += settlement["withdrawable"]

    # Admin Wallet (Feature 18): Revenue -> Commission -> Driver
    # Settlement -> Profit. There is no automated refund system in
    # this app (business decision - refunds, if ever needed, are
    # handled by support contacting the customer directly, not
    # tracked here), so profit is simply the platform's commission.
    profit = round(total_commission)

    return {
        "total_revenue": total_revenue,
        "today_revenue": today_revenue,
        "weekly_revenue": weekly_revenue,
        "monthly_revenue": monthly_revenue,
        "total_commission": total_commission,
        "today_commission": today_commission,
        "pending_driver_deposits": pending_driver_deposits,
        "pending_driver_payouts": pending_driver_payouts,
        "profit": profit
    }


def init_payment_admin(
    app,
    bookings_collection,
    drivers_collection,
    notifications_collection,
    settings_collection,
    transactions_collection=None,
    payment_transactions_collection=None
):

    def _admin_only():
        return "admin" in session or "admin_email" in session

    # =====================================================
    # UPDATE COMPANY BANK / UPI DETAILS
    # =====================================================

    @app.route("/admin/bank-details", methods=["POST"])
    def update_bank_details():
        if not _admin_only():
            return redirect(url_for("admin_login"))

        bank_name = request.form.get("bank_name", "").strip()
        account_holder = request.form.get("account_holder", "").strip()
        account_number = request.form.get("account_number", "").strip()
        ifsc = request.form.get("ifsc", "").strip().upper()
        upi_id = request.form.get("upi_id", "").strip()

        # =====================================================
        # SERVER-SIDE VALIDATION
        # =====================================================
        errors = []

        if ifsc and not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", ifsc):
            errors.append("IFSC code looks invalid (expected format e.g. CNRB0001234)")

        if account_number and not re.fullmatch(r"\d{9,18}", account_number):
            errors.append("Account number should be 9-18 digits")

        if upi_id and not re.fullmatch(r"[\w.\-]{2,256}@[a-zA-Z]{2,64}", upi_id):
            errors.append("UPI ID looks invalid (expected format e.g. name@bank)")

        if errors:
            flash(" | ".join(errors), "danger")
            return redirect(url_for("finance_management"))

        update_fields = {
            "bank_name": bank_name,
            "account_holder": account_holder,
            "account_number": account_number,
            "ifsc": ifsc,
            "upi_id": upi_id,
            "updated_at": datetime.now(),
            "updated_by": session.get("admin_email", "admin")
        }

        qr_file = request.files.get("qr_image")
        if qr_file and qr_file.filename:
            saved_path = save_payment_screenshot(app, qr_file)
            if saved_path:
                update_fields["qr_image"] = saved_path

        settings_collection.update_one(
            {"_id": _SETTINGS_ID},
            {"$set": update_fields},
            upsert=True
        )

        flash("Company bank / UPI details updated. Drivers will see this immediately.", "success")
        return redirect(url_for("finance_management"))

    # =====================================================
    # VERIFY / REJECT A DRIVER'S SUBMITTED PAYMENT
    # =====================================================

    @app.route("/admin/verify-payment/<booking_id>", methods=["POST"])
    def verify_driver_payment(booking_id):
        if not _admin_only():
            return redirect(url_for("admin_login"))

        action = request.form.get("action", "approve")
        remarks = request.form.get("remarks", "").strip()

        try:
            booking = bookings_collection.find_one({"_id": ObjectId(booking_id)})
        except Exception:
            flash("Invalid booking reference", "danger")
            return redirect(url_for("finance_management"))

        if not booking:
            flash("Booking not found", "danger")
            return redirect(url_for("finance_management"))

        if not booking.get("payment_submitted"):
            flash("No payment submission found for this booking", "warning")
            return redirect(url_for("finance_management"))

        driver_email = booking.get("driver_email")
        amount = booking.get("cash_submitted_amount") or booking.get("delivery_fee", 500)

        if action == "approve":
            bookings_collection.update_one(
                {"_id": ObjectId(booking_id)},
                {
                    "$set": {
                        "payment_submission_status": "Approved",
                        "payment_processed": True,
                        "payment_status": "Paid",
                        "payment_verified_at": datetime.now(),
                        "payment_verified_by": session.get("admin_email", "admin"),
                        "payment_verification_remarks": remarks
                    }
                }
            )

            if driver_email:
                drivers_collection.update_one(
                    {"email": driver_email},
                    {
                        "$inc": {"total_remitted_to_company": amount},
                        "$set": {"last_remittance_at": datetime.now()}
                    }
                )

            if notifications_collection is not None and driver_email:
                notifications_collection.insert_one({
                    "driver_email": driver_email,
                    "title": "Payment Verified ✅",
                    "message": f"Your submission of ₹{amount:,.0f} for booking #{booking_id[:8]} has been verified and received. Thank you!",
                    "type": "payment_verified",
                    "status": "success",
                    "icon": "✅",
                    "read": False,
                    "created_at": datetime.now()
                })

            log_transaction(
                payment_transactions_collection,
                type="cod_collection",
                status="Settled",
                amount=amount,
                booking_id=booking_id,
                user_email=booking.get("user_email"),
                driver_email=driver_email,
                gateway="cash",
                payment_screenshot=booking.get("payment_screenshot"),
                admin_remarks=remarks or session.get("admin_email", "admin"),
            )

            flash(f"Payment of ₹{amount:,.0f} approved and marked as received.", "success")

        else:
            bookings_collection.update_one(
                {"_id": ObjectId(booking_id)},
                {
                    "$set": {
                        "payment_submission_status": "Rejected",
                        "payment_submitted": False,
                        "payment_verification_remarks": remarks,
                        "payment_verified_at": datetime.now(),
                        "payment_verified_by": session.get("admin_email", "admin")
                    }
                }
            )

            if notifications_collection is not None and driver_email:
                notifications_collection.insert_one({
                    "driver_email": driver_email,
                    "title": "Payment Submission Rejected ❌",
                    "message": f"Your submission for booking #{booking_id[:8]} could not be verified"
                               + (f": {remarks}. " if remarks else ". ")
                               + "Please check the transaction reference and resubmit.",
                    "type": "payment_rejected",
                    "status": "danger",
                    "icon": "❌",
                    "read": False,
                    "created_at": datetime.now()
                })

            log_transaction(
                payment_transactions_collection,
                type="cod_collection",
                status="Rejected",
                amount=amount,
                booking_id=booking_id,
                user_email=booking.get("user_email"),
                driver_email=driver_email,
                gateway="cash",
                payment_screenshot=booking.get("payment_screenshot"),
                admin_remarks=remarks or session.get("admin_email", "admin"),
            )

            flash("Payment submission rejected. Driver has been notified to resubmit.", "info")

        return redirect(url_for("finance_management"))

    # =====================================================
    # DRIVER REQUESTS A SETTLEMENT (WITHDRAWAL) OF EARNED
    # COMMISSION
    # =====================================================

    @app.route("/driver/request-withdrawal", methods=["POST"])
    def request_driver_withdrawal():
        if "driver_email" not in session:
            return redirect(url_for("driver_login"))

        driver_email = session["driver_email"]

        try:
            amount = float(request.form.get("amount", "0"))
        except ValueError:
            amount = 0

        settlement = get_driver_settlement_summary(
            transactions_collection, bookings_collection, driver_email
        )

        driver_doc = drivers_collection.find_one({"email": driver_email}) if drivers_collection is not None else None
        bank_details = (driver_doc or {}).get("bank_details") or {}

        if bank_details.get("approval_status") != "Approved":
            flash(
                "Add your bank account details and wait for admin approval before requesting a withdrawal.",
                "danger"
            )
            return redirect(url_for("driver_earnings"))

        if amount <= 0:
            flash("Enter a valid withdrawal amount.", "danger")
        elif amount > settlement["withdrawable"]:
            flash(f"You can withdraw up to ₹{settlement['withdrawable']:,.0f} right now.", "danger")
        elif transactions_collection is None:
            flash("Withdrawals aren't available right now — please try again later.", "danger")
        else:
            ledger_txn_id = log_transaction(
                payment_transactions_collection,
                type="withdrawal",
                status="Pending",
                amount=amount,
                driver_email=driver_email,
                gateway="bank_transfer",
            )

            transactions_collection.insert_one({
                "type": "settlement",
                "driver_email": driver_email,
                "amount": amount,
                "status": "Pending",
                "requested_at": datetime.now(),
                "payment_transaction_id": ledger_txn_id
            })

            if notifications_collection is not None:
                notifications_collection.insert_one({
                    "driver_email": driver_email,
                    "title": "Withdrawal Requested",
                    "message": f"Your request to withdraw ₹{amount:,.0f} has been sent to admin for approval.",
                    "type": "settlement_requested",
                    "status": "info",
                    "icon": "💸",
                    "read": False,
                    "created_at": datetime.now()
                })

            flash(f"Withdrawal request for ₹{amount:,.0f} submitted. Admin will review it shortly.", "success")

        return redirect(url_for("driver_earnings"))

    # =====================================================
    # ADMIN APPROVES / REJECTS A DRIVER'S SETTLEMENT REQUEST
    # =====================================================

    @app.route("/admin/settlement/<request_id>/<action>", methods=["POST"])
    def resolve_driver_settlement(request_id, action):
        if not _admin_only():
            return redirect(url_for("admin_login"))

        if transactions_collection is None:
            flash("Settlements aren't available right now.", "danger")
            return redirect(url_for("finance_management"))

        try:
            req_doc = transactions_collection.find_one({"_id": ObjectId(request_id)})
        except Exception:
            req_doc = None

        if not req_doc:
            flash("Withdrawal request not found.", "danger")
            return redirect(url_for("finance_management"))

        new_status = {
            "approve": "Processing",
            "complete": "Completed",
            "reject": "Rejected"
        }.get(action)

        if not new_status:
            flash("Unknown action.", "danger")
            return redirect(url_for("finance_management"))

        transactions_collection.update_one(
            {"_id": ObjectId(request_id)},
            {"$set": {
                "status": new_status,
                "resolved_at": datetime.now(),
                "resolved_by": session.get("admin_email", "admin")
            }}
        )

        ledger_status = {
            "Processing": "Withdrawn",
            "Completed": "Withdrawn",
            "Rejected": "Rejected"
        }.get(new_status)
        update_transaction_status(
            payment_transactions_collection,
            req_doc.get("payment_transaction_id"),
            status=ledger_status or new_status,
            admin_remarks=session.get("admin_email", "admin")
        )

        if notifications_collection is not None:
            status_copy = {
                "Processing": ("Withdrawal Approved", f"Your withdrawal of ₹{req_doc['amount']:,.0f} was approved and is being processed.", "success", "✅"),
                "Completed": ("Withdrawal Completed", f"₹{req_doc['amount']:,.0f} has been transferred to your bank account.", "success", "💰"),
                "Rejected": ("Withdrawal Rejected", f"Your withdrawal request for ₹{req_doc['amount']:,.0f} was rejected. Contact admin for details.", "danger", "❌"),
            }[new_status]
            notifications_collection.insert_one({
                "driver_email": req_doc["driver_email"],
                "title": status_copy[0],
                "message": status_copy[1],
                "type": "settlement_update",
                "status": status_copy[2],
                "icon": status_copy[3],
                "read": False,
                "created_at": datetime.now()
            })

        flash(f"Settlement request marked as {new_status}.", "success")
        return redirect(url_for("finance_management"))

    # =====================================================
    # ADMIN APPROVES / REJECTS A DRIVER'S BANK DETAILS
    # =====================================================
    # Withdrawals stay locked (see request_driver_withdrawal above)
    # until this happens - admin should actually check the account
    # holder name matches the driver's identity before approving.

    @app.route("/admin/driver-bank-details/<driver_email>/<action>", methods=["POST"])
    def resolve_driver_bank_details(driver_email, action):
        if not _admin_only():
            return redirect(url_for("admin_login"))

        if action not in ("approve", "reject"):
            flash("Unknown action.", "danger")
            return redirect(url_for("finance_management"))

        new_status = "Approved" if action == "approve" else "Rejected"

        drivers_collection.update_one(
            {"email": driver_email},
            {"$set": {
                "bank_details.approval_status": new_status,
                "bank_details.approved_at": datetime.now(),
                "bank_details.approved_by": session.get("admin_email", "admin")
            }}
        )

        if notifications_collection is not None:
            notifications_collection.insert_one({
                "driver_email": driver_email,
                "title": f"Bank Details {new_status}",
                "message": (
                    "Your bank details were approved - you can now request withdrawals."
                    if new_status == "Approved" else
                    "Your bank details were rejected. Please re-check and resubmit them."
                ),
                "type": "bank_details_update",
                "status": "success" if new_status == "Approved" else "danger",
                "icon": "🏦",
                "read": False,
                "created_at": datetime.now()
            })

        flash(f"Driver bank details {new_status.lower()}.", "success")
        return redirect(url_for("finance_management"))

    # =====================================================
    # DOWNLOAD / VIEW A SETTLEMENT PAYMENT RECEIPT
    # =====================================================
    # Only makes sense once a settlement has actually been paid out
    # (Processing/Completed carry a linked payment_transaction_id).

    @app.route("/admin/settlement/<request_id>/receipt")
    def settlement_receipt(request_id):
        if not _admin_only():
            return redirect(url_for("admin_login"))

        if transactions_collection is None:
            flash("Settlements aren't available right now.", "danger")
            return redirect(url_for("finance_management"))

        from flask import render_template

        try:
            req_doc = transactions_collection.find_one({"_id": ObjectId(request_id)})
        except Exception:
            req_doc = None

        if not req_doc:
            flash("Settlement request not found.", "danger")
            return redirect(url_for("finance_management"))

        driver = None
        if drivers_collection is not None:
            driver = drivers_collection.find_one({"email": req_doc.get("driver_email")})

        ledger_txn = None
        if payment_transactions_collection is not None and req_doc.get("payment_transaction_id"):
            ledger_txn = payment_transactions_collection.find_one(
                {"transaction_id": req_doc["payment_transaction_id"]}
            )

        req_doc["_id"] = str(req_doc["_id"])

        return render_template(
            "settlement_receipt.html",
            req=req_doc,
            driver=driver,
            ledger_txn=ledger_txn
        )

    return app
