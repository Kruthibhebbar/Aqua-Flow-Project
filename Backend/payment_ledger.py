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

    # One ledger row per idempotency_key (e.g. one row per COD submission
    # attempt). Partial so rows without a key are unaffected. This is what
    # makes a repeated submit/approve unable to create a second row even
    # if two requests race past the application-level checks.
    collection.create_index(
        "idempotency_key",
        unique=True,
        partialFilterExpression={"idempotency_key": {"$exists": True}}
    )

    return collection


def log_transaction(payment_transactions_collection, type, status, amount,
                     booking_id=None, user_email=None, driver_email=None,
                     gateway=None, gst=None, platform_fee=None,
                     razorpay_order_id=None, razorpay_payment_id=None,
                     razorpay_signature=None, receipt=None,
                     payment_screenshot=None,
                     admin_remarks=None, notes=None, idempotency_key=None):
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

    # Idempotency: the same logical event (same key) is only ever recorded
    # once; a repeat returns the existing row's id instead of adding another.
    if idempotency_key:
        existing = payment_transactions_collection.find_one({"idempotency_key": idempotency_key})
        if existing:
            return existing["transaction_id"]

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
    if idempotency_key:
        doc["idempotency_key"] = idempotency_key

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
        if idempotency_key:
            raced = payment_transactions_collection.find_one({"idempotency_key": idempotency_key})
            if raced:
                return raced["transaction_id"]
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


# =====================================================================
# CASH-ON-DELIVERY DEPOSITS (type "cod_collection")
# =====================================================================
# ONE ledger row represents ONE driver cash-deposit submission for its whole
# life: it is created when the driver submits ("Waiting Admin Approval") and
# is then UPDATED IN PLACE when the admin decides ("Settled" / "Rejected").
# Its `status` is the single authoritative status. Never insert a second row
# for the admin's decision - that is what used to show the same deposit twice
# (one stale "Waiting Admin Approval" next to one "Verified").

COD_PENDING = "Waiting Admin Approval"
COD_VERIFIED = "Settled"
COD_REJECTED = "Rejected"
COD_SUPERSEDED = "Superseded"   # stale duplicate kept for audit, excluded from activity/totals

# What the admin screens call each stored status.
_COD_LABELS = {COD_PENDING: "Pending", COD_VERIFIED: "Verified", COD_REJECTED: "Rejected",
               COD_SUPERSEDED: "Superseded"}


def cod_status_label(status):
    return _COD_LABELS.get(status, status)


def cod_idempotency_key(booking_id, attempt):
    """A booking can be submitted again only after a rejection, so
    booking + attempt number identifies exactly one submission."""
    return f"cod:{booking_id}:{int(attempt or 1)}"


def record_cod_submission(coll, booking_id, attempt, **fields):
    """Create (once) the ledger row for a driver's cash-deposit submission."""
    return log_transaction(
        coll, type="cod_collection", status=COD_PENDING, booking_id=booking_id,
        gateway="cash", idempotency_key=cod_idempotency_key(booking_id, attempt), **fields
    )


def resolve_cod_collection(coll, booking_id, attempt, new_status, **fields):
    """Move the submission's EXISTING ledger row to Settled/Rejected.

    Returns (transaction_id, changed). `changed` is False when the row was
    already in `new_status` (a repeated approval) - nothing is modified then.
    Falls back to a legacy row (created before idempotency keys existed), and
    only if there is no row at all creates exactly one, under the same key.
    """
    if coll is None:
        return None, False
    key = cod_idempotency_key(booking_id, attempt)
    row = coll.find_one({"idempotency_key": key})
    if row is None:   # legacy pending row written before keys existed
        row = coll.find_one(
            {"type": "cod_collection", "booking_id": str(booking_id), "status": COD_PENDING,
             "idempotency_key": {"$exists": False}},
            sort=[("created_at", -1)])
    if row is None:
        txn = log_transaction(coll, type="cod_collection", status=new_status, booking_id=booking_id,
                              gateway="cash", idempotency_key=key, **fields)
        return txn, True

    if row.get("status") == new_status:
        return row["transaction_id"], False

    # Only a still-pending row may be decided; atomic so two admins can't both win.
    update = {"status": new_status, "updated_at": datetime.now(), "idempotency_key": key}
    for name in ("admin_remarks", "payment_screenshot"):
        if fields.get(name) is not None:
            update[name] = fields[name]
    result = coll.update_one(
        {"transaction_id": row["transaction_id"], "status": COD_PENDING}, {"$set": update})
    return row["transaction_id"], result.matched_count == 1


def find_duplicate_cod_rows(coll):
    """Pairs of cod_collection rows created by the old double-insert bug.

    Within ONE booking, each decided row (Settled/Rejected) is paired with the
    nearest earlier still-"Waiting Admin Approval" row from the SAME driver for
    the SAME amount, because one submission produced both. Same amount on a
    different booking, or a different driver/amount, is never paired, and a
    pending row with no later decision is a real pending deposit and is left
    alone. Returns [{"stale": row, "kept": row}].
    """
    rows = list(coll.find({"type": "cod_collection", "booking_id": {"$ne": None}}).sort("created_at", 1))
    by_booking = {}
    for r in rows:
        by_booking.setdefault(r["booking_id"], []).append(r)

    pairs = []
    for booking_rows in by_booking.values():
        waiting = [r for r in booking_rows if r.get("status") == COD_PENDING and "idempotency_key" not in r]
        for decided in (r for r in booking_rows if r.get("status") in (COD_VERIFIED, COD_REJECTED)
                        and "idempotency_key" not in r):
            candidates = [w for w in waiting
                          if w["created_at"] <= decided["created_at"]
                          and w.get("driver_email") == decided.get("driver_email")
                          and w.get("amount") == decided.get("amount")]
            if candidates:
                stale = candidates[-1]
                waiting.remove(stale)
                pairs.append({"stale": stale, "kept": decided})
    return pairs


def supersede_duplicate_cod_rows(coll, pairs):
    """Retire stale duplicates WITHOUT deleting anything: the stale row is
    marked "Superseded" (pointing at the row that was kept) and the submission
    reference it carried is copied onto the kept row. Safe to re-run."""
    done = 0
    for pair in pairs:
        stale, kept = pair["stale"], pair["kept"]
        res = coll.update_one(
            {"transaction_id": stale["transaction_id"], "status": COD_PENDING},
            {"$set": {"status": COD_SUPERSEDED, "superseded_by": kept["transaction_id"],
                      "superseded_at": datetime.now()}})
        if res.matched_count:
            carry = {"submission_transaction_id": stale["transaction_id"]}
            if stale.get("notes") and not kept.get("notes"):
                carry["notes"] = stale["notes"]
            if stale.get("payment_screenshot") and not kept.get("payment_screenshot"):
                carry["payment_screenshot"] = stale["payment_screenshot"]
            coll.update_one({"transaction_id": kept["transaction_id"]}, {"$set": carry})
            done += 1
    return done
