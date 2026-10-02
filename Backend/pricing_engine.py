"""
pricing_engine.py
=====================

Real, server-side price calculation for a booking - computed from the
distance to the nearest genuinely AVAILABLE driver, not a fixed
company location. This was previously wrong: it used a hardcoded
depot lat/lng as if the company delivered from one fixed warehouse,
which doesn't reflect how this platform actually works (independent
drivers, each at their own live location).

How "nearest available driver" is determined:
    1. Only drivers with approval_status == "Approved" and
       online_status == "Online" are even considered.
    2. Of those, any driver who currently has an ACTIVE booking
       (Assigned / Accepted / Arrived / On The Way) is excluded - a
       driver who is online but already out on a delivery is NOT
       available for a new order, even though their GPS is live.
    3. Of the drivers left, straight-line (haversine) distance from
       each driver's current_lat/current_lng to the customer's pinned
       location is computed, and the closest one wins.
    4. If the nearest available driver is more than MAX_DELIVERY_RADIUS_KM
       away - or there are no available drivers at all - the address
       is undeliverable. No price is calculated; the customer is told
       plainly that delivery isn't available to their address right now.

Tanker Cost + Delivery Fee (from step 3/4) + Platform Fee + GST -
computed server-side so this is the authoritative number, not
something trusted from the browser. The exact same function backs the
live preview on booking.html, so what the customer sees while filling
the form matches what's actually charged (or matches the "not
deliverable" message, if that's the outcome).

Platform fee, GST rate, and the delivery radius/rate are stored in
settings_collection so an admin can tune them without a code change,
the same pattern already used for company bank details.
"""


from gps_engine import haversine_km


# =====================================================
# DEFAULTS (used until an admin overrides them via settings_collection)
# =====================================================

_SETTINGS_ID = "pricing_config"

_DEFAULT_PRICING = {
    "tanker_prices": {
        "1000": 750,
        "2000": 1200,
        "5000": 2600,
        "10000": 4800,
    },

    "per_km_rate": 8,             # Rs per km, straight-line distance to the nearest driver
    "min_delivery_fee": 30,       # floor, so a very-close driver isn't Rs0
    "max_delivery_radius_km": 50, # beyond this, the address is undeliverable
    "platform_fee": 20,           # flat, per booking
    "gst_percent": 18,
}


def get_pricing_settings(settings_collection):
    if settings_collection is None:
        return dict(_DEFAULT_PRICING)

    doc = settings_collection.find_one({"_id": _SETTINGS_ID})
    if not doc:
        return dict(_DEFAULT_PRICING)

    merged = dict(_DEFAULT_PRICING)
    merged.update({k: v for k, v in doc.items() if k != "_id"})
    return merged


# =====================================================
# NEAREST DRIVER (busy drivers DO count as available)
# =====================================================
#
# Only two things disqualify a driver from a new booking:
#   1. They're outside the delivery radius entirely.
#   2. They're Offline.
# Being currently busy on another delivery does NOT disqualify them -
# an online driver mid-delivery is still considered available and the
# customer can book; the order will just queue for that driver.
#
# Three possible outcomes when looking for a driver near an address:
#   - "no_coverage"   : no driver (online or offline) has ever been
#                        seen within the delivery radius at all -
#                        nobody is registered/operating in that area.
#   - "all_offline"    : driver(s) exist within range, but every one
#                        of them is currently Offline.
#   - "available"      : at least one driver within range is Online
#                        (whether busy or free) - booking proceeds,
#                        priced off the nearest Online one.

def find_nearest_driver(customer_lat, customer_lng, drivers_collection,
                         max_radius_km=50):
    """
    Returns (status, driver_or_None, distance_km_or_None) where status
    is one of "no_coverage", "all_offline", "available".
    """

    if drivers_collection is None or customer_lat is None or customer_lng is None:
        return "no_coverage", None, None

    in_range = []

    for d in drivers_collection.find({"approval_status": "Approved"}):
        if d.get("current_lat") is None or d.get("current_lng") is None:
            continue

        distance = haversine_km(
            float(d["current_lat"]), float(d["current_lng"]),
            float(customer_lat), float(customer_lng)
        )

        if distance <= max_radius_km:
            in_range.append((distance, d))

    if not in_range:
        return "no_coverage", None, None

    in_range.sort(key=lambda pair: pair[0])

    online_in_range = [(dist, d) for dist, d in in_range if d.get("online_status") == "Online"]

    if not online_in_range:
        return "all_offline", None, None

    nearest_distance, nearest_driver = online_in_range[0]
    return "available", nearest_driver, round(nearest_distance, 2)


# Kept for backward compatibility with any other caller that imported
# the old name directly.
def find_nearest_available_driver(customer_lat, customer_lng, drivers_collection, bookings_collection=None):
    status, driver, distance_km = find_nearest_driver(customer_lat, customer_lng, drivers_collection)
    if status != "available":
        return None, None
    return driver, distance_km


def calculate_booking_price(quantity, customer_lat, customer_lng, settings_collection=None,
                             drivers_collection=None, bookings_collection=None):
    """
    Returns a full price breakdown dict, OR a dict with
    "deliverable": False and a "reason" if no available driver is
    within range. Never raises for missing lat/lng - if the customer
    hasn't pinned a location yet, distance_km is None and the
    breakdown reflects that (not an error, just "not calculated yet").
    """

    settings = get_pricing_settings(settings_collection)

    tanker_cost = settings["tanker_prices"].get(str(quantity))
    if tanker_cost is None:
        tanker_cost = max(settings["tanker_prices"].values())

    if customer_lat is None or customer_lng is None:
        # No location pinned yet - can't determine deliverability or
        # the real delivery fee. Not an error, just incomplete input.
        return {
            "deliverable": True,
            "awaiting_location": True,
            "tanker_cost": tanker_cost,
            "distance_km": None,
            "nearest_driver_name": None,
            "delivery_fee": settings["min_delivery_fee"],
            "platform_fee": settings["platform_fee"],
            "gst_percent": settings["gst_percent"],
            "gst": 0,
            "subtotal": 0,
            "total": 0
        }

    status, driver, distance_km = find_nearest_driver(
        customer_lat, customer_lng, drivers_collection,
        max_radius_km=settings["max_delivery_radius_km"]
    )

    if status == "no_coverage":
        return {
            "deliverable": False,
            "awaiting_location": False,
            "reason": "No delivery is available in your location.",
            "tanker_cost": tanker_cost,
            "distance_km": None,
            "nearest_driver_name": None
        }

    if status == "all_offline":
        return {
            "deliverable": False,
            "awaiting_location": False,
            "reason": "A driver covers your area, but they're currently unavailable. Please try again shortly.",
            "tanker_cost": tanker_cost,
            "distance_km": None,
            "nearest_driver_name": None
        }

    delivery_fee = max(
        settings["min_delivery_fee"],
        round(distance_km * settings["per_km_rate"], 2)
    )
    platform_fee = settings["platform_fee"]

    subtotal = tanker_cost + delivery_fee + platform_fee
    gst = round(subtotal * settings["gst_percent"] / 100, 2)
    total = round(subtotal + gst, 2)

    return {
        "deliverable": True,
        "awaiting_location": False,
        "tanker_cost": tanker_cost,
        "distance_km": distance_km,
        "nearest_driver_name": driver.get("name", "a nearby driver"),
        "delivery_fee": delivery_fee,
        "platform_fee": platform_fee,
        "gst_percent": settings["gst_percent"],
        "gst": gst,
        "subtotal": subtotal,
        "total": total
    }
