"""
dashboard_stats.py
=====================================================
Everything the customer dashboard shows is calculated HERE, from the
logged-in customer's real bookings - nothing is hardcoded or random.

COUNTING RULES (one place, so the page and the JSON endpoint can never
disagree):

  * A booking only "counts" if it is a real booking:
      - "Awaiting Payment" is an unpaid Pay-Now attempt  -> never counted
      - "Cancelled" + auto_cancelled_reason (abandoned Pay-Now) -> never counted
  * Total Bookings   = every counted booking, any status (incl. user-cancelled)
  * Pending          = status "Pending"
  * Active           = status Assigned / Accepted / Arrived / On The Way
  * Delivered        = status "Delivered"
  * Cancelled        = status "Cancelled" (user / admin cancelled)
  * Total Liters     = liters on bookings that are NOT cancelled
                       (pending + active + delivered); "liters_delivered"
                       is the delivered part of that.
  * Today's Deliveries = bookings whose delivery_date is today (in the
                       app timezone) and that are still scheduled
                       (Pending / Active). Already-delivered ones are
                       reported separately as "delivered_today".

One query per dashboard load, with a projection, grouped in Python - no
per-booking follow-up queries.
=====================================================
"""

import math
import os
from datetime import datetime, timedelta

try:  # zoneinfo ships with Python 3.9+; tzdata may be missing on Windows
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

PENDING = "Pending"
ACTIVE_STATUSES = ("Assigned", "Accepted", "Arrived", "On The Way")
DELIVERED = "Delivered"
# Real workflow statuses between admin approval and a driver being assigned. They are shown as the
# upcoming delivery but are not added to the Pending / Active metric buckets (those definitions are unchanged).
AWAITING_DISPATCH = ("Approved", "Waiting Driver Approval")
CANCELLED = "Cancelled"
AWAITING_PAYMENT = "Awaiting Payment"

FEED_LIMIT = 10
CHART_MAX_POINTS = 12
TABLE_LIMIT = 100          # newest bookings sent to the Recent Bookings table
WEEKS_SHOWN = 12           # weekly booking-activity bars (page can show 4/8/12)

# water-quality plausibility limits (reject garbage, not judge safety)
WQ_LIMITS = {"ph": (0.0, 14.0), "tds": (0.0, 5000.0), "turbidity": (0.0, 1000.0)}
WQ_MAX_AGE_HOURS = float(os.environ.get("WATER_QUALITY_MAX_AGE_HOURS", "24") or 24)

_PROJECTION = {
    "fullname": 1, "quantity": 1, "water_type": 1, "delivery_date": 1,
    "delivery_time": 1, "status": 1, "created_at": 1, "driver_name": 1,
    "truck_number": 1, "auto_cancelled_reason": 1, "approved_at": 1,
    "driver_assigned_at": 1, "started_at": 1, "delivered_at": 1,
    "address": 1, "invoice_number": 1,
}


# ---------------------------------------------------------------- time
def app_now():
    """Current time in the app's timezone (naive).

    APP_TIMEZONE (e.g. "Asia/Kolkata") is optional. When unset we use
    the server's local clock - the same clock the booking form uses to
    reject past dates - so "today" always means the same thing.
    """
    name = (os.environ.get("APP_TIMEZONE") or "").strip()
    if name and ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(name)).replace(tzinfo=None)
        except Exception:
            pass
    return datetime.now()


def _qty(value):
    try:
        n = int(float(str(value).strip().replace(",", "")))
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _parse_date(value):
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _counts(b):
    status = b.get("status")
    if status == AWAITING_PAYMENT:
        return False
    if status == CANCELLED and b.get("auto_cancelled_reason"):
        return False
    return True


def booking_ref(doc):
    """Short, stable reference shown to the customer.

    Uses the real invoice number when one has been issued, otherwise the
    last six characters of the booking's own database id (e.g. AF-3FA2C1)
    - derived from the record, never invented or sequential-looking."""
    inv = str(doc.get("invoice_number") or "").strip()
    if inv:
        return inv
    return "AF-" + str(doc["_id"])[-6:].upper()


def _week_start(day):
    return day - timedelta(days=day.weekday())


