"""
pricing_engine.py
=====================

Real, server-side price calculation for a booking - replaces the old
flat "capacity -> price" guess with an itemised, genuinely computed
total:

    Tanker Cost      - base water charge for the selected capacity
    Delivery Fee     - per_km_rate x REAL distance from the depot to
                       the customer's pinned location (haversine,
                       computed from actual lat/lng the customer
                       dropped on the map - not guessed)
    Platform Fee     - flat service fee
    GST              - 18% of (Tanker Cost + Delivery Fee + Platform Fee)
    ---------------------------------------------------------------
    Total            - sum of the above, before any coupon

Every number here is recomputed from scratch server-side at booking
time - the client-side preview call hits the same function, so what
the customer sees while filling the form is exactly what gets stored.
Nothing is ever trusted from the browser except capacity/lat/lng.

Depot location, per-km rate, platform fee, and GST rate are stored in
settings_collection so the admin can tune them without a code change,
the same pattern already used for company bank details.
"""

from gps_engine import haversine_km

# =====================================================
# DEFAULTS (used until an admin overrides them via settings_collection)
# =====================================================

_SETTINGS_ID = "pricing_config"

_DEFAULT_PRICING = {
    # Company depot location (used as the delivery-distance origin).
    # Default is central Bengaluru - update via settings_collection
    # once, or expose an admin settings page for it.
    "depot_lat": 12.9716,
    "depot_lng": 77.5946,

    "tanker_prices": {
        "1000": 750,
        "2000": 1200,
        "5000": 2600,
        "10000": 4800,
    },

    "per_km_rate": 8,        # Rs per km, straight-line distance
    "min_delivery_fee": 30,  # floor, so very-close addresses aren't Rs0
    "platform_fee": 20,      # flat, per booking
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


def calculate_booking_price(quantity, customer_lat, customer_lng, settings_collection=None):
    """
    Returns a full price breakdown dict. Never raises for missing
    lat/lng - falls back to the minimum delivery fee so the form still
    works if the customer's browser blocked location / map interaction
    failed, but distance_km will be None so the UI can be honest about it.
    """

    settings = get_pricing_settings(settings_collection)

    tanker_cost = settings["tanker_prices"].get(str(quantity))
    if tanker_cost is None:
        # Unknown capacity - shouldn't happen from the dropdown, but
        # never silently charge Rs0.
        tanker_cost = max(settings["tanker_prices"].values())

    distance_km = None
    if customer_lat is not None and customer_lng is not None:
        try:
            distance_km = round(
                haversine_km(
                    float(settings["depot_lat"]), float(settings["depot_lng"]),
                    float(customer_lat), float(customer_lng)
                ), 2
            )
        except (TypeError, ValueError):
            distance_km = None

    if distance_km is not None:
        delivery_fee = max(
            settings["min_delivery_fee"],
            round(distance_km * settings["per_km_rate"], 2)
        )
    else:
        delivery_fee = settings["min_delivery_fee"]

    platform_fee = settings["platform_fee"]

    subtotal = tanker_cost + delivery_fee + platform_fee
    gst = round(subtotal * settings["gst_percent"] / 100, 2)
    total = round(subtotal + gst, 2)

    return {
        "tanker_cost": tanker_cost,
        "distance_km": distance_km,
        "delivery_fee": delivery_fee,
        "platform_fee": platform_fee,
        "gst_percent": settings["gst_percent"],
        "gst": gst,
        "subtotal": subtotal,
        "total": total
    }
