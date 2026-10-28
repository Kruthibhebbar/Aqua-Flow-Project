"""
coupon_management.py
=====================

Feature 4 - Coupon System.

Admin side:
    - Create coupons (e.g. WELCOME100, SAVE50, SUMMER10) as either a
      flat rupee discount or a percentage discount.
    - Optionally mark a coupon "one use per customer, ever" - for
      welcome-style offers like WELCOME100. Once a customer redeems
      it on any booking, they can never apply it again, even after
      removing it or years later on a different booking - this is
      tracked permanently in coupon_redemptions_collection, not on
      the booking itself (which can be edited/cancelled).
    - Optional extras (all backward compatible - coupons created before
      they existed simply don't have the fields and behave as before):
        starts_at     scheduled start (not redeemable before it)
        usage_limit   total redemptions allowed across all customers
        min_amount    minimum booking amount required
        max_discount  cap for percentage coupons
    - Activate / disable / edit / delete (only if never redeemed).
    - Search, filter, sort, details + redemption history, CSV export.

Customer side:
    - Can be applied from the booking page itself (at booking-creation
      time, see app.py's /calculate-price and /booking routes) or from
      the payment page (Feature 3) for bookings that don't have one yet.
    - Remove an applied coupon to go back to the original amount - this
      also frees up a one-time coupon so it can be re-applied (removing
      isn't the same as spending it).

The original pre-coupon amount is always kept in "pre_coupon_amount"
so re-applying a different code (or applying twice) never compounds
discounts on top of a previous discount.

STATUS DEFINITIONS (single source of truth: coupon_status()):
    disabled   admin switched it off (active == False)            - wins over everything
    expired    expiry_at is in the past
    exhausted  usage_limit reached (counting only redemptions whose booking
               was not Cancelled/Rejected)
    scheduled  starts_at is in the future
    active     none of the above - customers can redeem it now
Each coupon is in exactly ONE status, so the dashboard counts always add up.
All times are naive server-local datetimes (same convention as the rest
of AquaFlow: datetime.now()).
"""

import csv
import io
import math
import re
from datetime import datetime

from bson.objectid import ObjectId
from flask import (Response, flash, jsonify, redirect, render_template,
                   request, session, url_for)

# ---------------------------------------------------------------------
# CONSTANTS
# ---------------------------------------------------------------------

CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,19}$")        # 3-20 chars after normalisation
STATUSES = ("active", "scheduled", "expired", "disabled", "exhausted")
STATUS_LABELS = {
    "active": "Active", "scheduled": "Scheduled", "expired": "Expired",
    "disabled": "Disabled", "exhausted": "Limit reached",
}
SORTS = ("newest", "oldest", "discount_high", "discount_low", "expiry_soon", "expiry_late")
NON_COUNTING_BOOKING_STATUSES = ("Cancelled", "Rejected")
SORT_REFERENCE_AMOUNT = 1000      # "highest discount" is judged on a Rs.1,000 booking
MAX_FLAT_VALUE = 1_000_000
DT_INPUT_FORMATS = ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S")
DT_LABEL = "%d %b %Y, %I:%M %p"


# ---------------------------------------------------------------------
# SMALL HELPERS
# ---------------------------------------------------------------------

def normalize_code(raw):
    """Trim + upper-case. Codes are case-insensitive everywhere."""
    return (raw or "").strip().upper()


def find_coupon(coupons_collection, code, active_only=False):
    """
    Look a coupon up by its (normalised) code. Exact match first; then a
    case-insensitive match so coupons created by older versions of AquaFlow
    with a lower/mixed-case code (e.g. "Welcome100") still work.
    """
    query = {"code": code}
    if active_only:
        query["active"] = True
    doc = coupons_collection.find_one(query)
    if doc or not code:
        return doc
    query["code"] = {"$regex": f"^{re.escape(code)}$", "$options": "i"}
    return coupons_collection.find_one(query)


def _fmt_money(x):
    return f"₹{x:,.0f}" if float(x).is_integer() else f"₹{x:,.2f}"


