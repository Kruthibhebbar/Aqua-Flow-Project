"""
driver_portal_data.py
=====================================================
Pure calculations behind the Driver Operations Suite pages. Nothing in
here touches Flask, so every number the driver sees can be unit-tested.

SINGLE SOURCE OF TRUTH FOR MONEY
--------------------------------
A delivery earns the driver ``round(delivery_fee * DRIVER_SHARE_RATE)`` -
exactly the formula payment_admin.get_driver_wallet_summary() uses for
the admin finance dashboard. ``driver_share()`` below is that same
formula, and tests/test_driver_portal.py asserts the two always agree,
so the driver's Earnings page can never show a different total from the
one the admin sees.

NOTHING IS INVENTED
-------------------
* Ratings: the app has no flow that stores a customer rating yet, so
  every rating figure is ``None`` unless real ``customer_rating`` values
  exist.
* Tips: nothing writes ``tip_amount``; tips are not shown.
* Cancellation dates: there is no ``cancelled_at`` field, so cancellation
  rates are computed over orders by their *scheduled* date instead.
=====================================================
"""

import math
import re
from datetime import date, datetime, timedelta

from payment_admin import DRIVER_SHARE_RATE

ACTIVE_STATUSES = ("Accepted", "On The Way", "Arrived")
LIVE_STATUSES = ("Assigned",) + ACTIVE_STATUSES

# status -> (label shown to the driver, tone used by the UI badge)
STATUS_META = {
    "Assigned": ("Awaiting your response", "warning"),
    "Accepted": ("Accepted", "info"),
    "On The Way": ("On the way", "primary"),
    "Arrived": ("Arrived", "accent"),
    "Delivered": ("Delivered", "success"),
    "Cancelled": ("Cancelled", "danger"),
    "Declined": ("Declined by you", "muted"),
}

WORKFLOW_STEPS = ("Assigned", "Accepted", "On The Way", "Arrived", "Delivered")

TRIP_TABS = ("all", "active", "assigned", "completed", "cancelled", "declined")

BONUS_EVERY = 25
BONUS_AMOUNT = 500


# -----------------------------------------------------
# small value helpers
# -----------------------------------------------------

def booking_ref(booking):
    """Same reference customers and admins see: first 8 characters of the id."""
    return "#" + str(booking.get("_id"))[:8]


def as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def as_number(value, default=0):
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def order_fee(order):
    """Delivery fee, with the same 500 fallback payment_admin uses."""
    return order.get("delivery_fee", 500)


def driver_share(order):
    """What the driver earns for one delivered order (the project's commission rule)."""
    return round(order_fee(order) * DRIVER_SHARE_RATE)


def platform_commission(order):
    return order_fee(order) - driver_share(order)


def litres(order):
    """Water quantity as a whole number of litres (0 if it can't be read)."""
    match = re.search(r"\d+(?:\.\d+)?", str(order.get("quantity", "")).replace(",", ""))
    return int(float(match.group())) if match else 0


def trip_date(order):
    """Date a trip is scheduled for (falls back to when it was booked)."""
    return as_date(order.get("delivery_date")) or as_date(order.get("created_at"))


def delivered_date(order):
    value = order.get("delivered_at")
    return value.date() if isinstance(value, datetime) else None