def _iso(dt):
    return dt.isoformat() if isinstance(dt, datetime) else None


# ------------------------------------------------------------ bookings
def build_booking_summary(bookings_collection, user_email, now=None):
    now = now or app_now()
    today = now.date()

    docs = list(bookings_collection.find({"user_email": user_email}, _PROJECTION))
    docs = [d for d in docs if _counts(d)]

    stats = {
        "total_bookings": len(docs), "pending": 0, "active": 0,
        "delivered": 0, "cancelled": 0, "total_liters": 0,
        "liters_delivered": 0, "today_deliveries": 0, "delivered_today": 0,
    }
    chart = {}
    weekly = {}
    upcoming = None
    alerts = []
    delayed = 0

    for d in docs:
        status = d.get("status")
        qty = _qty(d.get("quantity"))
        ddate = _parse_date(d.get("delivery_date"))

        if status == PENDING:
            stats["pending"] += 1
        elif status in ACTIVE_STATUSES:
            stats["active"] += 1
        elif status == DELIVERED:
            stats["delivered"] += 1
        elif status == CANCELLED:
            stats["cancelled"] += 1

        created = d.get("created_at")
        if isinstance(created, datetime):
            wk = _week_start(created.date())
            weekly[wk] = weekly.get(wk, 0) + 1

        if status != CANCELLED:
            stats["total_liters"] += qty
        if status == DELIVERED:
            stats["liters_delivered"] += qty
            done_at = d.get("delivered_at")
            key = ddate or (done_at.date() if isinstance(done_at, datetime) else None)
            if key:
                chart[key] = chart.get(key, 0) + qty

        scheduled = status == PENDING or status in ACTIVE_STATUSES
        if ddate == today:
            if scheduled:
                stats["today_deliveries"] += 1
            elif status == DELIVERED:
                stats["delivered_today"] += 1
        if scheduled and ddate and ddate < today:
            delayed += 1
        if (scheduled or status in AWAITING_DISPATCH) and ddate and ddate >= today:
            sort_key = (ddate, str(d.get("delivery_time") or ""))
            if upcoming is None or sort_key < upcoming[0]:
                upcoming = (sort_key, d)

    # ---- activity feed: newest first, one entry per booking -------------
    def _recent_key(d):
        return d.get("created_at") if isinstance(d.get("created_at"), datetime) else datetime.min

    docs.sort(key=_recent_key, reverse=True)
    feed = [{
        "id": str(d["_id"]),
        "name": d.get("fullname") or "",
        "quantity": _qty(d.get("quantity")),
        "water_type": d.get("water_type") or "",
        "delivery_date": d.get("delivery_date") or "",
        "status": d.get("status") or "",
        "created_at": _iso(d.get("created_at")),
    } for d in docs[:FEED_LIMIT]]

    # ---- Recent Bookings table rows (newest first; page filters/paginates)
    bookings = [{
        "id": str(d["_id"]),
        "ref": booking_ref(d),
        "water_type": d.get("water_type") or "",
        "quantity": _qty(d.get("quantity")),
        "delivery_date": d.get("delivery_date") or "",
        "delivery_time": d.get("delivery_time") or "",
        "status": d.get("status") or "",
        "created_at": _iso(d.get("created_at")),
    } for d in docs[:TABLE_LIMIT]]

    # ---- weekly booking activity: every week in range, zero if none -----
    this_week = _week_start(today)
    weeks = [this_week - timedelta(weeks=i) for i in range(WEEKS_SHOWN - 1, -1, -1)]
    activity_chart = {
        "labels": [w.isoformat() for w in weeks],
        "values": [weekly.get(w, 0) for w in weeks],
    }

    # ---- upcoming delivery + genuinely-recorded milestones ---------------
    upcoming_out = None
    if upcoming:
        d = upcoming[1]
        steps = [
            ("Booked", d.get("created_at")),
            ("Approved", d.get("approved_at")),
            ("Driver assigned", d.get("driver_assigned_at")),
            ("On the way", d.get("started_at")),
            ("Delivered", d.get("delivered_at")),
        ]
        upcoming_out = {
            "id": str(d["_id"]),
            "delivery_date": d.get("delivery_date") or "",
            "delivery_time": d.get("delivery_time") or "",
            "quantity": _qty(d.get("quantity")),
            "water_type": d.get("water_type") or "",
            "status": d.get("status") or "",
            "ref": booking_ref(d),
            "address": str(d.get("address") or "").strip(),
            "driver_name": d.get("driver_name") if d.get("driver_name") not in (None, "", "Not Assigned") else "",
            "truck_number": d.get("truck_number") if d.get("truck_number") not in (None, "", "Not Assigned") else "",
            "timeline": [{"label": label, "done": isinstance(ts, datetime), "at": _iso(ts)}
                         for label, ts in steps],
        }

    if stats["today_deliveries"]:
        n = stats["today_deliveries"]
        alerts.append({"level": "info", "text": f"{n} delivery{'s' if n != 1 else ''} scheduled for today."})
    if stats["active"]:
        n = stats["active"]
        alerts.append({"level": "info", "text": f"{n} order{'s' if n != 1 else ''} currently in progress with a driver."})
    if delayed:
        alerts.append({"level": "warn", "text": f"{delayed} booking{'s are' if delayed != 1 else ' is'} past the scheduled date and not yet delivered. Please contact support."})

    points = sorted(chart.items())[-CHART_MAX_POINTS:]
    return {
        "stats": stats,
        "activity": feed,
        "activity_total": len(docs),
        "bookings": bookings,
        "bookings_chart": activity_chart,
        "upcoming": upcoming_out,
        "alerts": alerts,
        "chart": {"labels": [k.isoformat() for k, _ in points], "values": [v for _, v in points]},
    }


