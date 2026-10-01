"""
payment_management.py
=======================

Real backend for the Admin Payment Management page (payment_system.html).

The page used to run entirely on a hardcoded in-browser JavaScript
array with a "Simulate new payment" button and click-to-toggle
Mark Success / Mark Pending / Delete actions - none of it touched the
database. That's fine for a demo, but dangerous to leave in an actual
admin dashboard since those buttons look exactly like real controls.

This module makes the page real:
    - /api/admin/payment-transactions  - filterable, searchable, paginated
      read of the unified payment_transactions ledger (payment_ledger.py),
      plus header stats computed from real data.
    - /api/admin/payment-transactions/export - same filters, as a CSV
      download.

This page is intentionally READ-ONLY. Changing a payment's real status
always goes through the flow that owns that money movement (Razorpay
signature verification, /admin/verify-payment for COD, withdrawal
approval) - each of those already updates the ledger via
payment_ledger.log_transaction()/update_transaction_status(). A generic
"click to mark paid" button on a monitoring page would bypass all of
that (no verification, no notification, no linked booking update), so
that control is removed rather than wired to a fake-safe endpoint.
"""

import csv
import io
from datetime import datetime, timedelta

from flask import request, jsonify, session, Response


def _admin_only():
    return "admin" in session or "admin_email" in session


def _serialize(doc):
    """Make a Mongo document JSON-safe for the API response."""
    out = dict(doc)
    out["_id"] = str(out["_id"])
    for field in ("created_at", "updated_at"):
        if isinstance(out.get(field), datetime):
            out[field] = out[field].strftime("%Y-%m-%d %H:%M")
    return out


def _build_filter(args):
    """Shared between the JSON API and the CSV export so both always
    return exactly the same rows for the same filters."""
    q = {}

    status = (args.get("status") or "all").strip()
    if status != "all":
        q["status"] = status

    txn_type = (args.get("type") or "all").strip()
    if txn_type != "all":
        q["type"] = txn_type

    gateway = (args.get("gateway") or "all").strip()
    if gateway != "all":
        q["gateway"] = gateway

    date_from = (args.get("date_from") or "").strip()
    date_to = (args.get("date_to") or "").strip()
    if date_from or date_to:
        date_q = {}
        if date_from:
            try:
                date_q["$gte"] = datetime.strptime(date_from, "%Y-%m-%d")
            except ValueError:
                pass
        if date_to:
            try:
                date_q["$lte"] = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
            except ValueError:
                pass
        if date_q:
            q["created_at"] = date_q

    search = (args.get("q") or "").strip()
    if search:
        q["$or"] = [
            {"transaction_id": {"$regex": search, "$options": "i"}},
            {"booking_id": {"$regex": search, "$options": "i"}},
            {"user_email": {"$regex": search, "$options": "i"}},
            {"driver_email": {"$regex": search, "$options": "i"}},
            {"razorpay_payment_id": {"$regex": search, "$options": "i"}},
        ]

    return q


def _compute_stats(payment_transactions_collection, base_filter):
    """Header-card stats. Computed over base_filter (search/date/type
    filters the admin has set) but ignoring the status filter itself,
    so the cards always show the full breakdown no matter which status
    tab is currently selected."""
    pipeline = [
        {"$match": base_filter},
        {"$group": {
            "_id": "$status",
            "count": {"$sum": 1},
            "total_amount": {"$sum": {"$ifNull": ["$amount", 0]}}
        }}
    ]
    by_status = {row["_id"]: row for row in payment_transactions_collection.aggregate(pipeline)}

    total_count = sum(row["count"] for row in by_status.values())
    total_revenue = sum(
        row["total_amount"] for status, row in by_status.items()
        if status in ("Paid", "Settled")
    )
    pending_count = sum(
        row["count"] for status, row in by_status.items()
        if status in ("Pending", "Waiting Admin Approval", "Collected By Driver", "COD Pending")
    )
    failed_count = sum(
        row["count"] for status, row in by_status.items()
        if status in ("Failed", "Verification Failed", "Rejected", "Expired")
    )
    success_rate = round((total_count - pending_count - failed_count) / total_count * 100) if total_count else 0

    return {
        "total_count": total_count,
        "total_revenue": total_revenue,
        "pending_count": pending_count,
        "failed_count": failed_count,
        "success_rate": success_rate,
        "by_status": {k: {"count": v["count"], "amount": v["total_amount"]} for k, v in by_status.items()}
    }


def init_payment_management(app, payment_transactions_collection):

    @app.route("/api/admin/payment-transactions")
    def api_payment_transactions():
        if not _admin_only():
            return jsonify({"success": False, "message": "Unauthorized"}), 401

        if payment_transactions_collection is None:
            return jsonify({"success": False, "message": "Payment ledger unavailable"}), 503

        args = request.args
        base_filter = _build_filter(args)

        # Stats ignore the status filter (see _compute_stats docstring)
        # so we need the filter minus 'status' for the header cards.
        stats_filter = dict(base_filter)
        stats_filter.pop("status", None)
        stats = _compute_stats(payment_transactions_collection, stats_filter)

        try:
            page = max(1, int(args.get("page", 1)))
        except ValueError:
            page = 1
        page_size = 20

        total_matching = payment_transactions_collection.count_documents(base_filter)
        total_pages = max(1, -(-total_matching // page_size))  # ceil

        rows = list(
            payment_transactions_collection.find(base_filter)
            .sort("created_at", -1)
            .skip((page - 1) * page_size)
            .limit(page_size)
        )

        return jsonify({
            "success": True,
            "transactions": [_serialize(r) for r in rows],
            "stats": stats,
            "page": page,
            "total_pages": total_pages,
            "total_matching": total_matching
        })

    @app.route("/api/admin/payment-transactions/export")
    def export_payment_transactions():
        if not _admin_only():
            return jsonify({"success": False, "message": "Unauthorized"}), 401

        if payment_transactions_collection is None:
            return jsonify({"success": False, "message": "Payment ledger unavailable"}), 503

        base_filter = _build_filter(request.args)
        rows = list(payment_transactions_collection.find(base_filter).sort("created_at", -1))

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([
            "Transaction ID", "Type", "Status", "Amount", "GST", "Net Amount",
            "Booking ID", "User Email", "Driver Email", "Gateway",
            "Razorpay Payment ID", "Admin Remarks", "Created At"
        ])
        for r in rows:
            writer.writerow([
                r.get("transaction_id", ""),
                r.get("type", ""),
                r.get("status", ""),
                r.get("amount", ""),
                r.get("gst", ""),
                r.get("net_amount", ""),
                r.get("booking_id", ""),
                r.get("user_email", ""),
                r.get("driver_email", ""),
                r.get("gateway", ""),
                r.get("razorpay_payment_id", ""),
                r.get("admin_remarks", ""),
                r["created_at"].strftime("%Y-%m-%d %H:%M") if isinstance(r.get("created_at"), datetime) else "",
            ])

        csv_data = buffer.getvalue()
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=aquaflow_payments.csv"}
        )

    return app
