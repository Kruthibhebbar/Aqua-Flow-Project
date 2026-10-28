"""
fleet_center.py
=====================================================
Admin-only internal pages of the Live Fleet Command Center:

    /admin/live_tracking                       (in app.py - the map console)
    /admin/live_tracking/delivery/<booking_id> Delivery details
    /admin/live_tracking/driver/<driver_id>    Driver details
    /admin/live_tracking/driver/<driver_id>/trail   GPS tracking details
    /api/fleet/delivery/<booking_id>           JSON used by the map + delivery page

Every page extends templates/fleet_base.html, which includes the shared
header (templates/_fleet_header.html) containing the
"Back to Admin Dashboard" link. That link is a plain <a href> built with
url_for('admin_dashboard') - it never uses history.back()/referrer, so one
click always returns to the dashboard no matter how many fleet pages were
opened.

Only real data is shown: nothing here invents a location, ETA or route.
=====================================================
"""

from datetime import datetime, timedelta

from bson.objectid import ObjectId
from flask import abort, jsonify, redirect, render_template, request, session, url_for

from gps_engine import (
    ACTIVE_DELIVERY_STATUSES,
    FRESHNESS_LIVE_SECONDS,
    cached_osrm_route,
    classify_freshness,
    gps_age_seconds,
    gps_label,
)

TRAIL_DEFAULT_HOURS = 6
TRAIL_MAX_HOURS = 72
TRAIL_DEFAULT_LIMIT = 300
TRAIL_MAX_LIMIT = 1000


def _oid(value):
    """ObjectId or None - never raises on user-supplied ids."""
    try:
        return ObjectId(value)
    except Exception:
        return None


def _int_param(name, default, lo, hi):
    """Validated integer query parameter, clamped to [lo, hi]."""
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        abort(400, description=f"'{name}' must be a whole number")
    return max(lo, min(hi, value))


def _iso(dt):
    return dt.isoformat() if isinstance(dt, datetime) else None