def _parse_dt(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in DT_INPUT_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError("invalid datetime")


def _num(raw, name, *, integer=False, allow_blank=True):
    """float/int parser that rejects NaN/inf/garbage. Blank -> None."""
    if raw is None or str(raw).strip() == "":
        if allow_blank:
            return None
        raise ValueError(f"{name} is required")
    try:
        v = float(str(raw).strip())
    except ValueError:
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(v):
        raise ValueError(f"{name} must be a finite number")
    if integer:
        if v != int(v):
            raise ValueError(f"{name} must be a whole number")
        return int(v)
    return v


def mask_email(email):
    """a***z@gmail.com - enough to recognise a customer without exposing the address."""
    if not email or "@" not in email:
        return "unknown"
    local, _, domain = email.partition("@")
    return (local[:1] + "***" + (local[-1:] if len(local) > 2 else "")) + "@" + domain


def _csv_safe(v):
    """Neutralise spreadsheet formula injection."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


# ---------------------------------------------------------------------
# STATUS + REDEMPTION ANALYTICS (shared by admin page, API and validation)
# ---------------------------------------------------------------------

def coupon_status(coupon, effective_redemptions=0, now=None):
    now = now or datetime.now()
    if not coupon.get("active", True):
        return "disabled"
    expiry_at = coupon.get("expiry_at")
    if expiry_at and expiry_at < now:
        return "expired"
    limit = coupon.get("usage_limit")
    if limit and effective_redemptions >= limit:
        return "exhausted"
    starts_at = coupon.get("starts_at")
    if starts_at and starts_at > now:
        return "scheduled"
    return "active"


def build_redemption_index(redemptions, bookings, code=None):
    """
    {code: {"count", "discount", "last", "rows": [...]}} built from the REAL
    redemption records, joined to their bookings.

    count     redemptions whose booking is NOT Cancelled/Rejected (this is what
              counts against usage_limit); records whose booking no longer
              exists are counted conservatively.
    discount  sum of discounts on those counted redemptions, only where the
              amount is actually known (stored on the redemption or on the
              booking's coupon snapshot) - never estimated.
    """
    index = {}
    if redemptions is None:
        return index
    query = {"code": code} if code else {}
    docs = list(redemptions.find(query))
    booking_ids = []
    for d in docs:
        try:
            booking_ids.append(ObjectId(d.get("booking_id")))
        except Exception:
            pass
    by_id = {}
    if bookings is not None and booking_ids:
        for b in bookings.find({"_id": {"$in": booking_ids}},
                               {"status": 1, "coupon": 1, "pre_coupon_amount": 1}):
            by_id[str(b["_id"])] = b

    for d in docs:
        c = d.get("code")
        entry = index.setdefault(c, {"count": 0, "discount": 0.0, "last": None, "rows": []})
        b = by_id.get(str(d.get("booking_id")))
        b_status = b.get("status") if b else None
        counts = b_status not in NON_COUNTING_BOOKING_STATUSES

        discount = d.get("discount")
        if discount is None and b and (b.get("coupon") or {}).get("code") == c:
            discount = (b.get("coupon") or {}).get("discount")

        if counts:
            entry["count"] += 1
            if discount is not None and b is not None:
                entry["discount"] += float(discount)
            ra = d.get("redeemed_at")
            if ra and (entry["last"] is None or ra > entry["last"]):
                entry["last"] = ra
        entry["rows"].append({
            "booking_id": str(d.get("booking_id") or ""),
            "booking_ref": str(d.get("booking_id") or "")[-6:].upper(),
            "booking_status": b_status if b else "Booking not found",
            "customer": mask_email(d.get("user_email")),
            "redeemed_at": d.get("redeemed_at"),
            "discount": float(discount) if discount is not None else None,
            "counted": counts,
        })
    for entry in index.values():
        entry["rows"].sort(key=lambda r: r["redeemed_at"] or datetime.min, reverse=True)
        entry["discount"] = round(entry["discount"], 2)
    return index


def sample_discount(coupon, amount):
    """Discount this coupon would give on `amount` (used for validation, preview and sorting)."""
    if coupon["discount_type"] == "percent":
        d = round(amount * coupon["value"] / 100, 2)
        cap = coupon.get("max_discount")
        if cap:
            d = min(d, cap)
    else:
        d = coupon["value"]
    return max(0, min(d, amount))


# ---------------------------------------------------------------------
# VALIDATION USED AT REDEMPTION TIME (booking page + payment page)
# ---------------------------------------------------------------------

def validate_coupon(coupons_collection, coupon_redemptions_collection, code, user_email,
                    base_amount, bookings_collection=None):
    """
    Shared validation used by both the booking page (apply-at-creation)
    and the payment page (apply-after-approval). Returns
    (coupon_doc, discount_amount, error_message) - exactly one of
    coupon_doc or error_message will be None.

    Every rule is enforced HERE, on the server; the browser checks are
    only a convenience.
    """

    code = normalize_code(code)
    if not code:
        return None, 0, "Please enter a coupon code"

    # disabled coupons are reported exactly like unknown ones
    coupon = find_coupon(coupons_collection, code, active_only=True)
    if not coupon:
        return None, 0, "Invalid or inactive coupon code"

    # sanity-check stored rules so corrupt data can never produce a bad discount
    value = coupon.get("value")
    if coupon.get("discount_type") not in ("flat", "percent") or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or value <= 0 \
            or (coupon["discount_type"] == "percent" and value > 100):
        return None, 0, "This coupon is misconfigured and can't be applied"

    now = datetime.now()

    expiry_at = coupon.get("expiry_at")
    if expiry_at and expiry_at < now:
        return None, 0, f"{code} expired on {expiry_at.strftime(DT_LABEL)}"

    starts_at = coupon.get("starts_at")
    if starts_at and starts_at > now:
        return None, 0, f"{code} isn't active yet - it starts on {starts_at.strftime(DT_LABEL)}"

    limit = coupon.get("usage_limit")
    if limit:
        index = build_redemption_index(coupon_redemptions_collection, bookings_collection, code)
        if index.get(code, {}).get("count", 0) >= limit:
            return None, 0, f"{code} has reached its usage limit"

    if coupon.get("one_time_per_user") and coupon_redemptions_collection is not None:
        already_used = coupon_redemptions_collection.find_one({
            "user_email": user_email,
            "code": code
        })
        if already_used:
            return None, 0, f"You've already used {code} before - it's a one-time offer per customer"

    if not isinstance(base_amount, (int, float)) or not math.isfinite(base_amount) or base_amount <= 0:
        return None, 0, "There is no payable amount to apply a coupon to"

    min_amount = coupon.get("min_amount")
    if min_amount and base_amount < min_amount:
        return None, 0, f"{code} needs a booking of at least {_fmt_money(min_amount)}"

    discount = sample_discount(coupon, base_amount)
    if discount <= 0:
        return None, 0, "This coupon gives no discount on this booking"

    return coupon, discount, None


def record_redemption(coupon_redemptions_collection, user_email, code, booking_id,
                      discount=None, coupons_collection=None, bookings_collection=None):
    """
    Record a redemption. Returns True if it was recorded, False if the coupon's
    usage limit / one-per-customer rule would have been exceeded.

    Safe under concurrency: the redemption is inserted FIRST and the limits are
    re-checked afterwards; if we went over, our own insert is rolled back. Two
    racing requests can therefore never both squeeze past a limit (in the worst
    case both are refused and the customer simply retries).
    """
    if coupon_redemptions_collection is None:
        return True
    code = normalize_code(code)
    doc = {
        "user_email": user_email,
        "code": code,
        "booking_id": str(booking_id),
        "redeemed_at": datetime.now()
    }
    if discount is not None:
        doc["discount"] = float(discount)
    inserted_id = coupon_redemptions_collection.insert_one(doc).inserted_id

    if coupons_collection is not None:
        coupon = find_coupon(coupons_collection, code)
        over = False
        if coupon:
            limit = coupon.get("usage_limit")
            if limit:
                index = build_redemption_index(coupon_redemptions_collection, bookings_collection, code)
                over = index.get(code, {}).get("count", 0) > limit
            if not over and coupon.get("one_time_per_user"):
                over = coupon_redemptions_collection.count_documents(
                    {"user_email": user_email, "code": code}) > 1
        if over:
            coupon_redemptions_collection.delete_one({"_id": inserted_id})
            return False
    return True


def release_redemption(coupon_redemptions_collection, user_email, code, booking_id):
    """Undo record_redemption - called when a coupon is removed before payment."""
    if coupon_redemptions_collection is None:
        return
    coupon_redemptions_collection.delete_one({
        "user_email": user_email,
        "code": code,
        "booking_id": str(booking_id)
    })


# ---------------------------------------------------------------------
# FIELD VALIDATION FOR ADMIN CREATE / EDIT
# ---------------------------------------------------------------------

def _clean_fields(raw, *, existing=None, redeemed=0, now=None):
    """
    Validate admin input. `raw` maps field name -> string/number.
    On create (existing is None) every field is read; on edit only the keys
    present in `raw` change and code/type/one-time are immutable.
    Returns (clean_dict, error_message).
    """
    now = now or datetime.now()
    editing = existing is not None
    clean = {}
    try:
        dtype = existing["discount_type"] if editing else (raw.get("discount_type") or "flat").strip().lower()
        if dtype not in ("flat", "percent"):
            return None, "Discount type must be flat or percent"

        if not editing or "value" in raw:
            value = _num(raw.get("value"), "Discount value", allow_blank=False)
            if value <= 0:
                return None, "Discount value must be greater than 0"
            if dtype == "percent" and value > 100:
                return None, "A percentage discount cannot exceed 100%"
            if dtype == "flat" and value > MAX_FLAT_VALUE:
                return None, f"A flat discount cannot exceed {_fmt_money(MAX_FLAT_VALUE)}"
            clean["value"] = value

        if not editing or "usage_limit" in raw:
            limit = _num(raw.get("usage_limit"), "Usage limit", integer=True)
            if limit is not None:
                if limit < 1:
                    return None, "Usage limit must be at least 1"
                if limit < redeemed:
                    return None, f"Usage limit can't be lower than the {redeemed} redemption(s) already made"
            clean["usage_limit"] = limit

        if not editing or "min_amount" in raw:
            m = _num(raw.get("min_amount"), "Minimum booking amount")
            if m is not None and m < 0:
                return None, "Minimum booking amount can't be negative"
            clean["min_amount"] = m or None

        if not editing or "max_discount" in raw:
            cap = _num(raw.get("max_discount"), "Maximum discount")
            if cap is not None:
                if dtype != "percent":
                    return None, "Maximum discount only applies to percentage coupons"
                if cap <= 0:
                    return None, "Maximum discount must be greater than 0"
            clean["max_discount"] = cap

        starts_at = existing.get("starts_at") if editing else None
        expiry_at = existing.get("expiry_at") if editing else None
        if not editing or "starts_at" in raw:
            try:
                starts_at = _parse_dt(raw.get("starts_at"))
            except ValueError:
                return None, "Please enter a valid start date and time"
            clean["starts_at"] = starts_at
        if not editing or "expiry_at" in raw:
            try:
                new_expiry = _parse_dt(raw.get("expiry_at"))
            except ValueError:
                return None, "Please enter a valid expiry date and time"
            changed = not editing or new_expiry != existing.get("expiry_at")
            if new_expiry and changed and new_expiry <= now:
                return None, "Coupon expiry must be a future date and time"
            expiry_at = new_expiry
            clean["expiry_at"] = expiry_at
        if starts_at and expiry_at and starts_at >= expiry_at:
            return None, "The start time must be before the expiry time"
    except ValueError as e:
        return None, str(e)
    return clean, None


# ---------------------------------------------------------------------
# MODULE INIT
# ---------------------------------------------------------------------

def init_coupon_management(app, coupons_collection, bookings_collection, coupon_redemptions_collection=None):

    # ---------- shared builders ----------

    def build_views(now=None):
        """Every coupon with its computed status + real redemption analytics."""
        now = now or datetime.now()
        index = build_redemption_index(coupon_redemptions_collection, bookings_collection)
        views = []
        for c in coupons_collection.find():
            code = c.get("code", "")
            info = index.get(normalize_code(code), {"count": 0, "discount": 0.0, "last": None, "rows": []})
            status = coupon_status(c, info["count"], now)
            ctype = c.get("discount_type", "flat")
            value = c.get("value", 0)
            limit = c.get("usage_limit")
            views.append({
                "id": str(c["_id"]),
                "code": code,
                "discount_type": ctype,
                "value": value,
                "discount_label": f"{value:g}% off" if ctype == "percent" else f"{_fmt_money(value)} off",
                "one_time_per_user": bool(c.get("one_time_per_user")),
                "usage_limit": limit,
                "min_amount": c.get("min_amount"),
                "max_discount": c.get("max_discount"),
                "active": bool(c.get("active", True)),
                "status": status,
                "status_label": STATUS_LABELS[status],
                "redemptions": info["count"],
                "discount_given": info["discount"],
                "last_redeemed": info["last"].strftime(DT_LABEL) if info["last"] else None,
                "created_label": c["created_at"].strftime("%d %b %Y") if c.get("created_at") else None,
                "starts_label": c["starts_at"].strftime(DT_LABEL) if c.get("starts_at") else None,
                "starts_input": c["starts_at"].strftime("%Y-%m-%dT%H:%M") if c.get("starts_at") else "",
                "expiry_label": c["expiry_at"].strftime(DT_LABEL) if c.get("expiry_at") else None,
                "expiry_input": c["expiry_at"].strftime("%Y-%m-%dT%H:%M") if c.get("expiry_at") else "",
                # sort keys (not shown)
                "_created": c.get("created_at") or datetime.min,
                "_expiry": c.get("expiry_at"),
                "_sample": sample_discount(
                    {"discount_type": ctype, "value": value if isinstance(value, (int, float)) else 0,
                     "max_discount": c.get("max_discount")}, SORT_REFERENCE_AMOUNT),
            })
        return views

    def compute_stats(views):
        by = {s: 0 for s in STATUSES}
        for v in views:
            by[v["status"]] += 1
        return {
            "total": len(views),
            **by,
            "redemptions": sum(v["redemptions"] for v in views),
            "discount_given": round(sum(v["discount_given"] for v in views), 2),
        }

    def parse_filters(args):
        """Validated filter/sort params shared by the list API and the CSV export."""
        f = {
            "q": normalize_code(args.get("q", ""))[:40],
            "status": (args.get("status") or "").strip().lower(),
            "type": (args.get("type") or "").strip().lower(),
            "sort": (args.get("sort") or "newest").strip().lower(),
            "expiry_from": None, "expiry_to": None,
        }
        if f["status"] and f["status"] not in STATUSES:
            return None, "Unknown status filter"
        if f["type"] and f["type"] not in ("flat", "percent"):
            return None, "Unknown discount type filter"
        if f["sort"] not in SORTS:
            return None, "Unknown sort option"
        for key in ("expiry_from", "expiry_to"):
            raw = (args.get(key) or "").strip()
            if raw:
                try:
                    f[key] = datetime.strptime(raw, "%Y-%m-%d")
                except ValueError:
                    return None, "Expiry dates must look like YYYY-MM-DD"
        if f["expiry_from"] and f["expiry_to"] and f["expiry_from"] > f["expiry_to"]:
            return None, "Expiry 'from' date must not be after the 'to' date"
        return f, None

    def apply_filters(views, f):
        out = []
        for v in views:
            if f["q"] and f["q"] not in v["code"].upper():
                continue
            if f["status"] and v["status"] != f["status"]:
                continue
            if f["type"] and v["discount_type"] != f["type"]:
                continue
            if f["expiry_from"] or f["expiry_to"]:
                e = v["_expiry"]
                if not e:
                    continue
                if f["expiry_from"] and e < f["expiry_from"]:
                    continue
                if f["expiry_to"] and e >= f["expiry_to"].replace(hour=23, minute=59, second=59):
                    if e.date() > f["expiry_to"].date():
                        continue
            out.append(v)
        s = f["sort"]
        far = datetime.max
        if s == "newest":
            out.sort(key=lambda v: v["_created"], reverse=True)
        elif s == "oldest":
            out.sort(key=lambda v: v["_created"])
        elif s == "discount_high":
            out.sort(key=lambda v: v["_sample"], reverse=True)
        elif s == "discount_low":
            out.sort(key=lambda v: v["_sample"])
        elif s == "expiry_soon":
            out.sort(key=lambda v: v["_expiry"] or far)             # no-expiry last
        elif s == "expiry_late":
            out.sort(key=lambda v: (v["_expiry"] is not None, v["_expiry"] or datetime.min), reverse=True)
        return out

    def public(v):
        return {k: val for k, val in v.items() if not k.startswith("_")}

    def wants_json():
        return request.headers.get("X-Requested-With") == "fetch" or \
            request.accept_mimetypes.best == "application/json"

    def respond(ok, message, status=200, back="coupon_management", **extra):
        """JSON for the new page (fetch), flash+redirect for classic form posts.

        `back` is the endpoint a classic form post returns to: creation lands on the
        Create Coupon page, edit/enable/disable/delete land on the All Coupons list.
        """
        if wants_json():
            return jsonify({"success": ok, "message": message, **extra}), status
        flash(message, "success" if ok else "danger")
        return redirect(url_for(back))

    def unauthorized():
        if wants_json() or request.path.startswith("/api/"):
            return jsonify({"success": False, "message": "Unauthorized"}), 401
        return redirect(url_for("admin_login"))

    def get_coupon(coupon_id):
        try:
            return coupons_collection.find_one({"_id": ObjectId(coupon_id)})
        except Exception:
            return None

    # =========================================
    # ADMIN - COUPON MANAGEMENT PAGE
    # =========================================

    @app.route("/coupon-management")
    def coupon_management():
        """Create Coupon page. It deliberately carries NO coupon records - only the
        statistics bar - so the page ends right after the Create New Coupon card."""

        if "admin" not in session:
            return redirect(url_for("admin_login"))

        now = datetime.now()
        return render_template(
            "coupon_management.html",
            page="create",
            initial={"coupons": [], "stats": compute_stats(build_views(now))},
            server_now=now.strftime(DT_LABEL),
            admin_name="Admin",
            admin_email=session.get("admin_email", "admin@aquaflow.com")
        )

    # =========================================
    # ADMIN - ALL COUPONS PAGE (search / filters / actions)
    # =========================================

    @app.route("/admin/coupons/list")
    def coupon_list():

        if "admin" not in session:
            return redirect(url_for("admin_login"))

        now = datetime.now()
        views = build_views(now)
        f, _ = parse_filters({})
        initial = {
            "coupons": [public(v) for v in apply_filters(views, f)],
            "stats": compute_stats(views),
        }
        return render_template(
            "coupon_management.html",
            page="list",
            initial=initial,
            server_now=now.strftime(DT_LABEL),
            admin_name="Admin",
            admin_email=session.get("admin_email", "admin@aquaflow.com")
        )

    # =========================================
    # ADMIN - LIST / SEARCH / FILTER API
    # =========================================

    @app.route("/api/admin/coupons")
    def api_admin_coupons():
        if "admin" not in session:
            return unauthorized()
        f, err = parse_filters(request.args)
        if err:
            return jsonify({"success": False, "message": err}), 400
        views = build_views()
        return jsonify({
            "success": True,
            "coupons": [public(v) for v in apply_filters(views, f)],
            "stats": compute_stats(views),
        })

    # =========================================
    # ADMIN - DETAILS + REDEMPTION HISTORY
    # =========================================

    @app.route("/api/admin/coupons/<coupon_id>")
    def api_admin_coupon_details(coupon_id):
        if "admin" not in session:
            return unauthorized()
        c = get_coupon(coupon_id)
        if not c:
            return jsonify({"success": False, "message": "Coupon not found"}), 404
        view = next((public(v) for v in build_views() if v["id"] == str(c["_id"])), None)
        index = build_redemption_index(coupon_redemptions_collection, bookings_collection, normalize_code(c["code"]))
        rows = index.get(normalize_code(c["code"]), {}).get("rows", [])
        return jsonify({
            "success": True,
            "coupon": view,
            "redemptions": [{
                "booking_ref": r["booking_ref"],
                "booking_status": r["booking_status"],
                "customer": r["customer"],
                "redeemed_at": r["redeemed_at"].strftime(DT_LABEL) if r["redeemed_at"] else None,
                "discount": r["discount"],
                "counted": r["counted"],
            } for r in rows],
        })

    # =========================================
    # ADMIN - CSV EXPORT (respects the current filters)
    # =========================================

    @app.route("/export-coupons")
    def export_coupons():
        if "admin" not in session:
            return redirect(url_for("admin_login"))
        f, err = parse_filters(request.args)
        if err:
            return jsonify({"success": False, "message": err}), 400
        kind = (request.args.get("kind") or "coupons").lower()
        if kind not in ("coupons", "redemptions"):
            return jsonify({"success": False, "message": "kind must be coupons or redemptions"}), 400

        views = apply_filters(build_views(), f)
        buf = io.StringIO()
        w = csv.writer(buf)
        if kind == "coupons":
            w.writerow(["Code", "Type", "Value", "Status", "Usage limit", "Redemptions", "Discount given (INR)",
                        "Min booking amount", "Max discount", "One use per customer", "Starts", "Expires",
                        "Created", "Last redeemed"])
            for v in views:
                w.writerow([_csv_safe(x) for x in [
                    v["code"], v["discount_type"], v["value"], v["status_label"],
                    v["usage_limit"] or "Unlimited", v["redemptions"], v["discount_given"],
                    v["min_amount"] or "", v["max_discount"] or "",
                    "Yes" if v["one_time_per_user"] else "No",
                    v["starts_label"] or "", v["expiry_label"] or "No expiry",
                    v["created_label"] or "", v["last_redeemed"] or ""]])
        else:
            w.writerow(["Code", "Booking ref", "Booking status", "Customer (masked)", "Redeemed at",
                        "Discount (INR)", "Counts toward limit"])
            index = build_redemption_index(coupon_redemptions_collection, bookings_collection)
            for v in views:
                for r in index.get(normalize_code(v["code"]), {}).get("rows", []):
                    w.writerow([_csv_safe(x) for x in [
                        v["code"], r["booking_ref"], r["booking_status"], r["customer"],
                        r["redeemed_at"].strftime("%Y-%m-%d %H:%M:%S") if r["redeemed_at"] else "",
                        "" if r["discount"] is None else r["discount"],
                        "Yes" if r["counted"] else "No"]])

        stamp = datetime.now().strftime("%Y%m%d")
        return Response(
            "﻿" + buf.getvalue(),          # BOM so Excel shows the data correctly
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=aquaflow_{kind}_{stamp}.csv"}
        )

    # =========================================
    # ADMIN - CREATE COUPON
    # =========================================

    @app.route("/create-coupon", methods=["POST"])
    def create_coupon():

        if "admin" not in session:
            return unauthorized()

        try:
            code = normalize_code(request.form.get("code"))
            if not CODE_RE.match(code):
                return respond(False, "Coupon code must be 3-20 characters: letters, numbers, - or _ (e.g. WELCOME100).", 400)

            dtype = (request.form.get("discount_type") or "flat").strip().lower()
            clean, err = _clean_fields(request.form, existing=None)
            if err:
                return respond(False, err, 400)

            # Case-insensitive duplicate protection (WELCOME100 == welcome100 == " Welcome100 ")
            if coupons_collection.find_one({"code": {"$regex": f"^{re.escape(code)}$", "$options": "i"}}):
                return respond(False, f"A coupon with code {code} already exists.", 409)

            doc = {
                "code": code,
                "discount_type": dtype,
                "value": clean["value"],
                "one_time_per_user": request.form.get("one_time_per_user") in ("on", "true", "1"),
                "expiry_at": clean["expiry_at"],
                "active": True,
                "created_at": datetime.now(),
            }
            for optional in ("starts_at", "usage_limit", "min_amount", "max_discount"):
                if clean.get(optional) is not None:
                    doc[optional] = clean[optional]

            # upsert-if-absent keeps the check+insert atomic for the same code
            res = coupons_collection.update_one({"code": code}, {"$setOnInsert": doc}, upsert=True)
            if res.upserted_id is None:
                return respond(False, f"A coupon with code {code} already exists.", 409)

            return respond(True, f"Coupon {code} created successfully.")

        except Exception as e:
            return respond(False, f"Could not create coupon: {str(e)}", 500)

    # =========================================
    # ADMIN - EDIT COUPON (safe fields only)
    # =========================================

    @app.route("/update-coupon/<coupon_id>", methods=["POST"])
    def update_coupon(coupon_id):

        if "admin" not in session:
            return unauthorized()

        c = get_coupon(coupon_id)
        if not c:
            return respond(False, "Coupon not found.", 404, back="coupon_list")

        raw = request.get_json(silent=True)
        if raw is None:
            raw = request.form.to_dict()
        allowed = {"value", "expiry_at", "starts_at", "usage_limit", "min_amount", "max_discount"}
        raw = {k: v for k, v in raw.items() if k in allowed}
        if not raw:
            return respond(False, "Nothing to update.", 400, back="coupon_list")

        index = build_redemption_index(coupon_redemptions_collection, bookings_collection, normalize_code(c["code"]))
        redeemed = index.get(normalize_code(c["code"]), {}).get("count", 0)

        clean, err = _clean_fields(raw, existing=c, redeemed=redeemed)
        if err:
            return respond(False, err, 400, back="coupon_list")

        sets, unsets = {}, {}
        for k, v in clean.items():
            if v is None:
                unsets[k] = ""
            else:
                sets[k] = v
        ops = {}
        if sets:
            ops["$set"] = sets
        if unsets:
            ops["$unset"] = unsets
        if ops:
            coupons_collection.update_one({"_id": c["_id"]}, ops)
        return respond(True, f"Coupon {c['code']} updated.", back="coupon_list")

    # =========================================
    # ADMIN - ENABLE / DISABLE (history is kept)
    # =========================================

    @app.route("/toggle-coupon/<coupon_id>", methods=["POST"])
    def toggle_coupon(coupon_id):

        if "admin" not in session:
            return unauthorized()

        try:
            coupon = get_coupon(coupon_id)
            if not coupon:
                return respond(False, "Coupon not found.", 404, back="coupon_list")

            body = request.get_json(silent=True) or {}
            if isinstance(body.get("active"), bool):
                new_state = body["active"]
            else:
                new_state = not coupon.get("active", True)

            coupons_collection.update_one({"_id": coupon["_id"]}, {"$set": {"active": new_state}})
            if wants_json():
                return jsonify({"success": True, "active": new_state,
                                "message": f"Coupon {coupon['code']} {'enabled' if new_state else 'disabled'}."})
            return redirect(url_for("coupon_list"))

        except Exception as e:
            return respond(False, f"Could not update coupon: {str(e)}", 500, back="coupon_list")

    # =========================================
    # ADMIN - DELETE COUPON (never if it has redemption history)
    # =========================================

    @app.route("/delete-coupon/<coupon_id>", methods=["POST"])
    def delete_coupon(coupon_id):

        if "admin" not in session:
            return unauthorized()

        try:
            c = get_coupon(coupon_id)
            if not c:
                return respond(False, "Coupon not found.", 404, back="coupon_list")
            if coupon_redemptions_collection is not None and \
                    coupon_redemptions_collection.find_one({"code": normalize_code(c["code"])}):
                return respond(False, f"{c['code']} has redemption history and can't be deleted. "
                                      "Disable it instead - its history is kept.", 409)
            coupons_collection.delete_one({"_id": c["_id"]})
            return respond(True, "Coupon deleted.", back="coupon_list")
        except Exception as e:
            return respond(False, f"Could not delete coupon: {str(e)}", 500, back="coupon_list")

    # =========================================
    # CUSTOMER - LIST ACTIVE, UNEXPIRED COUPONS
    # (shown on the booking page so users can see and
    # apply available offers without knowing a code)
    # =========================================

    @app.route("/api/active-coupons")
    def api_active_coupons():

        if "user_email" not in session:
            return jsonify({"success": False, "message": "Not logged in"}), 401

        now = datetime.now()
        query = {
            "active": True,
            "$or": [
                {"expiry_at": None},
                {"expiry_at": {"$exists": False}},
                {"expiry_at": {"$gt": now}}
            ]
        }

        coupons = list(coupons_collection.find(query).sort("created_at", -1))
        index = build_redemption_index(coupon_redemptions_collection, bookings_collection) \
            if any(c.get("usage_limit") for c in coupons) else {}
        coupons = [c for c in coupons
                   if coupon_status(c, index.get(normalize_code(c["code"]), {}).get("count", 0), now) == "active"]

        return jsonify({
            "success": True,
            "coupons": [
                {
                    "code": c["code"],
                    "discount_type": c["discount_type"],
                    "value": c["value"],
                    "one_time_per_user": bool(c.get("one_time_per_user")),
                    "min_amount": c.get("min_amount"),
                    "max_discount": c.get("max_discount"),
                    "expiry_at": c["expiry_at"].isoformat() if c.get("expiry_at") else None
                }
                for c in coupons
            ]
        })

    # =========================================
    # CUSTOMER - APPLY COUPON (payment page, Feature 3)
    # =========================================

    @app.route("/apply-coupon/<booking_id>", methods=["POST"])
    def apply_coupon(booking_id):

        if "user_email" not in session:
            return jsonify({"success": False, "message": "Not logged in"}), 401

        data = request.get_json(silent=True) or {}
        code = normalize_code(data.get("code", ""))

        try:
            booking = bookings_collection.find_one({"_id": ObjectId(booking_id)})
        except Exception:
            booking = None

        if not booking or booking.get("user_email") != session["user_email"]:
            return jsonify({"success": False, "message": "Booking not found"}), 404

        if booking.get("payment_status") == "Paid (Online)":
            return jsonify({"success": False, "message": "This booking has already been paid for"})

        if booking.get("coupon"):
            return jsonify({"success": False, "message": "A coupon is already applied - remove it first"})

        # Anchor to the ORIGINAL amount so re-applying a different code
        # (or applying twice) never stacks discounts.
        base_amount = booking.get("pre_coupon_amount")
        if base_amount is None:
            base_amount = booking.get("delivery_fee", 0)

        coupon, discount, error = validate_coupon(
            coupons_collection, coupon_redemptions_collection,
            code, session["user_email"], base_amount, bookings_collection
        )
        if error:
            return jsonify({"success": False, "message": error})

        new_amount = max(0, round(base_amount - discount, 2))

        # Claim the redemption BEFORE touching the booking so a lost race on
        # a usage limit / one-time coupon leaves the booking untouched.
        if not record_redemption(coupon_redemptions_collection, session["user_email"], code, booking_id,
                                 discount=discount, coupons_collection=coupons_collection,
                                 bookings_collection=bookings_collection):
            return jsonify({"success": False,
                            "message": f"{code} is no longer available (usage limit reached)"})

        bookings_collection.update_one(
            {"_id": ObjectId(booking_id)},
            {"$set": {
                "delivery_fee": new_amount,
                "pre_coupon_amount": base_amount,
                "coupon": {
                    "code": code,
                    "discount_type": coupon["discount_type"],
                    "value": coupon["value"],
                    "discount": discount
                }
            }}
        )

        return jsonify({
            "success": True,
            "message": f"Coupon {code} applied successfully",
            "discount": discount,
            "new_amount": new_amount,
            "fully_paid": new_amount <= 0
        })

    # =========================================
    # CUSTOMER - REMOVE COUPON
    # =========================================

    @app.route("/remove-coupon/<booking_id>", methods=["POST"])
    def remove_coupon(booking_id):

        if "user_email" not in session:
            return jsonify({"success": False, "message": "Not logged in"}), 401

        try:
            booking = bookings_collection.find_one({"_id": ObjectId(booking_id)})
        except Exception:
            booking = None

        if not booking or booking.get("user_email") != session["user_email"]:
            return jsonify({"success": False, "message": "Booking not found"}), 404

        if not booking.get("coupon"):
            return jsonify({"success": False, "message": "No coupon is applied on this booking"})

        original_amount = booking.get("pre_coupon_amount", booking.get("delivery_fee", 0))
        code = booking["coupon"]["code"]

        bookings_collection.update_one(
            {"_id": ObjectId(booking_id)},
            {
                "$set": {"delivery_fee": original_amount},
                "$unset": {"coupon": "", "pre_coupon_amount": ""}
            }
        )
        release_redemption(coupon_redemptions_collection, session["user_email"], code, booking_id)

        return jsonify({
            "success": True,
            "message": "Coupon removed",
            "new_amount": original_amount
        })

    return app