# -------------------------------------------------------- water quality
def _valid_reading(doc):
    """Return {'ph','tds','turbidity'} floats if every value is a finite
    number inside plausible limits, else None."""
    out = {}
    for key, (lo, hi) in WQ_LIMITS.items():
        v = doc.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        if math.isnan(v) or math.isinf(v) or not (lo <= v <= hi):
            return None
        out[key] = float(v)
    return out


def build_water_quality(readings_collection, now=None):
    """
    AquaFlow has no built-in sensors. If an operator (or a sensor
    gateway) writes documents into the `water_quality_readings`
    collection:
        {ph, tds, turbidity, measured_at (datetime), source (str)}
    the NEWEST one is shown - but only if it is valid and fresh.
    Otherwise we show clearly-labelled reference ranges, never numbers
    that look like a live measurement.
    """
    now = now or app_now()
    reference = {
        "ph": "6.5 – 8.5", "tds": "up to 500 mg/L", "turbidity": "up to 1 NTU",
        "note": "Acceptable-limit ranges from Indian drinking-water standard BIS IS 10500. "
                "Reference only - not a measurement of your delivery.",
    }
    base = {"reference": reference, "max_age_hours": WQ_MAX_AGE_HOURS,
            "disclaimer": "These three values alone do not show that water is safe to drink."}

    doc = readings_collection.find_one(sort=[("measured_at", -1)]) if readings_collection is not None else None
    if not doc:
        return {**base, "state": "none", "message": "No sensor is connected. Showing reference ranges only."}

    measured_at = doc.get("measured_at")
    values = _valid_reading(doc)
    if values is None or not isinstance(measured_at, datetime):
        return {**base, "state": "none", "message": "No recent measurement available."}

    age = now - measured_at
    if age > timedelta(hours=WQ_MAX_AGE_HOURS) or age < timedelta(minutes=-5):
        return {**base, "state": "stale", "message": "No recent measurement available.",
                "measured_at": _iso(measured_at), "source": str(doc.get("source") or "")[:80]}

    return {**base, "state": "live", "values": values, "measured_at": _iso(measured_at),
            "source": str(doc.get("source") or "Unknown sensor")[:80]}


def build_dashboard(bookings_collection, readings_collection, user_email, now=None):
    now = now or app_now()
    data = build_booking_summary(bookings_collection, user_email, now)
    data["water_quality"] = build_water_quality(readings_collection, now)
    data["server_time"] = now.isoformat(timespec="seconds")
    data["today"] = now.date().isoformat()
    return data
