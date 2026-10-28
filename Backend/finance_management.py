"""
finance_management.py
======================

The Finance Management admin page.

Driver Management (driver_management.py) owns everything about a
driver as a person/vehicle/employee. This module owns everything
about MONEY moving between AquaFlow and its drivers:

    - the company's own bank/UPI details (where drivers send cash
      they've collected)
    - driver bank-account approvals (must happen before a driver can
      request a payout)
    - driver settlement (payout) requests - money flowing
      company -> driver
    - COD cash-collection verification - money flowing
      driver -> company
    - the full payout payment history / ledger

All the actual business logic already lives in payment_admin.py
(bank details, settlement resolution, payment verification, etc.)
and payment_ledger.py (the unified transaction ledger) - this module
does not duplicate or change any of it. It only gathers everything
those modules already compute into one enriched, finance-focused
view, and adds read-only conveniences on top of it:

    - search / status / date-range / sort / pagination for the payout
      history (server-side, so it works on the full history, not just
      the rows currently on screen)
    - CSV exports of the payout history and cash-verification history
      (they honour the same filters)
    - a small JSON endpoint the page uses to refresh its summary cards
    - an Indian-rupee template filter (`|inr`)
"""

import csv
import io
import logging
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from flask import Response, jsonify, redirect, render_template, request, session, url_for

from payment_ledger import cod_status_label
from payment_admin import (
    get_bank_details,
    get_driver_settlement_summary,
    get_driver_wallet_summary,
    get_finance_overview,
    get_ledger_overview,
    mask_account_number,
)

logger = logging.getLogger(__name__)

HISTORY_PAGE_SIZE = 15
RECENT_VERIFICATIONS_LIMIT = 25
RECENT_ACTIVITY_LIMIT = 8

# (value, label) - the value is what travels in the URL.
HISTORY_STATUS_CHOICES = (
    ("withdrawn", "Paid & approved"),
    ("paid", "Paid"),
    ("processing", "Approved (processing)"),
    ("pending", "Pending"),
    ("rejected", "Rejected"),
    ("all", "All statuses"),
)

HISTORY_SORT_CHOICES = (
    ("date_desc", "Newest first"),
    ("date_asc", "Oldest first"),
    ("amount_desc", "Amount: high to low"),
    ("amount_asc", "Amount: low to high"),
    ("status", "Status"),
)

_HISTORY_SORT_SPECS = {
    "date_desc": [("updated_at", -1)],
    "date_asc": [("updated_at", 1)],
    "amount_desc": [("amount", -1), ("updated_at", -1)],
    "amount_asc": [("amount", 1), ("updated_at", -1)],
    "status": [("status", 1), ("updated_at", -1)],
}

COD_STATUS_CHOICES = (
    ("decided", "Approved & rejected"),
    ("Approved", "Approved"),
    ("Rejected", "Rejected"),
    ("Pending Verification", "Pending verification"),
    ("all", "All"),
)


# =====================================================
# FORMATTING HELPERS (also registered as Jinja filters)
# =====================================================

def format_inr(value, decimals=2):
    """Indian-rupee formatting with lakh/crore grouping: 125000 -> ₹1,25,000.00.

    Safe on None / blank / garbage (-> ₹0.00) so a bad record can never
    crash the page. Zero is a real value and is formatted as such.
    """
    try:
        amount = Decimal(str(value if value not in (None, "") else 0))
        if not amount.is_finite():
            amount = Decimal(0)
    except (InvalidOperation, ValueError, TypeError):
        amount = Decimal(0)

    quant = Decimal(1).scaleb(-decimals) if decimals else Decimal(1)
    amount = amount.quantize(quant, rounding=ROUND_HALF_UP)
    negative = amount < 0
    text = f"{abs(amount):f}"
    whole, _, frac = text.partition(".")

    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])

    result = "₹" + whole + (("." + frac) if decimals else "")
    return "-" + result if negative else result


def format_datetime(value, fmt="%d %b %Y, %I:%M %p"):
    """Dates in the same style the page has always used; '—' when missing."""
    if isinstance(value, datetime):
        return value.strftime(fmt)
    return "—"


def epoch(value):
    """Seconds since epoch (0 when missing) - used for client-side sorting."""
    if isinstance(value, datetime):
        try:
            return int(value.timestamp())
        except (OverflowError, OSError, ValueError):
            return 0
    return 0