def init_fleet_center(app, drivers_collection, bookings_collection, tracking_history_collection):

    def admin_page_guard():
        if "admin" not in session:
            return redirect(url_for("admin_login"))
        return None

    def delivery_payload(booking, driver):
        """Public-to-admin view of one delivery. Real values only."""
        loc_ok = bool(driver) and driver.get("current_lat") is not None \
            and driver.get("current_lng") is not None
        last_gps_at = driver.get("last_gps_at") if driver else None
        return {
            "booking_id": str(booking["_id"]),
            "booking_ref": str(booking["_id"])[-6:].upper(),
            "customer": booking.get("fullname"),
            "address": booking.get("address"),
            "status": booking.get("status"),
            "quantity": booking.get("quantity"),
            "driver_id": str(driver["_id"]) if driver else None,
            "driver_name": booking.get("driver_name") or (driver.get("name") if driver else None),
            "driver_phone": driver.get("phone") if driver else None,
            "truck_number": booking.get("truck_number") or (driver.get("truck_number") if driver else None),
            "customer_lat": booking.get("latitude"),
            "customer_lng": booking.get("longitude"),
            "driver_lat": driver.get("current_lat") if loc_ok else None,
            "driver_lng": driver.get("current_lng") if loc_ok else None,
            "last_gps_at": _iso(last_gps_at),
            "last_gps_age_s": gps_age_seconds(last_gps_at),
            "last_gps_label": gps_label(last_gps_at),
            "location_freshness": classify_freshness(last_gps_at, loc_ok),
            "driver_assigned_at": _iso(booking.get("driver_assigned_at")),
            "delivered_at": _iso(booking.get("delivered_at")),
            "route_deviated": bool(booking.get("route_deviated", False)),
        }

    # -------------------------------------------------
    # JSON: delivery + (when possible) real road route
    # -------------------------------------------------
    @app.route("/api/fleet/delivery/<booking_id>")
    def fleet_delivery_api(booking_id):
        if "admin" not in session:
            return jsonify({"success": False, "message": "Unauthorized"}), 401

        oid = _oid(booking_id)
        if oid is None:
            return jsonify({"success": False, "message": "Invalid booking id"}), 400
        booking = bookings_collection.find_one({"_id": oid})
        if not booking:
            return jsonify({"success": False, "message": "Booking not found"}), 404

        driver = None
        if booking.get("driver_email"):
            driver = drivers_collection.find_one({"email": booking["driver_email"]})

        data = delivery_payload(booking, driver)
        data["route"] = {"available": False, "reason": None}

        if booking.get("status") not in ("Accepted", "On The Way", "Arrived"):
            data["route"]["reason"] = "Route is shown once the driver has accepted the delivery."
        elif data["driver_lat"] is None:
            data["route"]["reason"] = "No GPS location has been received from this driver yet."
        elif data["location_freshness"] != "live":
            data["route"]["reason"] = (
                f"Driver location is stale (no GPS ping in the last {FRESHNESS_LIVE_SECONDS} s)."
            )
        elif data["customer_lat"] is None or data["customer_lng"] is None:
            data["route"]["reason"] = "This booking has no delivery coordinates."
        else:
            route = cached_osrm_route(
                data["driver_lat"], data["driver_lng"],
                data["customer_lat"], data["customer_lng"],
            )
            if route["source"] == "osrm":
                data["route"] = {
                    "available": True,
                    "source": "osrm",
                    "distance_km": route["distance_km"],
                    "eta_min": route["duration_min"],
                    "geometry": route["geometry"],
                }
            else:
                data["route"]["reason"] = (
                    "Road routing service is unreachable, so no road route or ETA is shown. "
                    f"Straight-line distance is about {route['distance_km']} km."
                )
        return jsonify({"success": True, "delivery": data,
                        "freshness_live_seconds": FRESHNESS_LIVE_SECONDS})

    # -------------------------------------------------
    # PAGE: delivery details
    # -------------------------------------------------
    @app.route("/admin/live_tracking/delivery/<booking_id>")
    def fleet_delivery_page(booking_id):
        guard = admin_page_guard()
        if guard:
            return guard
        oid = _oid(booking_id)
        booking = bookings_collection.find_one({"_id": oid}) if oid else None
        if not booking:
            abort(404)
        driver = None
        if booking.get("driver_email"):
            driver = drivers_collection.find_one({"email": booking["driver_email"]})
        return render_template(
            "fleet_delivery.html",
            d=delivery_payload(booking, driver),
            live_seconds=FRESHNESS_LIVE_SECONDS,
        )

    # -------------------------------------------------
    # PAGE: driver details
    # -------------------------------------------------
    @app.route("/admin/live_tracking/driver/<driver_id>")
    def fleet_driver_page(driver_id):
        guard = admin_page_guard()
        if guard:
            return guard
        oid = _oid(driver_id)
        driver = drivers_collection.find_one({"_id": oid}) if oid else None
        if not driver:
            abort(404)

        recent = list(
            bookings_collection.find({"driver_email": driver.get("email")})
            .sort("driver_assigned_at", -1).limit(10)
        )
        deliveries = [{
            "booking_id": str(b["_id"]),
            "booking_ref": str(b["_id"])[-6:].upper(),
            "customer": b.get("fullname"),
            "status": b.get("status"),
            "active": b.get("status") in ACTIVE_DELIVERY_STATUSES,
            "assigned_at": _iso(b.get("driver_assigned_at")),
        } for b in recent]

        has_loc = driver.get("current_lat") is not None and driver.get("current_lng") is not None
        last_gps_at = driver.get("last_gps_at")
        info = {
            "id": str(driver["_id"]),
            "name": driver.get("name", "Driver"),
            "phone": driver.get("phone", ""),
            "truck_number": driver.get("truck_number", ""),
            "approval_status": driver.get("approval_status", ""),
            "online_status": driver.get("online_status", "Offline"),
            "moving_status": driver.get("moving_status", "Unknown"),
            "lat": driver.get("current_lat") if has_loc else None,
            "lng": driver.get("current_lng") if has_loc else None,
            "last_gps_at": _iso(last_gps_at),
            "last_gps_age_s": gps_age_seconds(last_gps_at),
            "last_gps_label": gps_label(last_gps_at),
            "accuracy": driver.get("gps_accuracy"),
            "battery_level": driver.get("battery_level"),
            "location_freshness": classify_freshness(last_gps_at, has_loc),
        }
        return render_template(
            "fleet_driver.html", drv=info, deliveries=deliveries,
            live_seconds=FRESHNESS_LIVE_SECONDS,
        )

    # -------------------------------------------------
    # PAGE: GPS tracking details (real recorded points)
    # -------------------------------------------------
    @app.route("/admin/live_tracking/driver/<driver_id>/trail")
    def fleet_trail_page(driver_id):
        guard = admin_page_guard()
        if guard:
            return guard
        oid = _oid(driver_id)
        driver = drivers_collection.find_one({"_id": oid}) if oid else None
        if not driver:
            abort(404)

        hours = _int_param("hours", TRAIL_DEFAULT_HOURS, 1, TRAIL_MAX_HOURS)
        limit = _int_param("limit", TRAIL_DEFAULT_LIMIT, 10, TRAIL_MAX_LIMIT)
        since = datetime.now() - timedelta(hours=hours)

        rows = list(
            tracking_history_collection.find(
                {"driver_id": str(driver["_id"]), "timestamp": {"$gte": since}}
            ).sort("timestamp", -1).limit(limit)
        )
        rows.reverse()  # oldest -> newest for the polyline
        points = [{
            "lat": r.get("latitude"),
            "lng": r.get("longitude"),
            "t": gps_label(r.get("timestamp")),
            "speed": r.get("speed"),
            "accuracy": r.get("accuracy"),
        } for r in rows if r.get("latitude") is not None and r.get("longitude") is not None]

        return render_template(
            "fleet_trail.html",
            drv={"id": str(driver["_id"]), "name": driver.get("name", "Driver"),
                 "truck_number": driver.get("truck_number", "")},
            points=points, hours=hours, limit=limit,
        )
