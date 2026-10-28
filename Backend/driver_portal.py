"""
driver_portal.py
=====================================================
AquaFlow Driver Operations Suite - the driver-facing pages and their
JSON endpoints:

    /driver-dashboard        priority delivery, KPIs, GPS map
    /driver-trips            My Trips (search, filters, details drawer)
    /driver-earnings         wallet, payouts, statement (+ CSV)
    /driver-analytics        charts over a chosen date range
    /driver-profile          profile, photo, password
    /driver-notifications    inbox with read/unread
    /driver-support          FAQ, support requests, emergency alert
    /driver-documents        (lives in driver_documents.py)

All money comes from driver_portal_data.py, which reuses the project's
commission rule, and from payment_admin's wallet/settlement summaries -
the same functions the admin finance dashboard uses.

Delivery actions (accept / start / arrived / OTP completion) are NOT
re-implemented here: the pages call the existing, server-enforced routes in
driver_dashboard.py.
=====================================================
"""

import csv
import hmac
import io
import logging
import os
import secrets
from datetime import date, datetime, timedelta

from bson.objectid import ObjectId
from flask import (
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from driver_dashboard import driver_login_required
from driver_documents import (
    documents_overview,
    init_driver_documents,
    sync_expiry_notifications,
)
from driver_portal_data import (
    ACTIVE_STATUSES,
    NOTIFICATION_ICONS,
    STATUS_META,
    TRIP_TABS,
    WORKFLOW_STEPS,
    as_number,
    booking_ref,
    build_series,
    cash_status,
    choose_bucket,
    delivered_date,
    driver_share,
    inr,
    list_trips,
    litres,
    milestone_bonus,
    needs_cash_submission,
    notification_category,
    notification_target,
    order_fee,
    performance_summary,
    platform_commission,
    profile_completeness,
    time_ago,
    trip_date,
    trip_detail,
)
from driver_portal_secondary import register_secondary_routes
from payment_admin import (
    DRIVER_SHARE_RATE,
    get_bank_details_masked,
    get_driver_settlement_summary,
    get_driver_wallet_summary,
    mask_account_number,
)

logger = logging.getLogger(__name__)

NAV_ITEMS = (
    ("driver_dashboard", "dashboard", "Dashboard", "fa-gauge-high"),
    ("driver_trips", "trips", "My Trips", "fa-route"),
    ("driver_earnings", "earnings", "Earnings", "fa-wallet"),
    ("driver_analytics", "analytics", "Analytics", "fa-chart-column"),
    ("driver_documents", "documents", "Documents", "fa-folder-open"),
    ("driver_profile", "profile", "My Profile", "fa-id-badge"),
    ("driver_notifications", "notifications", "Notifications", "fa-bell"),
    ("driver_support", "support", "Help & Support", "fa-life-ring"),
)

# State-changing driver requests that must carry the session's CSRF token.
CSRF_PROTECTED_PREFIXES = (
    "/api/driver", "/api/accept-booking", "/api/reject-booking", "/api/update-",
    "/api/submit-collected-payment", "/api/gps/update", "/start-delivery",
    "/arrived-location", "/verify-delivery-otp", "/generate-delivery-otp",
    "/driver/", "/send-emergency-message", "/accept-booking", "/reject-booking",
)

PER_PAGE = 10


def _iso_date(value):
    """Strict YYYY-MM-DD from a query string, else None."""
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _csv_safe(value):
    """Stop spreadsheet formula injection in exported cells."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def init_driver_portal(
    app,
    drivers_collection,
    bookings_collection,
    notifications_collection,
    documents_collection,
    activity_collection,
    settings_collection,
    transactions_collection,
    support_collection,
    mail=None,
):
    # =====================================================
    # TEMPLATE HELPERS
    # =====================================================

    def driver_asset(filename):
        """static URL with a ?v=<mtime> suffix. The app serves /static with a
        one-year immutable cache, so changed CSS/JS needs a new URL to be
        picked up."""
        try:
            version = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
        except OSError:
            version = 0
        return url_for("static", filename=filename, v=version)

    def photo_url(driver):
        photo = (driver or {}).get("photo")
        if not photo:
            return None
        photo = str(photo).replace("\\", "/")
        return photo if photo.startswith(("http://", "https://", "/")) else "/" + photo

    def initials(name):
        parts = [p for p in str(name or "").split() if p]
        if not parts:
            return "D"
        return (parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")).upper()

    def fdate(value, fmt="%d %b %Y"):
        if isinstance(value, (datetime, date)):
            return value.strftime(fmt)
        return value or "-"

    def fdatetime(value):
        return value.strftime("%d %b %Y, %I:%M %p") if isinstance(value, datetime) else "-"

    def csrf_token():
        token = session.get("_driver_csrf")
        if not token:
            token = secrets.token_urlsafe(32)
            session["_driver_csrf"] = token
        return token

    # Jinja filter names are global to the whole app, and finance_management.py
    # already owns "inr" - so every filter here carries an "af_" prefix and
    # can never override (or be overridden by) another module's filters.
    app.add_template_filter(inr, "af_inr")
    app.add_template_filter(fdate, "af_date")
    app.add_template_filter(fdatetime, "af_datetime")
    app.add_template_filter(time_ago, "af_ago")
    app.add_template_filter(photo_url, "af_photo")
    app.add_template_filter(initials, "af_initials")
    app.jinja_env.globals.update(
        driver_asset=driver_asset,
        driver_csrf_token=csrf_token,
        STATUS_META=STATUS_META,
        WORKFLOW_STEPS=WORKFLOW_STEPS,
        DRIVER_SHARE_PCT=round(DRIVER_SHARE_RATE * 100),
    )

    # =====================================================
    # CSRF - state-changing driver requests only
    # -----------------------------------------------------
    # Skipped when WTF_CSRF_ENABLED is False (the test suite's existing
    # convention), enforced everywhere else.
    # =====================================================

    @app.before_request
    def _driver_csrf_guard():
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        if not app.config.get("WTF_CSRF_ENABLED", True):
            return None
        if "driver_email" not in session or not request.path.startswith(CSRF_PROTECTED_PREFIXES):
            return None
        sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or ""
        expected = session.get("_driver_csrf") or ""
        if not expected or not hmac.compare_digest(str(sent), str(expected)):
            return jsonify({
                "success": False,
                "message": "Your page was out of date. Refresh it and try again.",
                "csrf": True,
            }), 400
        return None

    # =====================================================
    # SHARED RENDERING
    # =====================================================

    def _notification_view(note):
        category = notification_category(note)
        target = notification_target(note)
        href = url_for(target) if target else None
        if target == "driver_trips" and note.get("booking_id"):
            href = url_for("driver_trips", q=str(note["booking_id"])[:8])
        support = note.get("support") if isinstance(note.get("support"), dict) else None
        if support and support.get("ticket"):
            href = url_for("driver_support", ticket=support["ticket"])
        return {
            "support": support,
            "id": str(note["_id"]),
            "title": note.get("title") or "Notification",
            "message": note.get("message") or "",
            "category": category,
            "icon": NOTIFICATION_ICONS.get(category, "fa-bell"),
            "tone": note.get("status") if note.get("status") in ("success", "warning", "danger", "info") else "info",
            "unread": note.get("read") is False,
            "ago": time_ago(note.get("created_at")),
            "when": fdatetime(note.get("created_at")),
            "href": href,
        }

    def _inbox_filter(email, extra=None):
        flt = {"driver_email": email, "type": {"$ne": "emergency"}}
        if extra:
            flt.update(extra)
        return flt

    def render_page(template, driver, page, title, subtitle, crumbs=None, **context):
        email = driver.get("email")
        unread = notifications_collection.count_documents(_inbox_filter(email, {"read": False}))
        recent = list(
            notifications_collection.find(_inbox_filter(email)).sort("created_at", -1).limit(6)
        )
        return render_template(
            template,
            driver=driver,
            page=page,
            page_title=title,
            page_subtitle=subtitle,
            crumbs=crumbs or [],
            nav_items=NAV_ITEMS,
            unread_count=unread,
            shell_notes=[_notification_view(n) for n in recent],
            **context,
        )

    def _driver_or_logout():
        driver = drivers_collection.find_one({"email": session.get("driver_email")})
        if not driver:
            session.clear()
        return driver

    def _owned_orders(email):
        return list(bookings_collection.find({"driver_email": email}))

    def _live_signature(orders):
        """Changes whenever an assignment appears, disappears or changes status."""
        return "|".join(sorted(
            f"{o['_id']}:{o.get('status')}" for o in orders
            if o.get("status") in ("Assigned",) + ACTIVE_STATUSES
        ))

    # =====================================================
    # DASHBOARD
    # =====================================================

    @app.route("/driver-dashboard")
    @driver_login_required
    def driver_dashboard():
        email = session["driver_email"]
        driver = _driver_or_logout()
        if not driver:
            return redirect(url_for("driver_login"))

        today = date.today()
        orders = _owned_orders(email)

        sync_expiry_notifications(documents_collection, notifications_collection, email)
        settlement = get_driver_settlement_summary(transactions_collection, bookings_collection, email)
        perf = performance_summary(orders)
        docs = documents_overview(app, documents_collection, email)
        completeness = profile_completeness(driver, docs, driver.get("bank_details"))

        rank = {"Arrived": 0, "On The Way": 1, "Accepted": 2}
        in_progress = sorted(
            (o for o in orders if o.get("status") in ACTIVE_STATUSES),
            key=lambda o: (rank.get(o["status"], 9), o.get("driver_accept_time") or datetime.max),
        )
        active = in_progress[0] if in_progress else None
        upcoming = [trip_detail(o, driver) for o in in_progress[1:]]
        awaiting = [
            trip_detail(o, driver)
            for o in sorted((o for o in orders if o.get("status") == "Assigned"),
                            key=lambda o: trip_date(o) or date.max)
        ]

        delivered = sorted(
            (o for o in orders if o.get("status") == "Delivered"),
            key=lambda o: o.get("delivered_at") if isinstance(o.get("delivered_at"), datetime) else datetime.min,
            reverse=True,
        )
        recent_deliveries = [
            {"id": str(o["_id"]), "ref": booking_ref(o), "date": delivered_date(o),
             "litres": litres(o), "share": driver_share(o), "cash": cash_status(o)}
            for o in delivered[:5]
        ]

        week = build_series(orders, today - timedelta(days=6), today, "day")
        cash_due_orders = [o for o in orders if needs_cash_submission(o)]
        cash_due = sum(o.get("cash_submitted_amount") or order_fee(o) for o in cash_due_orders)

        attention = [
            _notification_view(n)
            for n in notifications_collection.find(
                _inbox_filter(email, {"read": False})).sort("created_at", -1).limit(4)
        ]

        active_detail = trip_detail(active, driver) if active else None
        map_data = None
        if active:
            lat, lng = active.get("latitude"), active.get("longitude")
            map_data = {
                "booking_id": str(active["_id"]),
                "status": active.get("status"),
                "dest_lat": as_number(lat, None) if lat is not None else None,
                "dest_lng": as_number(lng, None) if lng is not None else None,
                "address": active.get("address") or "",
            }

        subtitle = "Here's what needs your attention today."
        return render_page(
            "driver_dashboard.html", driver, "dashboard", "Dashboard", subtitle,
            kpis={
                "total_trips": perf["total_trips"],
                "active": perf["active"],
                "awaiting": perf["awaiting_response"],
                "completed": perf["completed"],
                "today": settlement["todays_earnings"],
                "pending_payouts": settlement["pending_settlement_total"],
                "withdrawable": settlement["withdrawable"],
                "rating": perf["rating"],
                "rating_count": perf["rating_count"],
            },
            perf=perf,
            signature=_live_signature(orders),
            active=active_detail,
            map_data=map_data,
            awaiting=awaiting,
            upcoming=upcoming,
            recent_deliveries=recent_deliveries,
            week_series=[{"label": p["label"], "earnings": p["earnings"], "deliveries": p["deliveries"]} for p in week],
            week_total=sum(p["earnings"] for p in week),
            cash_due=cash_due,
            cash_due_count=len(cash_due_orders),
            attention=attention,
            docs=docs,
            completeness=completeness,
            last_gps=driver.get("last_gps_at") if isinstance(driver.get("last_gps_at"), datetime) else None,
        )

    @app.route("/api/driver/dashboard-live")
    @driver_login_required
    def api_driver_dashboard_live():
        """Cheap poll: lets the open dashboard notice a new/changed assignment."""
        email = session["driver_email"]
        orders = _owned_orders(email)
        perf = performance_summary(orders)
        return jsonify({
            "success": True,
            "signature": _live_signature(orders),
            "active": perf["active"],
            "awaiting": perf["awaiting_response"],
            "unread": notifications_collection.count_documents(_inbox_filter(email, {"read": False})),
        })

    # =====================================================
    # MY TRIPS
    # =====================================================

    @app.route("/driver-trips")
    @driver_login_required
    def driver_trips():
        email = session["driver_email"]
        driver = _driver_or_logout()
        if not driver:
            return redirect(url_for("driver_login"))

        args = request.args
        date_from, date_to = _iso_date(args.get("from")), _iso_date(args.get("to"))
        notice = None
        if date_from and date_to and date_from > date_to:
            date_from, date_to = date_to, date_from
            notice = "The dates were the wrong way round, so we swapped them."
        try:
            page = int(args.get("page", 1))
        except ValueError:
            page = 1

        owned = _owned_orders(email)
        declined = [
            o for o in bookings_collection.find({"rejected_by_drivers": email})
            if o.get("driver_email") != email
        ]
        result = list_trips(
            owned, declined,
            tab=args.get("tab", "all"), query=args.get("q", ""),
            date_from=date_from, date_to=date_to,
            sort=args.get("sort", "newest"), page=page, per_page=PER_PAGE,
        )
        base_args = {
            k: v for k, v in {
                "tab": result["tab"] if result["tab"] != "all" else None,
                "q": (args.get("q") or "").strip() or None,
                "from": date_from.isoformat() if date_from else None,
                "to": date_to.isoformat() if date_to else None,
                "sort": result["sort"] if result["sort"] != "newest" else None,
            }.items() if v
        }
        filtered = bool(base_args.get("q") or base_args.get("from") or base_args.get("to"))

        def link(overrides):
            merged = {**base_args, **overrides}
            return url_for("driver_trips", **{k: v for k, v in merged.items() if v not in (None, "")})

        tab_links = {t: link({"tab": t if t != "all" else None}) for t in TRIP_TABS}
        prev_url = link({"page": result["page"] - 1}) if result["page"] > 1 else None
        next_url = link({"page": result["page"] + 1}) if result["page"] < result["pages"] else None
        clear_url = link({"q": None, "from": None, "to": None, "sort": None})

        return render_page(
            "driver_trips.html", driver, "trips", "My Trips",
            "Every delivery assigned to you, from new requests to completed jobs.",
            crumbs=[("Dashboard", "driver_dashboard"), ("My Trips", None)],
            result=result, filtered=filtered, notice=notice, tabs=TRIP_TABS,
            tab_links=tab_links, prev_url=prev_url, next_url=next_url, clear_url=clear_url,
            date_from=date_from, date_to=date_to, query=args.get("q", ""),
        )

    @app.route("/api/driver/trips/<booking_id>")
    @driver_login_required
    def api_driver_trip_detail(booking_id):
        try:
            oid = ObjectId(booking_id)
        except Exception:
            return jsonify({"success": False, "message": "Invalid booking ID"}), 400
        # The owner is part of the query: another driver's id simply isn't found.
        order = bookings_collection.find_one({"_id": oid, "driver_email": session["driver_email"]})
        if not order:
            return jsonify({"success": False, "message": "This trip isn't assigned to you."}), 404
        driver = drivers_collection.find_one({"email": session["driver_email"]}) or {}
        detail = trip_detail(order, driver)
        detail["date"] = detail["date"].isoformat() if detail["date"] else None
        return jsonify({"success": True, "trip": detail})

    # =====================================================
    # EARNINGS
    # =====================================================

    def _earnings_filters():
        args = request.args
        date_from, date_to = _iso_date(args.get("from")), _iso_date(args.get("to"))
        if date_from and date_to and date_from > date_to:
            date_from, date_to = date_to, date_from
        query = (args.get("q") or "").strip().lower().lstrip("#")
        return date_from, date_to, query

    def _earning_rows(orders, date_from, date_to, query):
        rows = []
        for order in orders:
            if order.get("status") != "Delivered":
                continue
            when = delivered_date(order)
            if date_from and (not when or when < date_from):
                continue
            if date_to and (not when or when > date_to):
                continue
            if query and query not in str(order["_id"]).lower():
                continue
            label, tone = cash_status(order)
            rows.append({
                "id": str(order["_id"]), "ref": booking_ref(order),
                "at": order.get("delivered_at"), "date": when,
                "litres": litres(order), "fee": order_fee(order),
                "share": driver_share(order), "commission": platform_commission(order),
                "cash_label": label, "cash_tone": tone,
                "can_submit": needs_cash_submission(order),
                "cash_amount": order.get("cash_submitted_amount") or order_fee(order),
            })
        rows.sort(key=lambda r: r["at"] if isinstance(r["at"], datetime) else datetime.min, reverse=True)
        return rows

    def _payout_rows(requests_list, date_from, date_to, query):
        rows = []
        for req in requests_list:
            requested = req.get("requested_at")
            when = requested.date() if isinstance(requested, datetime) else None
            if date_from and (not when or when < date_from):
                continue
            if date_to and (not when or when > date_to):
                continue
            reference = req.get("bank_reference") or ""
            if query and query not in reference.lower() and query not in str(req.get("_id", "")).lower():
                continue
            rows.append({
                "id": str(req.get("_id", "")),
                "at": requested,
                "amount": req.get("amount", 0),
                "status": req.get("status") or "Pending",
                "reference": reference,
                "paid_at": req.get("paid_at") or (req.get("resolved_at") if req.get("status") == "Completed" else None),
            })
        return rows

    @app.route("/driver-earnings")
    @driver_login_required
    def driver_earnings():
        email = session["driver_email"]
        driver = _driver_or_logout()
        if not driver:
            return redirect(url_for("driver_login"))

        settlement = get_driver_settlement_summary(transactions_collection, bookings_collection, email)
        wallet = get_driver_wallet_summary(bookings_collection, email)
        orders = _owned_orders(email)
        completed_count = sum(1 for o in orders if o.get("status") == "Delivered")

        date_from, date_to, query = _earnings_filters()
        rows = _earning_rows(orders, date_from, date_to, query)
        payouts = _payout_rows(settlement["requests"], date_from, date_to, query)

        try:
            page = max(1, int(request.args.get("page", 1)))
        except ValueError:
            page = 1
        pages = max(1, -(-len(rows) // PER_PAGE))
        page = min(page, pages)
        page_rows = rows[(page - 1) * PER_PAGE: page * PER_PAGE]

        today = date.today()
        chart_to = min(date_to or today, today)
        chart_from = date_from or (chart_to - timedelta(days=13))
        if (chart_to - chart_from).days > 365:
            chart_from = chart_to - timedelta(days=365)
        bucket = choose_bucket(chart_from, chart_to)
        series = build_series(orders, chart_from, chart_to, bucket)

        base_args = {k: v for k, v in {
            "q": query or None,
            "from": date_from.isoformat() if date_from else None,
            "to": date_to.isoformat() if date_to else None,
        }.items() if v}

        bank = driver.get("bank_details") or None
        bank_view = None
        if bank:
            bank_view = {
                "holder": bank.get("account_holder_name"),
                "account": mask_account_number(bank.get("account_number")),
                "ifsc": bank.get("ifsc"),
                "status": bank.get("approval_status") or "Pending",
            }
        bank_approved = bool(bank_view and bank_view["status"] == "Approved")

        filtered_total = sum(r["share"] for r in rows)

        def link(endpoint, overrides):
            merged = {**base_args, **overrides}
            return url_for(endpoint, **{k: v for k, v in merged.items() if v not in (None, "")})

        prev_url = link("driver_earnings", {"page": page - 1}) if page > 1 else None
        next_url = link("driver_earnings", {"page": page + 1}) if page < pages else None
        clear_url = url_for("driver_earnings")
        csv_url = link("driver_earnings_csv", {})

        return render_page(
            "driver_earnings.html", driver, "earnings", "Earnings",
            "Your earnings, payouts and the cash you owe the company.",
            crumbs=[("Dashboard", "driver_dashboard"), ("Earnings", None)],
            settlement=settlement, wallet=wallet,
            rows=page_rows, total_rows=len(rows), cur_page=page, pages=pages,
            payouts=payouts[:20], base_args=base_args,
            filtered=bool(base_args), query=query, date_from=date_from, date_to=date_to,
            prev_url=prev_url, next_url=next_url, clear_url=clear_url, csv_url=csv_url,
            filtered_total=filtered_total,
            filtered_fees=sum(r["fee"] for r in rows),
            filtered_commission=sum(r["commission"] for r in rows),
            chart={"bucket": bucket, "series": [
                {"label": p["label"], "earnings": p["earnings"], "deliveries": p["deliveries"]} for p in series]},
            chart_total=sum(p["earnings"] for p in series),
            bonus=milestone_bonus(completed_count), completed_count=completed_count,
            bank=bank_view, bank_approved=bank_approved,
            company_bank=get_bank_details_masked(settings_collection),
            cash_due_rows=[r for r in _earning_rows(orders, None, None, "") if r["can_submit"]],
        )

    @app.route("/driver-earnings/statement.csv")
    @driver_login_required
    def driver_earnings_csv():
        email = session["driver_email"]
        settlement = get_driver_settlement_summary(transactions_collection, bookings_collection, email)
        date_from, date_to, query = _earnings_filters()
        rows = _earning_rows(_owned_orders(email), date_from, date_to, query)
        payouts = _payout_rows(settlement["requests"], date_from, date_to, query)

        entries = []
        for r in rows:
            entries.append((r["at"], [
                r["date"].isoformat() if r["date"] else "", "Delivery earning", r["ref"],
                f"{r['litres']} L; fee {r['fee']}; platform commission {r['commission']}",
                r["share"], "Earned"]))
        for p in payouts:
            entries.append((p["at"], [
                p["at"].date().isoformat() if isinstance(p["at"], datetime) else "", "Payout request",
                p["reference"], "Withdrawal to bank account", -p["amount"], p["status"]]))
        entries.sort(key=lambda e: e[0] if isinstance(e[0], datetime) else datetime.min, reverse=True)

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["Date", "Type", "Reference", "Details", "Amount (INR)", "Status"])
        for _, values in entries:
            writer.writerow([_csv_safe(v) if isinstance(v, str) else v for v in values])

        filename = f"aquaflow-statement-{date.today():%Y%m%d}.csv"
        return Response(
            "\ufeff" + buffer.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # Analytics, profile, notifications, support and the admin support queue.
    register_secondary_routes(
        app, drivers_collection, bookings_collection, notifications_collection,
        documents_collection, activity_collection, support_collection, mail,
        render_page, _driver_or_logout, _owned_orders, _inbox_filter, _notification_view,
    )

    init_driver_documents(
        app, drivers_collection, documents_collection, notifications_collection,
        activity_collection, driver_login_required, render_page,
    )
    return app