def _csv_cell(value):
    """Neutralise spreadsheet formula injection in exported text."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _parse_date(value):
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d")
    except ValueError:
        return None


def _choice(value, choices, default):
    return value if value in {c[0] for c in choices} else default


def _csv_response(filename, header, rows):
    buffer = io.StringIO()
    buffer.write("\ufeff")  # BOM so Excel opens ₹ and non-English names correctly
    writer = csv.writer(buffer)
    writer.writerow(header)
    for row in rows:
        writer.writerow([_csv_cell(cell) for cell in row])
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# =====================================================
# MODULE INIT
# =====================================================

def init_finance_management(
    app,
    drivers_collection,
    bookings_collection,
    settings_collection=None,
    transactions_collection=None,
    payment_transactions_collection=None,
    notifications_collection=None
):

    app.add_template_filter(format_inr, "inr")
    app.add_template_filter(format_datetime, "fin_dt")
    app.add_template_filter(epoch, "fin_epoch")

    def _admin_ok():
        return "admin_email" in session or "admin" in session

    # -------------------------------------------------
    # small shared lookups
    # -------------------------------------------------

    def _driver_directory(emails):
        """{email: driver document} for the given emails (one query)."""
        wanted = sorted({e for e in emails if e})
        if not wanted or drivers_collection is None:
            return {}
        return {
            d.get("email"): d
            for d in drivers_collection.find({"email": {"$in": wanted}})
        }

    def _settlement_payment_ids(status):
        """Ledger transaction ids of settlement requests currently in `status`."""
        if transactions_collection is None:
            return []
        return [
            s.get("payment_transaction_id")
            for s in transactions_collection.find(
                {"type": "settlement", "status": status},
                {"payment_transaction_id": 1}
            )
            if s.get("payment_transaction_id")
        ]

    # -------------------------------------------------
    # PAYOUT HISTORY (ledger rows of type "withdrawal")
    # -------------------------------------------------

    def _history_params(args):
        return {
            "q": (args.get("h_q") or "").strip()[:80],
            "status": _choice(args.get("h_status"), HISTORY_STATUS_CHOICES, "withdrawn"),
            "date_from": (args.get("h_from") or "").strip() if _parse_date(args.get("h_from")) else "",
            "date_to": (args.get("h_to") or "").strip() if _parse_date(args.get("h_to")) else "",
            "sort": _choice(args.get("h_sort"), HISTORY_SORT_CHOICES, "date_desc"),
        }

    def _history_filter(params):
        flt = {"type": "withdrawal"}

        status = params["status"]
        if status == "withdrawn":
            flt["status"] = "Withdrawn"
        elif status == "paid":
            flt["status"] = "Withdrawn"
            processing_ids = _settlement_payment_ids("Processing")
            if processing_ids:
                flt["transaction_id"] = {"$nin": processing_ids}
        elif status == "processing":
            flt["status"] = "Withdrawn"
            flt["transaction_id"] = {"$in": _settlement_payment_ids("Processing")}
        elif status == "pending":
            flt["status"] = "Pending"
        elif status == "rejected":
            flt["status"] = "Rejected"
        # "all": no status restriction

        if params["q"]:
            rx = {"$regex": re.escape(params["q"]), "$options": "i"}
            clauses = [
                {"transaction_id": rx},
                {"driver_email": rx},
                {"bank_reference": rx},
                {"admin_remarks": rx},
            ]
            if drivers_collection is not None:
                matched_emails = [
                    d.get("email")
                    for d in drivers_collection.find(
                        {"$or": [{"name": rx}, {"phone": rx}]}, {"email": 1}
                    )
                    if d.get("email")
                ]
                if matched_emails:
                    clauses.append({"driver_email": {"$in": matched_emails}})
            flt["$or"] = clauses

        date_range = {}
        start = _parse_date(params["date_from"])
        end = _parse_date(params["date_to"])
        if start:
            date_range["$gte"] = start
        if end:
            date_range["$lt"] = end + timedelta(days=1)
        if date_range:
            flt["updated_at"] = date_range

        return flt

    def _decorate_history(rows):
        """Attach driver name/phone, the originating settlement request (for the
        receipt link) and a human status (Paid vs Processing) to ledger rows."""
        directory = _driver_directory(r.get("driver_email") for r in rows)

        settlements = {}
        if transactions_collection is not None and rows:
            ids = [r.get("transaction_id") for r in rows if r.get("transaction_id")]
            for s in transactions_collection.find({"payment_transaction_id": {"$in": ids}}):
                settlements[s.get("payment_transaction_id")] = s

        for txn in rows:
            driver = directory.get(txn.get("driver_email")) or {}
            settlement = settlements.get(txn.get("transaction_id"))

            txn["driver_name"] = driver.get("name") or txn.get("driver_email")
            txn["driver_phone"] = driver.get("phone") or ""
            txn["settlement_request_id"] = str(settlement["_id"]) if settlement else None
            txn["bank_reference"] = txn.get("bank_reference") or (settlement or {}).get("bank_reference") or ""

            if txn.get("status") == "Withdrawn":
                txn["status_label"] = (
                    "Processing" if (settlement or {}).get("status") == "Processing" else "Paid"
                )
            else:
                txn["status_label"] = txn.get("status") or "—"
        return rows

    def _load_history(args, paginate=True):
        params = _history_params(args)
        flt = _history_filter(params)

        total = payment_transactions_collection.count_documents(flt)
        pages = max(1, -(-total // HISTORY_PAGE_SIZE))
        try:
            page = min(max(1, int(args.get("h_page", 1))), pages)
        except (TypeError, ValueError):
            page = 1

        cursor = payment_transactions_collection.find(flt).sort(_HISTORY_SORT_SPECS[params["sort"]])
        if paginate:
            cursor = cursor.skip((page - 1) * HISTORY_PAGE_SIZE).limit(HISTORY_PAGE_SIZE)

        rows = _decorate_history(list(cursor))
        return {
            **params,
            "rows": rows,
            "total": total,
            "page": page,
            "pages": pages,
            "page_size": HISTORY_PAGE_SIZE,
            "first": (page - 1) * HISTORY_PAGE_SIZE + 1 if total else 0,
            "last": min(page * HISTORY_PAGE_SIZE, total),
            "active": bool(
                params["q"] or params["date_from"] or params["date_to"]
                or params["status"] != "withdrawn" or params["sort"] != "date_desc"
            ),
        }

    # -------------------------------------------------
    # CASH (COD) verification history
    # -------------------------------------------------

    def _cod_filter(args):
        status = _choice(args.get("c_status"), COD_STATUS_CHOICES, "decided")
        flt = {}
        if status == "decided":
            flt["payment_submission_status"] = {"$in": ["Approved", "Rejected"]}
        elif status == "all":
            flt["payment_submission_status"] = {"$in": ["Approved", "Rejected", "Pending Verification"]}
        else:
            flt["payment_submission_status"] = status

        q = (args.get("c_q") or "").strip()[:80]
        if q:
            rx = {"$regex": re.escape(q), "$options": "i"}
            clauses = [
                {"driver_name": rx},
                {"driver_email": rx},
                {"payment_transaction_ref": rx},
            ]
            if drivers_collection is not None:
                matched = [
                    d.get("email")
                    for d in drivers_collection.find(
                        {"$or": [{"name": rx}, {"phone": rx}]}, {"email": 1}
                    )
                    if d.get("email")
                ]
                if matched:
                    clauses.append({"driver_email": {"$in": matched}})
            flt["$or"] = clauses

        date_range = {}
        start = _parse_date(args.get("c_from"))
        end = _parse_date(args.get("c_to"))
        if start:
            date_range["$gte"] = start
        if end:
            date_range["$lt"] = end + timedelta(days=1)
        if date_range:
            flt["payment_verified_at"] = date_range
        return flt

    # -------------------------------------------------
    # cash currently held by drivers (cash reconciliation)
    # -------------------------------------------------

    def _cash_holders():
        """Approved drivers who still owe the company cash they collected.
        Same wallet maths the driver dashboard and finance totals already use."""
        holders = []
        if drivers_collection is None:
            return holders
        for driver in drivers_collection.find({"approval_status": "Approved"}):
            wallet = get_driver_wallet_summary(bookings_collection, driver.get("email"))
            if wallet["pending_deposit"] > 0:
                holders.append({
                    "name": driver.get("name") or driver.get("email"),
                    "email": driver.get("email"),
                    "phone": driver.get("phone") or "",
                    "collected": wallet["collected_total"],
                    "verified": wallet["approved_total"],
                    "in_review": wallet["pending_verification_total"],
                    "outstanding": wallet["pending_deposit"],
                })
        holders.sort(key=lambda h: h["outstanding"], reverse=True)
        return holders

    # -------------------------------------------------
    # summary numbers shared by the page and the refresh endpoint
    # -------------------------------------------------

    def _summary_payload():
        finance = get_finance_overview(
            bookings_collection, drivers_collection, transactions_collection
        )
        ledger_overview = get_ledger_overview(payment_transactions_collection)
        return finance, ledger_overview

    # =====================================================
    # MAIN PAGE
    # =====================================================

    @app.route("/finance-management")
    def finance_management():

        # =========================================
        # ADMIN LOGIN CHECK
        # =========================================

        if not _admin_ok():
            return redirect(url_for("admin_login"))

        args = request.args
        section_errors = {}

        def _safe(section, default, loader):
            """Load one section. If its query fails the page still renders and
            that section shows an error state - it never shows made-up zeros."""
            try:
                return loader()
            except Exception:
                logger.exception("Finance Management: could not load %s", section)
                section_errors[section] = True
                return default

        # =========================================
        # COMPANY BANK / UPI DETAILS
        # Shown here for the admin to edit, and on
        # every driver's dashboard so they know
        # where to send collected cash.
        # The full account number is never rendered
        # into this page - only the masked version;
        # admins reveal it on demand.
        # =========================================

        def _load_bank():
            details = get_bank_details(settings_collection)
            details["has_account_number"] = bool(details.get("account_number"))
            details["account_number"] = mask_account_number(details.get("account_number"))
            return details

        bank_details = _safe("bank", {}, _load_bank)

        # =========================================
        # PENDING PAYMENT VERIFICATIONS (COD)
        # every delivery where a driver has
        # submitted a transaction reference /
        # screenshot that admin hasn't checked yet
        # =========================================

        def _load_pending_cod():
            rows = list(
                bookings_collection.find({
                    "payment_submitted": True,
                    "payment_submission_status": "Pending Verification"
                }).sort("payment_submitted_at", -1)
            )
            directory = _driver_directory(r.get("driver_email") for r in rows)
            for r in rows:
                r["driver_phone"] = (directory.get(r.get("driver_email")) or {}).get("phone") or ""
            return rows

        pending_verifications = _safe("cod", [], _load_pending_cod)

        def _load_recent_cod():
            rows = list(
                bookings_collection.find({
                    "payment_submission_status": {"$in": ["Approved", "Rejected"]}
                }).sort("payment_verified_at", -1).limit(RECENT_VERIFICATIONS_LIMIT)
            )
            directory = _driver_directory(r.get("driver_email") for r in rows)
            for r in rows:
                r["driver_phone"] = (directory.get(r.get("driver_email")) or {}).get("phone") or ""
            return rows

        recent_verifications = _safe("cod", [], _load_recent_cod)

        # =========================================
        # PENDING DRIVER BANK-DETAILS APPROVALS
        # withdrawals stay locked for a driver until
        # their bank details are approved here
        # =========================================

        def _load_pending_banks():
            rows = list(drivers_collection.find({"bank_details.approval_status": "Pending"}))
            for d in rows:
                bank = d.get("bank_details") or {}
                d["bank_account_masked"] = mask_account_number(bank.get("account_number"))
            return rows

        pending_bank_details = _safe("banks", [], _load_pending_banks)

        # =========================================
        # DRIVER SETTLEMENT (PAYOUT) REQUESTS
        # Enriched with everything the admin needs
        # to actually pay a driver, without having
        # to jump to another page: driver profile,
        # bank/UPI info, verification status,
        # earnings/paid/pending totals, all computed
        # live from MongoDB.
        # =========================================

        def _load_settlements():
            result = []
            if transactions_collection is None:
                return result

            raw_requests = list(
                transactions_collection.find({
                    "type": "settlement",
                    "status": {"$in": ["Pending", "Processing"]}
                }).sort("requested_at", -1)
            )
            directory = _driver_directory(r.get("driver_email") for r in raw_requests)

            for req in raw_requests:
                driver = directory.get(req.get("driver_email"))

                bank = (driver or {}).get("bank_details") or {}
                settlement_summary = get_driver_settlement_summary(
                    transactions_collection, bookings_collection,
                    req.get("driver_email")
                )

                req["driver_photo"] = (driver or {}).get("photo")
                req["driver_name"] = (driver or {}).get("name", req.get("driver_email"))
                req["driver_id"] = str((driver or {}).get("_id", ""))
                req["driver_phone"] = (driver or {}).get("phone")
                req["bank_account_holder"] = bank.get("account_holder_name")
                req["bank_name"] = bank.get("bank_name")
                req["bank_account_number_masked"] = mask_account_number(bank.get("account_number"))
                req["bank_ifsc"] = bank.get("ifsc")
                req["bank_upi_id"] = bank.get("upi_id")
                req["bank_verification_status"] = bank.get("approval_status", "Not Submitted")
                req["total_earnings"] = settlement_summary["driver_earned_total"]
                req["total_paid"] = settlement_summary["settled_total"]
                req["current_pending_payout"] = settlement_summary["withdrawable"]

                result.append(req)
            return result

        pending_settlements = _safe("settlements", [], _load_settlements)

        # =========================================
        # PAYOUT / SETTLEMENT PAYMENT HISTORY
        # Full audit trail of driver payouts, pulled
        # from the unified payment ledger
        # (payment_ledger.py) - the single source of
        # truth for transaction references, payment
        # method and who confirmed it. Searchable,
        # filterable, sortable and paginated.
        # =========================================

        history_default = {
            "rows": [], "total": 0, "page": 1, "pages": 1, "page_size": HISTORY_PAGE_SIZE,
            "first": 0, "last": 0, "active": False, "q": "", "status": "withdrawn",
            "date_from": "", "date_to": "", "sort": "date_desc",
        }

        def _history_loader():
            if payment_transactions_collection is None:
                return history_default
            return _load_history(args)

        history = _safe("history", history_default, _history_loader)
        payment_history = history["rows"]  # kept for backward compatibility

        # =========================================
        # FINANCE-WIDE OVERVIEW STATS
        # =========================================

        finance = None
        ledger_overview = None
        try:
            finance, ledger_overview = _summary_payload()
        except Exception:
            logger.exception("Finance Management: could not compute the summary figures")
            section_errors["metrics"] = True

        # =========================================
        # CASH RECONCILIATION + RECENT ACTIVITY
        # =========================================

        cash_holders = _safe("cash", [], _cash_holders)

        def _load_activity():
            events = []
            if payment_transactions_collection is not None:
                rows = list(
                    payment_transactions_collection.find({
                        "type": {"$in": ["withdrawal", "cod_collection"]},
                        "status": {"$ne": "Superseded"}   # stale duplicates kept only for audit
                    }).sort("updated_at", -1).limit(RECENT_ACTIVITY_LIMIT)
                )
                directory = _driver_directory(r.get("driver_email") for r in rows)
                for r in rows:
                    is_payout = r.get("type") == "withdrawal"
                    status = r.get("status")
                    if is_payout:
                        label = {"Withdrawn": "Paid", "Pending": "Pending", "Rejected": "Rejected"}.get(status, status)
                    else:
                        label = cod_status_label(status)   # the ledger row's own status is the only source
                    events.append({
                        "kind": "Driver payout" if is_payout else "Cash deposit",
                        "direction": "out" if is_payout else "in",
                        "driver": (directory.get(r.get("driver_email")) or {}).get("name") or r.get("driver_email") or "—",
                        "amount": r.get("amount") or 0,
                        "when": r.get("updated_at"),
                        "status": label,
                        "ref": r.get("transaction_id"),
                    })
            # A pending submission already has its ledger row above; only add a
            # booking-derived line for legacy submissions that have none, so the
            # same deposit is never listed twice.
            ledgered = set()
            if payment_transactions_collection is not None and pending_verifications:
                ledgered = {
                    r.get("booking_id") for r in payment_transactions_collection.find(
                        {"type": "cod_collection", "status": {"$ne": "Superseded"},
                         "booking_id": {"$in": [str(o.get("_id")) for o in pending_verifications]}},
                        {"booking_id": 1})
                }
            for order in pending_verifications:
                if str(order.get("_id")) in ledgered:
                    continue
                events.append({
                    "kind": "Cash deposit",
                    "direction": "in",
                    "driver": order.get("driver_name") or order.get("driver_email") or "—",
                    "amount": order.get("cash_submitted_amount", order.get("delivery_fee", 500)),
                    "when": order.get("payment_submitted_at"),
                    "status": "Pending",
                    "ref": order.get("payment_transaction_ref"),
                })
            events.sort(key=lambda e: epoch(e["when"]), reverse=True)
            return events[:RECENT_ACTIVITY_LIMIT]

        recent_activity = _safe("activity", [], _load_activity)

        # =========================================
        # RENDER PAGE
        # =========================================

        initial_tab = "history" if any(k.startswith("h_") for k in args) else ""

        return render_template(
            "finance_management.html",

            bank_details=bank_details,

            pending_verifications=pending_verifications,
            recent_verifications=recent_verifications,

            pending_bank_details=pending_bank_details,

            pending_settlements=pending_settlements,
            payment_history=payment_history,
            history=history,
            history_status_choices=HISTORY_STATUS_CHOICES,
            history_sort_choices=HISTORY_SORT_CHOICES,
            cod_status_choices=COD_STATUS_CHOICES,

            cash_holders=cash_holders,
            recent_activity=recent_activity,

            finance=finance,
            ledger_overview=ledger_overview,
            section_errors=section_errors,
            initial_tab=initial_tab,

            finance_nav_current="finance",
            current_time=datetime.now()
        )

    # =====================================================
    # SUMMARY CARDS REFRESH (JSON)
    # =====================================================

    @app.route("/finance-management/api/summary")
    def finance_summary_api():
        if not _admin_ok():
            return jsonify({"ok": False, "message": "Unauthorized"}), 401

        try:
            finance, ledger_overview = _summary_payload()
        except Exception:
            logger.exception("Finance summary refresh failed")
            return jsonify({"ok": False, "message": "Could not load the latest figures."}), 500

        return jsonify({
            "ok": True,
            "updated": datetime.now().strftime("%d %b, %I:%M %p"),
            "values": {
                "pending_driver_deposits": format_inr(finance["pending_driver_deposits"]),
                "pending_driver_payouts": format_inr(finance["pending_driver_payouts"]),
                "today_revenue": format_inr(finance["today_revenue"]),
                "profit": format_inr(finance["profit"]),
                "gst_collected": format_inr(ledger_overview["gst_collected"]),
                "total_withdrawals": format_inr(ledger_overview["total_withdrawals"]),
            },
        })

    # =====================================================
    # CSV EXPORTS (same filters as the on-screen tables)
    # =====================================================

    @app.route("/finance-management/export/payouts.csv")
    def finance_export_payouts():
        if not _admin_ok():
            return redirect(url_for("admin_login"))
        if payment_transactions_collection is None:
            return Response("Payment ledger unavailable", status=503)

        data = _load_history(request.args, paginate=False)
        rows = [
            [
                t.get("transaction_id", ""),
                t.get("bank_reference", ""),
                t.get("driver_name", ""),
                t.get("driver_email", ""),
                t.get("driver_phone", ""),
                f"{(t.get('amount') or 0):.2f}",
                (t.get("gateway") or "bank_transfer").replace("_", " ").title(),
                t.get("status_label", ""),
                t.get("admin_remarks", "") or "",
                t["updated_at"].strftime("%Y-%m-%d %H:%M") if isinstance(t.get("updated_at"), datetime) else "",
                t.get("settlement_request_id") or "",
            ]
            for t in data["rows"]
        ]
        return _csv_response(
            f"aquaflow-payouts-{datetime.now():%Y%m%d}.csv",
            ["Transaction Ref", "Bank Reference", "Driver", "Driver Email", "Driver Phone",
             "Amount (INR)", "Method", "Status", "Processed By", "Date", "Settlement ID"],
            rows,
        )

    @app.route("/finance-management/export/cash-verifications.csv")
    def finance_export_cash():
        if not _admin_ok():
            return redirect(url_for("admin_login"))

        orders = list(
            bookings_collection.find(_cod_filter(request.args)).sort("payment_verified_at", -1)
        )
        directory = _driver_directory(o.get("driver_email") for o in orders)

        def when(value):
            return value.strftime("%Y-%m-%d %H:%M") if isinstance(value, datetime) else ""

        rows = [
            [
                str(o["_id"]),
                o.get("driver_name") or (directory.get(o.get("driver_email")) or {}).get("name") or "",
                o.get("driver_email", ""),
                (directory.get(o.get("driver_email")) or {}).get("phone") or "",
                f"{(o.get('cash_submitted_amount') or o.get('delivery_fee', 500)):.2f}",
                o.get("payment_submission_method", ""),
                o.get("payment_transaction_ref", ""),
                o.get("payment_submission_status", ""),
                when(o.get("payment_submitted_at")),
                when(o.get("payment_verified_at")),
                o.get("payment_verified_by", ""),
                o.get("payment_verification_remarks", ""),
            ]
            for o in orders
        ]
        return _csv_response(
            f"aquaflow-cash-verifications-{datetime.now():%Y%m%d}.csv",
            ["Booking", "Driver", "Driver Email", "Driver Phone", "Amount (INR)", "Method",
             "Transaction Ref", "Status", "Submitted", "Decided", "Decided By", "Remarks"],
            rows,
        )

    return app