def inr(value):
    """Indian digit grouping: 1234567 -> 12,34,567"""
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return "0"
    sign = "-" if number < 0 else ""
    digits = str(abs(number))
    if len(digits) <= 3:
        return sign + digits
    head, tail = digits[:-3], digits[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return sign + ",".join(parts) + "," + tail


def cash_status(order):
    """(label, tone) describing where the cash for a delivered order stands."""
    if order.get("payment_status") == "Paid (Online)":
        return "Paid online", "success"
    status = order.get("payment_submission_status")
    if status == "Approved":
        return "Verified by admin", "success"
    if status == "Rejected":
        return "Rejected - resubmit", "danger"
    if status in ("Pending Verification", "Waiting Admin Approval") or order.get("payment_submitted"):
        return "Awaiting admin check", "warning"
    return "Not submitted", "muted"


def needs_cash_submission(order):
    """Delivered, paid in cash, and the driver hasn't reported the transfer yet."""
    return (
        order.get("status") == "Delivered"
        and order.get("payment_status") != "Paid (Online)"
        and not order.get("payment_submitted")
    )


# -----------------------------------------------------
# trips
# -----------------------------------------------------

def trip_tab(order):
    status = order.get("status")
    if status == "Delivered":
        return "completed"
    if status == "Cancelled":
        return "cancelled"
    if status == "Assigned":
        return "assigned"
    if status in ACTIVE_STATUSES:
        return "active"
    return None   # pre-assignment statuses are not the driver's trips


def next_actions(order):
    """Which buttons make sense next. The server re-checks every one of these."""
    status = order.get("status")
    if status == "Assigned":
        return ["accept", "decline"]
    if status == "Accepted":
        return ["start"]
    if status == "On The Way":
        return ["arrived", "complete"]
    if status == "Arrived":
        return ["complete"]
    if status == "Delivered" and needs_cash_submission(order):
        return ["submit_cash"]
    return []


def short_address(address, limit=70):
    text = " ".join(str(address or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def trip_row(order):
    status = order.get("status")
    label, tone = STATUS_META.get(status, (status or "Unknown", "muted"))
    when = trip_date(order)
    return {
        "id": str(order["_id"]),
        "ref": booking_ref(order),
        "status": status,
        "status_label": label,
        "tone": tone,
        "address": short_address(order.get("address")),
        "date": when,
        "time": order.get("delivery_time") or "",
        "litres": litres(order),
        "water_type": order.get("water_type") or "Water",
        "fee": order_fee(order),
        "share": driver_share(order) if status == "Delivered" else None,
        "online": order.get("payment_status") == "Paid (Online)",
        "actions": next_actions(order),
        "tab": trip_tab(order),
    }


def declined_row(order):
    """A trip this driver turned down. Deliberately minimal - no customer data."""
    when = order.get("rejected_at")
    return {
        "id": str(order["_id"]),
        "ref": booking_ref(order),
        "status": "Declined",
        "status_label": STATUS_META["Declined"][0],
        "tone": "muted",
        "address": "",
        "date": when.date() if isinstance(when, datetime) else trip_date(order),
        "time": "",
        "litres": litres(order),
        "water_type": order.get("water_type") or "Water",
        "fee": order_fee(order),
        "share": None,
        "online": False,
        "actions": [],
        "tab": "declined",
    }


def _sort_key(row):
    return (row["date"] or date.min, row["time"] or "", row["id"])


def list_trips(owned_orders, declined_orders, tab="all", query="", date_from=None,
               date_to=None, sort="newest", page=1, per_page=10):
    """
    Filter / sort / paginate a driver's own trips. ``owned_orders`` and
    ``declined_orders`` are already scoped to the driver by the caller's
    database query - nothing here can reach another driver's bookings.
    """
    tab = tab if tab in TRIP_TABS else "all"
    rows = [trip_row(o) for o in owned_orders if trip_tab(o)]
    rows += [declined_row(o) for o in declined_orders]

    counts = {name: 0 for name in TRIP_TABS}
    for row in rows:
        counts["all"] += 1 if row["tab"] != "declined" else 0
        counts[row["tab"]] += 1

    if tab == "all":
        rows = [r for r in rows if r["tab"] != "declined"]
    else:
        rows = [r for r in rows if r["tab"] == tab]

    query = (query or "").strip().lower().lstrip("#")
    if query:
        rows = [r for r in rows if query in r["id"].lower()]
    if date_from:
        rows = [r for r in rows if r["date"] and r["date"] >= date_from]
    if date_to:
        rows = [r for r in rows if r["date"] and r["date"] <= date_to]

    if sort == "oldest":
        rows.sort(key=_sort_key)
    elif sort == "litres":
        rows.sort(key=lambda r: (r["litres"], _sort_key(r)), reverse=True)
    elif sort == "status":
        order_index = {s: i for i, s in enumerate(WORKFLOW_STEPS)}
        rows.sort(key=lambda r: (order_index.get(r["status"], 99), _sort_key(r)))
    else:
        rows.sort(key=_sort_key, reverse=True)

    total = len(rows)
    per_page = max(1, per_page)
    pages = max(1, math.ceil(total / per_page))
    page = min(max(1, page), pages)
    start = (page - 1) * per_page
    return {
        "rows": rows[start:start + per_page],
        "total": total,
        "page": page,
        "pages": pages,
        "per_page": per_page,
        "counts": counts,
        "tab": tab,
        "sort": sort if sort in ("newest", "oldest", "litres", "status") else "newest",
    }


def trip_detail(order, driver):
    """Everything the details drawer shows for ONE of the driver's own bookings."""
    status = order.get("status")
    label, tone = STATUS_META.get(status, (status or "Unknown", "muted"))
    in_progress = status in ACTIVE_STATUSES
    paid_online = order.get("payment_status") == "Paid (Online)"

    timeline = [
        ("Booked", order.get("created_at")),
        ("Assigned to you", order.get("driver_assigned_at")),
        ("Accepted", order.get("driver_accept_time")),
        ("Delivery started", order.get("started_at")),
        ("Arrived", order.get("arrived_at")),
        ("Delivered", order.get("delivered_at")),
    ]
    detail = {
        "id": str(order["_id"]),
        "ref": booking_ref(order),
        "status": status,
        "status_label": label,
        "tone": tone,
        "address": order.get("address") or "",
        "has_coordinates": order.get("latitude") is not None and order.get("longitude") is not None,
        "litres": litres(order),
        "water_type": order.get("water_type") or "Water",
        "date": trip_date(order),
        "time": order.get("delivery_time") or "",
        "fee": order_fee(order),
        "share": driver_share(order),
        "commission": platform_commission(order),
        "payment_method": order.get("payment_method") or "Cash on Delivery",
        "paid_online": paid_online,
        "collect_cash": (not paid_online) and status != "Delivered" and status != "Cancelled",
        "vehicle": (driver or {}).get("truck_number") or "",
        # Customer contact only while the delivery is actually in progress.
        "customer_name": order.get("fullname") if in_progress else None,
        "customer_phone": order.get("phone") if in_progress else None,
        "timeline": [
            {"label": name, "at": at.strftime("%d %b %Y, %I:%M %p") if isinstance(at, datetime) else None}
            for name, at in timeline
        ],
        "actions": next_actions(order),
    }
    if status == "Delivered":
        detail["cash_label"], detail["cash_tone"] = cash_status(order)
    return detail


# -----------------------------------------------------
# aggregates (dashboard, profile, analytics)
# -----------------------------------------------------

def average_delivery_minutes(orders):
    """Mean minutes from 'On The Way' to 'Delivered', or (None, 0) with no usable timestamps."""
    minutes = []
    for order in orders:
        started, finished = order.get("started_at"), order.get("delivered_at")
        if isinstance(started, datetime) and isinstance(finished, datetime) and finished >= started:
            minutes.append((finished - started).total_seconds() / 60.0)
    if not minutes:
        return None, 0
    return round(sum(minutes) / len(minutes)), len(minutes)


def average_rating(orders):
    ratings = [as_number(o.get("customer_rating"), None) for o in orders if o.get("customer_rating")]
    ratings = [r for r in ratings if r]
    if not ratings:
        return None, 0
    return round(sum(ratings) / len(ratings), 1), len(ratings)


def completion_rate(orders):
    """Delivered / (delivered + cancelled), or None when nothing has finished yet."""
    delivered = sum(1 for o in orders if o.get("status") == "Delivered")
    cancelled = sum(1 for o in orders if o.get("status") == "Cancelled")
    finished = delivered + cancelled
    return round(100.0 * delivered / finished) if finished else None


def performance_summary(orders):
    delivered = [o for o in orders if o.get("status") == "Delivered"]
    minutes, minutes_n = average_delivery_minutes(delivered)
    rating, rating_n = average_rating(delivered)
    return {
        "total_trips": sum(1 for o in orders if trip_tab(o)),
        "completed": len(delivered),
        "active": sum(1 for o in orders if o.get("status") in ACTIVE_STATUSES),
        "awaiting_response": sum(1 for o in orders if o.get("status") == "Assigned"),
        "cancelled": sum(1 for o in orders if o.get("status") == "Cancelled"),
        "litres_delivered": sum(litres(o) for o in delivered),
        "completion_rate": completion_rate(orders),
        "avg_delivery_minutes": minutes,
        "avg_delivery_samples": minutes_n,
        "rating": rating,
        "rating_count": rating_n,
    }


def milestone_bonus(completed_count):
    """The project's existing 'every 25 deliveries' milestone, as progress only."""
    completed_count = max(0, int(completed_count or 0))
    reached = completed_count // BONUS_EVERY
    into = completed_count % BONUS_EVERY
    return {
        "every": BONUS_EVERY,
        "amount": BONUS_AMOUNT,
        "reached": reached,
        "milestone_value": reached * BONUS_AMOUNT,
        "remaining": BONUS_EVERY - into,
        "progress_pct": round(100 * into / BONUS_EVERY),
    }


# -----------------------------------------------------
# time series
# -----------------------------------------------------

def week_start(day):
    return day - timedelta(days=day.weekday())


def bucket_start(day, bucket):
    if bucket == "week":
        return week_start(day)
    if bucket == "month":
        return day.replace(day=1)
    return day


def next_bucket(day, bucket):
    if bucket == "week":
        return day + timedelta(days=7)
    if bucket == "month":
        return (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return day + timedelta(days=1)


def bucket_label(day, bucket):
    if bucket == "month":
        return day.strftime("%b %Y")
    if bucket == "week":
        return "Wk of " + day.strftime("%d %b")
    return day.strftime("%d %b")


def choose_bucket(start, end):
    days = (end - start).days + 1
    if days <= 31:
        return "day"
    if days <= 120:
        return "week"
    return "month"


def build_series(orders, start, end, bucket="day"):
    """
    One point per bucket between ``start`` and ``end`` inclusive, from REAL
    delivered orders (dated by delivered_at, like the wallet summary). Buckets
    with no activity are zero - never filled with sample values.
    """
    points = {}
    cursor = bucket_start(start, bucket)
    while cursor <= end:
        points[cursor] = {"start": cursor, "label": bucket_label(cursor, bucket),
                          "deliveries": 0, "litres": 0, "earnings": 0}
        cursor = next_bucket(cursor, bucket)

    for order in orders:
        if order.get("status") != "Delivered":
            continue
        day = delivered_date(order)
        if not day or day < start or day > end:
            continue
        point = points.get(bucket_start(day, bucket))
        if point is None:
            continue
        point["deliveries"] += 1
        point["litres"] += litres(order)
        point["earnings"] += driver_share(order)
    return list(points.values())


def orders_scheduled_between(orders, start, end):
    """Orders by scheduled date - the only reliable date for non-delivered outcomes."""
    result = []
    for order in orders:
        when = trip_date(order)
        if when and start <= when <= end:
            result.append(order)
    return result


def analytics_payload(orders, start, end, bucket=None):
    bucket = bucket if bucket in ("day", "week", "month") else choose_bucket(start, end)
    series = build_series(orders, start, end, bucket)

    delivered_in_range = [
        o for o in orders
        if o.get("status") == "Delivered" and delivered_date(o) and start <= delivered_date(o) <= end
    ]
    scheduled = orders_scheduled_between(orders, start, end)
    cancelled = sum(1 for o in scheduled if o.get("status") == "Cancelled")
    finished_delivered = sum(1 for o in scheduled if o.get("status") == "Delivered")
    finished = cancelled + finished_delivered

    span = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=span - 1)
    prev_delivered = [
        o for o in orders
        if o.get("status") == "Delivered" and delivered_date(o) and prev_start <= delivered_date(o) <= prev_end
    ]

    deliveries = len(delivered_in_range)
    earnings = sum(driver_share(o) for o in delivered_in_range)
    minutes, minutes_n = average_delivery_minutes(delivered_in_range)
    rating, rating_n = average_rating(delivered_in_range)

    def delta(current, previous):
        # Only a trustworthy comparison: needs real activity in the previous period.
        if previous <= 0:
            return None
        return round(100.0 * (current - previous) / previous)

    rating_series = []
    if rating_n:
        grouped = {}
        for order in delivered_in_range:
            if order.get("customer_rating"):
                grouped.setdefault(bucket_start(delivered_date(order), bucket), []).append(
                    as_number(order.get("customer_rating"), 0))
        rating_series = [
            {"label": bucket_label(day, bucket), "rating": round(sum(vals) / len(vals), 2)}
            for day, vals in sorted(grouped.items())
        ]

    return {
        "range": {"from": start.isoformat(), "to": end.isoformat(), "bucket": bucket, "days": span},
        "kpis": {
            "deliveries": deliveries,
            "litres": sum(litres(o) for o in delivered_in_range),
            "earnings": earnings,
            "deliveries_delta": delta(deliveries, len(prev_delivered)),
            "earnings_delta": delta(earnings, sum(driver_share(o) for o in prev_delivered)),
            "completion_rate": round(100.0 * finished_delivered / finished) if finished else None,
            "cancelled": cancelled,
            "cancellation_rate": round(100.0 * cancelled / finished) if finished else None,
            "avg_delivery_minutes": minutes,
            "avg_delivery_samples": minutes_n,
            "rating": rating,
            "rating_count": rating_n,
        },
        "series": [
            {"label": p["label"], "start": p["start"].isoformat(), "deliveries": p["deliveries"],
             "litres": p["litres"], "earnings": p["earnings"]}
            for p in series
        ],
        "ratings": rating_series,
        "empty": deliveries == 0 and cancelled == 0,
    }


# -----------------------------------------------------
# profile
# -----------------------------------------------------

PHONE_RE = re.compile(r"^\+?\d{10,13}$")
LICENSE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 \-/]{4,24}$")


def normalize_phone(raw):
    cleaned = re.sub(r"[\s\-().]", "", str(raw or ""))
    return cleaned if PHONE_RE.match(cleaned) else None


def validate_profile(form, existing_phone=None):
    """Validate the fields a driver may edit. Returns (clean_values, field_errors)."""
    errors, clean = {}, {}

    phone = normalize_phone(form.get("phone"))
    if not phone:
        errors["phone"] = "Enter a valid phone number (10-13 digits)."
    else:
        clean["phone"] = phone

    address = " ".join(str(form.get("address") or "").split())
    if len(address) < 10:
        errors["address"] = "Enter your full address (at least 10 characters)."
    elif len(address) > 250:
        errors["address"] = "Keep the address under 250 characters."
    else:
        clean["address"] = address

    emergency_raw = str(form.get("emergency_contact") or "").strip()
    if emergency_raw:
        emergency = normalize_phone(emergency_raw)
        if not emergency:
            errors["emergency_contact"] = "Enter a valid phone number (10-13 digits)."
        elif emergency == phone:
            errors["emergency_contact"] = "Use a different number from your own."
        else:
            clean["emergency_contact"] = emergency
    else:
        clean["emergency_contact"] = ""

    experience_raw = str(form.get("experience_years") if form.get("experience_years") is not None else "").strip()
    if experience_raw == "":
        clean["experience_years"] = None
    else:
        try:
            years = int(experience_raw)
            if not 0 <= years <= 60:
                raise ValueError
            clean["experience_years"] = years
        except ValueError:
            errors["experience_years"] = "Enter whole years between 0 and 60."

    license_raw = " ".join(str(form.get("license_number") or "").split())
    if license_raw:
        if not LICENSE_RE.match(license_raw):
            errors["license_number"] = "Use 5-25 letters, numbers, spaces or dashes."
        else:
            clean["license_number"] = license_raw.upper()
    else:
        clean["license_number"] = ""

    return clean, errors


def validate_new_password(current, new, confirm):
    errors = {}
    if not current:
        errors["current_password"] = "Enter your current password."
    if len(new or "") < 8:
        errors["new_password"] = "Use at least 8 characters."
    elif not re.search(r"[A-Za-z]", new) or not re.search(r"\d", new):
        errors["new_password"] = "Include at least one letter and one number."
    elif new == current:
        errors["new_password"] = "Choose a password you haven't used here before."
    if new != confirm:
        errors["confirm_password"] = "The two passwords don't match."
    return errors


def profile_completeness(driver, documents_overview, bank_details):
    """Percent complete plus the checklist, driven only by what is really on file."""
    items = [
        ("Profile photo", bool(driver.get("photo")), "driver_profile"),
        ("Phone number", bool(driver.get("phone")), "driver_profile"),
        ("Home address", bool(driver.get("address")), "driver_profile"),
        ("Emergency contact", bool(driver.get("emergency_contact")), "driver_profile"),
        ("Years of experience", driver.get("experience_years") not in (None, ""), "driver_profile"),
        ("Licence number", bool(driver.get("license_number")), "driver_profile"),
        ("Vehicle details", bool(driver.get("truck_number") and driver.get("truck_capacity")), "driver_support"),
        ("Required documents uploaded", bool(documents_overview and documents_overview["all_required_uploaded"]), "driver_documents"),
        ("Bank details for payouts", bool(bank_details), "driver_earnings"),
    ]
    done = sum(1 for _, ok, _ in items if ok)
    return {
        "percent": round(100 * done / len(items)),
        "items": [{"label": label, "done": ok, "endpoint": endpoint} for label, ok, endpoint in items],
        "missing": [label for label, ok, _ in items if not ok],
    }


# -----------------------------------------------------
# notifications
# -----------------------------------------------------

NOTIFICATION_FILTERS = ("all", "unread", "deliveries", "payments", "documents", "account", "support")


def notification_category(note):
    kind = str(note.get("type") or "").lower()
    if kind.startswith("document"):
        return "documents"
    if kind.startswith(("settlement", "payment", "withdraw", "cod", "cash")):
        return "payments"
    if kind.startswith(("support", "emergency")):
        return "support"
    if kind.startswith(("bank", "account", "approval", "profile", "password")):
        return "account"
    return "deliveries"


NOTIFICATION_ICONS = {
    "deliveries": "fa-truck-fast",
    "payments": "fa-wallet",
    "documents": "fa-folder-open",
    "account": "fa-user-shield",
    "support": "fa-life-ring",
}


def notification_target(note):
    """Endpoint name a notification should link to (None = no useful link)."""
    category = notification_category(note)
    return {
        "deliveries": "driver_trips",
        "payments": "driver_earnings",
        "documents": "driver_documents",
        "account": "driver_profile",
        "support": "driver_support",
    }.get(category)


def time_ago(moment, now=None):
    if not isinstance(moment, datetime):
        return ""
    now = now or datetime.now()
    seconds = max(0, int((now - moment).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    if seconds < 86400 * 7:
        return f"{seconds // 86400} d ago"
    return moment.strftime("%d %b %Y")
