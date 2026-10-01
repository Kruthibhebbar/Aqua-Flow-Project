"""
payment_ledger.py
===================

Single unified audit trail for every rupee that moves through AquaFlow.

WHY THIS EXISTS
----------------
Every money-moving flow in this codebase already does real work:
razorpay_payments.py verifies signatures server-side, wallet_recharge.py
credits the wallet only after that same verification, payment_admin.py
handles driver cash-remittance approval and withdrawal settlement.
Each of those is correct on its own.

What was missing was ONE place that can answer, for any payment,
"what happened, when, and what's its current status" without having
to know which of five different collections
(bookings/wallet_transactions/wallet_topups/transactions) to look in
and which field name that flow happens to use.

This module owns exactly that: the `payment_transactions` collection.
It does not replace any existing logic - every call site below still
does its own verification/approval exactly as before. This module is
only ever called AFTER that real work has already happened, to record
what occurred. If this logging call failed for some reason it would
never be allowed to block or reverse the real transaction.

Note: there is no automated customer-refund system in this app (a
deliberate business decision - a delivered/paid tanker booking isn't
realistically reversible, so any resolution is handled by support
contacting the customer directly, not tracked here). "Refunded" is
kept in the status vocabulary below only for the wallet-credit-return
case (a customer's own stored wallet balance being released back to
them, e.g. on an early cancellation) - never for reversing a real
payment.

STATUS VALUES
-------------
Pending, Initiated, Authorized, Captured, Paid, Failed, Cancelled,
Expired, Verification Failed, COD Pending, Collected By Driver,
Waiting Admin Approval, Settled, Withdrawn, Rejected.

TRANSACTION TYPES
-----------------
booking_payment, wallet_topup, cod_collection, withdrawal,
settlement, penalty, adjustment, admin_bonus.
"""

import uuid
from datetime import datetime


def init_payment_ledger(db):
    """Create/return the payment_transactions collection with the
    indexes the admin payment-management page (filters, search) and
    the per-user/per-driver history views will need."""
    collection = db["payment_transactions"]

    # Safe to call on every startup - create_index is a no-op if the
    # index already exists with the same spec.
    collection.create_index("transaction_id", unique=True)
    collection.create_index("booking_id")
    collection.create_index("user_email")
    collection.create_index("driver_email")
    collection.create_index("status")
    collection.create_index("type")
    collection.create_index("gateway")
    collection.create_index("created_at")
    # Defense in depth alongside the idempotency checks in
    # razorpay_payments.py/wallet_recharge.py: even if two verification
    # requests for the same real payment somehow raced past those
    # checks, the database itself refuses a second "Paid" row for the
    # same razorpay_payment_id. Partial (only applies where the field
    # exists) since COD/withdrawal rows never have one.
    collection.create_index(
        "razorpay_payment_id",
        unique=True,
        partialFilterExpression={"razorpay_payment_id": {"$exists": True}, "status": "Paid"}
    )

    return collection


def log_transaction(payment_transactions_collection, type, status, amount,
                     booking_id=None, user_email=None, driver_email=None,
                     gateway=None, gst=None, platform_fee=None,
                     razorpay_order_id=None, razorpay_payment_id=None,
                     razorpay_signature=None, receipt=None,
                     payment_screenshot=None,
                     admin_remarks=None, notes=None):
    """
    Record one payment event. Called AFTER the real work (signature
    verification, admin approval, etc.) already happened elsewhere -
    this function never decides whether a payment is valid, it only
    records the outcome.

    Returns the generated transaction_id (string) so callers that need
    to update this same row later (e.g. a withdrawal moving from
    Pending -> Processing -> Completed) can pass it to
    update_transaction_status() below.
    """
    if payment_transactions_collection is None:
        return None

    now = datetime.now()
    net_amount = None
    if amount is not None:
        net_amount = round(
            amount - (gst or 0) - (platform_fee or 0), 2
        )

    doc = {
        "transaction_id": f"TXN{uuid.uuid4().hex[:12].upper()}",
        "type": type,
        "status": status,
        "amount": amount,
        "gst": gst,
        "platform_fee": platform_fee,
        "net_amount": net_amount,
        "booking_id": str(booking_id) if booking_id else None,
        "user_email": user_email,
        "driver_email": driver_email,
        "gateway": gateway,
        "razorpay_order_id": razorpay_order_id,
        "razorpay_payment_id": razorpay_payment_id,
        "razorpay_signature": razorpay_signature,
        "receipt": receipt,
        "payment_screenshot": payment_screenshot,
        "admin_remarks": admin_remarks,
        "notes": notes,
        "created_at": now,
        "updated_at": now,
    }

    try:
        payment_transactions_collection.insert_one(doc)
    except Exception as exc:
        # Only expected to happen on a genuine race for the same
        # razorpay_payment_id hitting the partial unique index in
        # init_payment_ledger() above - the payment was already
        # recorded by the other request, so this one has nothing
        # further to do.
        if "duplicate key" not in str(exc).lower():
            raise
        existing = payment_transactions_collection.find_one(
            {"razorpay_payment_id": doc.get("razorpay_payment_id"), "status": "Paid"}
        )
        return existing["transaction_id"] if existing else None

    return doc["transaction_id"]


def update_transaction_status(payment_transactions_collection, transaction_id,
                               status, admin_remarks=None, **extra_fields):
    """Update an existing ledger row (e.g. a withdrawal request moving
    from Pending to Processing to Completed, or Rejected)."""
    if payment_transactions_collection is None or not transaction_id:
        return

    fields = {"status": status, "updated_at": datetime.now()}
    if admin_remarks is not None:
        fields["admin_remarks"] = admin_remarks
    fields.update(extra_fields)

    payment_transactions_collection.update_one(
        {"transaction_id": transaction_id},
        {"$set": fields}
    )
