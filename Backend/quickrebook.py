# =====================================================================
# quickrebook.py
# ---------------------------------------------------------------------
# Real backend for the "Quick Rebook" page.
#
# The old quick_rebook.html was a static demo: six hardcoded bookings
# living only in the browser's localStorage, never touching MongoDB.
# This module replaces that with the customer's actual booking history:
#
#   - GET  /quick_rebook                       renders the page from
#                                               bookings_collection
#   - POST /api/quick-rebook/<key>              places a REAL new
#                                               booking in MongoDB,
#                                               cloned from that
#                                               address's most recent
#                                               order
#   - POST /api/quick-rebook/<key>/hide         hides an address from
#                                               the customer's quick
#                                               list (stored on their
#                                               user document — never
#                                               deletes real bookings)
#
# Wired into app.py the same way every other feature module is:
#
#   from quickrebook import init_quickrebook
#   init_quickrebook(app, bookings_collection, users_collection)
# =====================================================================

from flask import render_template, redirect, url_for, session, jsonify
from datetime import datetime, timedelta

# Kept in sync with the pricing shown in booking.html / enforced in the
# /booking route — the source of truth for what a quantity actually costs.
PRICING_MAP = {
    "1000": 750,
    "2000": 1200,
    "5000": 2600,
    "10000": 4800,
}

DEFAULT_DELIVERY_TIME = "09:00"


def _next_valid_delivery_date():
    """The /booking route rejects past dates, so a rebook always targets
    tomorrow — the earliest date guaranteed to still be valid."""
    return (datetime.now().date() + timedelta(days=1)).strftime("%Y-%m-%d")


def _quantity_int(raw_quantity):
    try:
        return int(raw_quantity)
    except (TypeError, ValueError):
        return 0


def _relative_day_label(created_at):
    if not isinstance(created_at, datetime):
        return "Unknown"
    delta_days = (datetime.now().date() - created_at.date()).days
    if delta_days <= 0:
        return "Today"
    if delta_days == 1:
        return "Yesterday"
    if delta_days < 30:
        return f"{delta_days} days ago"
    months = delta_days // 30
    if months < 12:
        return f"{months} month{'s' if months != 1 else ''} ago"
    years = delta_days // 365
    return f"{years} year{'s' if years != 1 else ''} ago"


def init_quickrebook(app, bookings_collection, users_collection):

    def _build_quick_items(user_email, hidden_keys):
        """One card per unique delivery address, built from the
        customer's real bookings, most-recently-ordered first."""
        raw_bookings = list(
            bookings_collection.find({"user_email": user_email}).sort("created_at", -1)
        )

        items_by_key = {}
        key_order = []

        for b in raw_bookings:
            address = (b.get("address") or "").strip()
            if not address:
                continue
            key = address.lower()

            if key not in items_by_key:
                items_by_key[key] = {
                    "key": key,
                    "address": address,
                    "quantity": b.get("quantity"),
                    "quantity_int": _quantity_int(b.get("quantity")),
                    "water_type": b.get("water_type") or "Fresh Water",
                    "fullname": b.get("fullname"),
                    "phone": b.get("phone"),
                    "latitude": b.get("latitude"),
                    "longitude": b.get("longitude"),
                    "source_booking_id": str(b.get("_id")),
                    "last_ordered_display": _relative_day_label(b.get("created_at")),
                    "rebook_count": 0,
                }
                key_order.append(key)

            items_by_key[key]["rebook_count"] += 1

        return [items_by_key[k] for k in key_order if k not in hidden_keys]

    @app.route("/quick_rebook")
    def quick_rebook():
        if "user_email" not in session:
            return redirect(url_for("login"))

        user_email = session["user_email"]
        user = users_collection.find_one({"email": user_email}) or {}
        hidden_keys = set(user.get("hidden_quick_rebook", []))

        quick_items = _build_quick_items(user_email, hidden_keys)

        total_bookings = len(quick_items)
        total_liters = sum(item["quantity_int"] for item in quick_items)
        # "Repeat orders" = times an address was ordered again beyond the first time.
        total_rebooks = sum(max(item["rebook_count"] - 1, 0) for item in quick_items)

        return render_template(
            "quick_rebook.html",
            quick_items=quick_items,
            total_bookings=total_bookings,
            total_liters=total_liters,
            total_rebooks=total_rebooks,
        )

    @app.route("/api/quick-rebook/<path:booking_key>", methods=["POST"])
    def api_quick_rebook_create(booking_key):
        if "user_email" not in session:
            return jsonify({"ok": False, "error": "Please log in again."}), 401

        user_email = session["user_email"]
        user = users_collection.find_one({"email": user_email}) or {}

        # Blocked accounts keep their session but lose the ability to
        # book - same rule enforced on the main /booking route.
        if user.get("is_blocked"):
            session.clear()
            return jsonify({
                "ok": False,
                "error": "Your account has been suspended. Contact AquaFlow support for help."
            }), 403

        hidden_keys = set(user.get("hidden_quick_rebook", []))

        template = next(
            (item for item in _build_quick_items(user_email, hidden_keys)
             if item["key"] == booking_key),
            None,
        )
        if not template:
            return jsonify({"ok": False, "error": "That address wasn't found in your history."}), 404

        quantity = str(template["quantity"])
        delivery_fee = PRICING_MAP.get(quantity)
        if delivery_fee is None:
            return jsonify({"ok": False, "error": "This quantity can no longer be auto-rebooked — please use New Booking."}), 400

        if not template.get("phone") or template.get("latitude") is None or template.get("longitude") is None:
            return jsonify({"ok": False, "error": "This address is missing details needed to rebook — please use New Booking instead."}), 400

        new_booking = {
            "fullname": template.get("fullname") or "Customer",
            "phone": template.get("phone"),
            "address": template.get("address"),
            "latitude": template.get("latitude"),
            "longitude": template.get("longitude"),
            "water_type": template.get("water_type") or "Fresh Water",
            "quantity": quantity,
            "delivery_fee": delivery_fee,
            "delivery_date": _next_valid_delivery_date(),
            "delivery_time": DEFAULT_DELIVERY_TIME,
            "admin_seen_future": False,
            "user_email": user_email,
            "status": "Pending",
            "driver_name": "Not Assigned",
            "driver_phone": "Not Available",
            "truck_number": "Not Assigned",
            "live_location": "Waiting For Approval",
            "ETA": "Not Available",
            "payment_status": "Pending",
            "payment_method": "Cash on Delivery",
            "created_at": datetime.now(),
            "rebooked_from": template["source_booking_id"],
        }

        result = bookings_collection.insert_one(new_booking)

        return jsonify({
            "ok": True,
            "booking_id": str(result.inserted_id),
            "message": f"Booked {quantity}L for {template['address']} — arriving {new_booking['delivery_date']}.",
        })

    @app.route("/api/quick-rebook/<path:booking_key>/hide", methods=["POST"])
    def api_quick_rebook_hide(booking_key):
        if "user_email" not in session:
            return jsonify({"ok": False, "error": "Please log in again."}), 401

        users_collection.update_one(
            {"email": session["user_email"]},
            {"$addToSet": {"hidden_quick_rebook": booking_key}},
        )
        return jsonify({"ok": True})
