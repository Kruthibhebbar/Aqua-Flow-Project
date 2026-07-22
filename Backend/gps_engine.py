"""
gps_engine.py
=====================================================
Real-time GPS tracking + OSRM road routing engine.

Replaces every manually-entered location/ETA in the project with
values computed from:
  - the driver's REAL browser GPS (navigator.geolocation.watchPosition,
    pushed here every 5 seconds from driver_dashboard.html)
  - OSRM (Open Source Routing Machine, public demo server) for real
    road distance + driving-time ETA between two GPS points
  - a haversine straight-line fallback ONLY if OSRM is unreachable,
    so the page never breaks - but this is clearly not what's used
    for the primary "real routing" experience.

Nothing here accepts or stores a manually-typed location or ETA.
=====================================================
"""

from flask import request, jsonify, session
from bson.objectid import ObjectId
from datetime import datetime, timedelta
import math
import requests

# =====================================================
# CONFIG
# =====================================================

OSRM_BASE = "https://router.project-osrm.org"
OSRM_TIMEOUT_SECONDS = 4

GPS_STALE_AFTER_SECONDS = 30      # driver health monitor threshold
IDLE_AFTER_SECONDS = 5 * 60       # "Stopped" if no real movement for 5 min
IDLE_MOVE_THRESHOLD_KM = 0.03     # < 30m counts as "hasn't moved"
ARRIVAL_RADIUS_KM = 0.1           # ~100 meters
ROUTE_DEVIATION_KM = 0.5          # notify if driver strays >500m off route


# =====================================================
# MATH HELPERS
# =====================================================

def haversine_km(lat1, lng1, lat2, lng2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlng / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def osrm_route(lat1, lng1, lat2, lng2):
    """
    Real road distance + driving ETA between two GPS points via OSRM.
    Returns {distance_km, duration_min, geometry: [[lat,lng], ...], source}.
    Falls back to a straight-line haversine estimate only if OSRM
    cannot be reached, so the feature degrades gracefully instead of
    crashing - the 'source' field always tells you which one you got.
    """
    try:
        url = f"{OSRM_BASE}/route/v1/driving/{lng1},{lat1};{lng2},{lat2}"
        resp = requests.get(
            url,
            params={"overview": "full", "geometries": "geojson"},
            timeout=OSRM_TIMEOUT_SECONDS
        )
        data = resp.json()

        if data.get("code") == "Ok" and data.get("routes"):
            route = data["routes"][0]
            coords = route["geometry"]["coordinates"]  # OSRM gives [lng, lat]
            geometry = [[c[1], c[0]] for c in coords]

            return {
                "distance_km": round(route["distance"] / 1000, 2),
                "duration_min": max(1, round(route["duration"] / 60)),
                "geometry": geometry,
                "source": "osrm"
            }
    except Exception as e:
        print("OSRM routing error:", e)

    # ---- fallback: straight-line distance, ~30km/h average ----
    dist = haversine_km(lat1, lng1, lat2, lng2)
    return {
        "distance_km": round(dist, 2),
        "duration_min": max(1, round((dist / 30) * 60)),
        "geometry": [[lat1, lng1], [lat2, lng2]],
        "source": "fallback"
    }


def point_to_route_min_distance_km(lat, lng, geometry):
    """Cheap nearest-point-on-polyline distance, used for route-deviation checks."""
    if not geometry:
        return None
    step = max(1, len(geometry) // 200)
    best = None
    for i in range(0, len(geometry), step):
        p = geometry[i]
        d = haversine_km(lat, lng, p[0], p[1])
        if best is None or d < best:
            best = d
    return best


# =====================================================
# MODULE INIT
# =====================================================

def init_gps_engine(
    app,
    drivers_collection,
    bookings_collection,
    driver_locations_collection,
    tracking_history_collection,
    notifications_collection
):

    def is_driver_stale(driver):
        """Driver health monitor: no GPS ping in 30s+ => treated as offline,
        regardless of whatever online_status is still sitting in the DB."""
        last = driver.get("last_gps_at")
        if not last:
            return True
        return (datetime.now() - last).total_seconds() > GPS_STALE_AFTER_SECONDS

    def sync_stale_drivers():
        """Lazy sweep: flips any driver whose GPS has gone silent for
        30s+ to Offline in the DB, so every other query that filters on
        online_status (assign screen, fleet list, etc.) reflects reality
        without needing a background cron job."""
        cutoff = datetime.now() - timedelta(seconds=GPS_STALE_AFTER_SECONDS)
        drivers_collection.update_many(
            {
                "online_status": "Online",
                "$or": [
                    {"last_gps_at": {"$lt": cutoff}},
                    {"last_gps_at": {"$exists": False}}
                ]
            },
            {"$set": {"online_status": "Offline", "moving_status": "Offline"}}
        )

    # =================================================
    # STEP 1 + 2 + 8 + 10 + 11 + 12 (partial):
    # UNIFIED GPS PING
    # Driver's browser calls this every 5 seconds via
    # watchPosition, whether idle or on an active delivery.
    # =================================================

    @app.route("/api/gps/update", methods=["POST"])
    def gps_update():

        if "driver_email" not in session:
            return jsonify({"success": False, "message": "Not logged in"}), 401

        data = request.get_json(silent=True) or {}

        try:
            lat = float(data.get("lat"))
            lng = float(data.get("lng"))
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "lat/lng required"}), 400

        accuracy = data.get("accuracy")
        speed = data.get("speed")
        heading = data.get("heading")
        battery_level = data.get("battery_level")
        internet_status = data.get("internet_status", "online")

        now = datetime.now()
        driver_email = session["driver_email"]

        driver = drivers_collection.find_one({"email": driver_email})
        if not driver:
            return jsonify({"success": False, "message": "Driver not found"}), 404

        driver_id = str(driver["_id"])

        # ---- STEP: Driver Idle Detection ("Stopped" if no real movement
        #      for 5+ minutes) ----
        last_loc = driver_locations_collection.find_one({"driver_id": driver_id})
        moving_status = "Moving"
        if last_loc and last_loc.get("timestamp"):
            elapsed = (now - last_loc["timestamp"]).total_seconds()
            moved_km = haversine_km(lat, lng, last_loc["latitude"], last_loc["longitude"])
            if elapsed >= IDLE_AFTER_SECONDS and moved_km < IDLE_MOVE_THRESHOLD_KM:
                moving_status = "Stopped"

        point_doc = {
            "driver_id": driver_id,
            "driver_email": driver_email,
            "latitude": lat,
            "longitude": lng,
            "accuracy": accuracy,
            "speed": speed,
            "heading": heading,
            "timestamp": now,
            "moving_status": moving_status
        }

        # driver_locations: latest fix only (fast to read)
        driver_locations_collection.update_one(
            {"driver_id": driver_id},
            {"$set": point_doc},
            upsert=True
        )

        # tracking_history: every point, forever - replay / analytics / ML
        tracking_history_collection.insert_one(dict(point_doc))

        drivers_collection.update_one(
            {"_id": driver["_id"]},
            {"$set": {
                "current_lat": lat,
                "current_lng": lng,
                "gps_accuracy": accuracy,
                "current_speed": speed,
                "current_heading": heading,
                "battery_level": battery_level,
                "internet_status": internet_status,
                "last_gps_at": now,
                "last_seen": now,
                "online_status": "Online",
                "moving_status": moving_status
            }}
        )

        response_extra = {}

        # ---- STEP 11: Arrival Detection (~100m) ----
        # ---- STEP: Route Deviation Detection ----
        active_booking = bookings_collection.find_one({
            "driver_email": driver_email,
            "status": "On The Way"
        })

        if active_booking and active_booking.get("latitude") is not None:

            dist_to_customer = haversine_km(
                lat, lng,
                active_booking["latitude"], active_booking["longitude"]
            )
            response_extra["distance_to_customer_km"] = round(dist_to_customer, 3)

            if dist_to_customer <= ARRIVAL_RADIUS_KM:
                bookings_collection.update_one(
                    {"_id": active_booking["_id"], "status": "On The Way"},
                    {"$set": {"status": "Arrived", "arrived_at": now}}
                )
                notifications_collection.insert_one({
                    "user_email": active_booking["user_email"],
                    "title": "🚛 Driver Arrived",
                    "message": "Your AquaFlow driver has arrived at your location.",
                    "type": "arrived",
                    "status": "success",
                    "icon": "📍",
                    "read": False,
                    "created_at": now
                })
                response_extra["arrived"] = True

            planned_route = active_booking.get("planned_route_geometry")
            if planned_route:
                deviation_km = point_to_route_min_distance_km(lat, lng, planned_route)
                deviated = bool(deviation_km is not None and deviation_km > ROUTE_DEVIATION_KM)
                bookings_collection.update_one(
                    {"_id": active_booking["_id"]},
                    {"$set": {"route_deviated": deviated}}
                )
                response_extra["route_deviated"] = deviated

        return jsonify({
            "success": True,
            "moving_status": moving_status,
            **response_extra
        })

    # =================================================
    # STEP 5: INTELLIGENT DRIVER RANKING
    # Admin's "Assign Driver" screen calls this for a
    # booking - returns every online driver sorted nearest
    # -> farthest by REAL OSRM road distance/ETA.
    # =================================================

    @app.route("/api/gps/nearest-drivers/<booking_id>")
    def gps_nearest_drivers(booking_id):

        if "admin" not in session:
            return jsonify({"success": False, "message": "Unauthorized"}), 401

        sync_stale_drivers()

        try:
            booking = bookings_collection.find_one({"_id": ObjectId(booking_id)})
        except Exception:
            return jsonify({"success": False, "message": "Invalid booking id"}), 400

        if not booking or booking.get("latitude") is None or booking.get("longitude") is None:
            return jsonify({
                "success": False,
                "message": "This booking has no delivery GPS coordinates"
            }), 400

        clat, clng = booking["latitude"], booking["longitude"]

        drivers = list(drivers_collection.find({
            "approval_status": "Approved",
            "online_status": "Online"
        }))

        ranked = []
        for d in drivers:
            if d.get("current_lat") is None or d.get("current_lng") is None:
                continue

            route = osrm_route(d["current_lat"], d["current_lng"], clat, clng)

            ranked.append({
                "id": str(d["_id"]),
                "name": d.get("name", "Driver"),
                "email": d.get("email", ""),
                "phone": d.get("phone", ""),
                "truck_number": d.get("truck_number", "Not Assigned"),
                "distance_km": route["distance_km"],
                "eta_min": route["duration_min"],
                "moving_status": d.get("moving_status", "Unknown"),
                "gps_accuracy": d.get("gps_accuracy"),
                "route_source": route["source"]
            })

        ranked.sort(key=lambda x: x["distance_km"])
        for i, r in enumerate(ranked):
            r["recommended"] = (i == 0)

        return jsonify({"success": True, "drivers": ranked})

    # =================================================
    # STEP 4: INTELLIGENT DRIVER ASSIGNMENT
    # Admin sends ONLY driver_id. Everything else - live
    # location, distance, route, ETA - is computed here.
    # =================================================

    @app.route("/api/gps/auto-assign/<booking_id>", methods=["POST"])
    def gps_auto_assign(booking_id):

        if "admin" not in session:
            return jsonify({"success": False, "message": "Unauthorized"}), 401

        data = request.get_json(silent=True) or {}
        driver_id = data.get("driver_id")

        if not driver_id:
            return jsonify({"success": False, "message": "driver_id required"}), 400

        try:
            booking = bookings_collection.find_one({"_id": ObjectId(booking_id)})
            driver = drivers_collection.find_one({"_id": ObjectId(driver_id)})
        except Exception:
            return jsonify({"success": False, "message": "Invalid id"}), 400

        if not booking or not driver:
            return jsonify({"success": False, "message": "Booking or driver not found"}), 404

        if driver.get("current_lat") is None or booking.get("latitude") is None:
            return jsonify({
                "success": False,
                "message": "Missing real GPS coordinates for the driver or the customer"
            }), 400

        route = osrm_route(
            driver["current_lat"], driver["current_lng"],
            booking["latitude"], booking["longitude"]
        )
        now = datetime.now()

        bookings_collection.update_one(
            {"_id": booking["_id"]},
            {"$set": {
                "driver_name": driver.get("name"),
                "driver_email": driver.get("email"),
                "driver_phone": driver.get("phone"),
                "truck_number": driver.get("truck_number", "Not Assigned"),
                "live_location": f"{route['distance_km']} km away",
                "ETA": f"{route['duration_min']} min",
                "planned_route_geometry": route["geometry"],
                "route_deviated": False,
                "status": "Assigned",
                "driver_response": "Pending",
                "last_updated": now,
                "driver_assigned_at": now
            }}
        )

        notifications_collection.insert_one({
            "user_email": booking["user_email"],
            "title": "🚛 Driver Assigned - Awaiting Confirmation",
            "message": (
                f"Driver {driver.get('name')} has been assigned to your delivery "
                f"({route['distance_km']} km away, ~{route['duration_min']} min)."
            ),
            "type": "waiting_driver",
            "status": "warning",
            "icon": "⏳",
            "read": False,
            "created_at": now
        })

        notifications_collection.insert_one({
            "driver_email": driver.get("email"),
            "title": "📦 New Water Delivery Request",
            "message": (
                f"New delivery request from {booking.get('fullname')} - "
                f"{route['distance_km']} km away (~{route['duration_min']} min)."
            ),
            "booking_id": str(booking["_id"]),
            "type": "driver_request",
            "status": "Pending",
            "read": False,
            "created_at": now
        })

        return jsonify({
            "success": True,
            "distance_km": route["distance_km"],
            "eta_min": route["duration_min"],
            "route_source": route["source"]
        })

    # =================================================
    # STEP 8, 9, 10: LIVE TRACKING API
    # Used by the customer tracking page AND the admin
    # tracking page - real driver GPS, real OSRM route,
    # dynamic distance/ETA recalculated every call.
    # =================================================

    @app.route("/api/gps/track/<booking_id>")
    def gps_track(booking_id):

        try:
            booking = bookings_collection.find_one({"_id": ObjectId(booking_id)})
        except Exception:
            return jsonify({"success": False, "message": "Invalid booking id"}), 400

        if not booking:
            return jsonify({"success": False, "message": "Booking not found"}), 404

        result = {
            "success": True,
            "status": booking.get("status", "Pending"),
            "driver_name": booking.get("driver_name"),
            "driver_phone": booking.get("driver_phone"),
            "truck_number": booking.get("truck_number"),
            "customer_lat": booking.get("latitude"),
            "customer_lng": booking.get("longitude"),
            "arrived": booking.get("status") == "Arrived",
            "route_deviated": booking.get("route_deviated", False)
        }

        driver = None
        if booking.get("driver_email"):
            driver = drivers_collection.find_one({"email": booking["driver_email"]})

        if driver and driver.get("current_lat") is not None:
            result["driver_lat"] = driver["current_lat"]
            result["driver_lng"] = driver["current_lng"]
            result["driver_speed"] = driver.get("current_speed")
            result["driver_heading"] = driver.get("current_heading")
            result["driver_accuracy"] = driver.get("gps_accuracy")
            result["driver_online"] = not is_driver_stale(driver)
            result["moving_status"] = driver.get("moving_status", "Unknown")
            result["last_gps_at"] = (
                driver["last_gps_at"].isoformat() if driver.get("last_gps_at") else None
            )

            if (result["customer_lat"] is not None and result["driver_online"]
                    and booking.get("status") in ("Accepted", "On The Way", "Arrived")):
                route = osrm_route(
                    driver["current_lat"], driver["current_lng"],
                    booking["latitude"], booking["longitude"]
                )
                result["distance_remaining_km"] = route["distance_km"]
                result["eta_min"] = route["duration_min"]
                result["route_geometry"] = route["geometry"]

        return jsonify(result)

    # =================================================
    # STEP: REAL-TIME ADMIN DASHBOARD (fleet overview)
    # Every active delivery, real driver GPS, real ETA -
    # no simulated/random data.
    # =================================================

    @app.route("/api/gps/admin-fleet")
    def gps_admin_fleet():

        if "admin" not in session:
            return jsonify({"success": False, "message": "Unauthorized"}), 401

        sync_stale_drivers()

        active_bookings = list(bookings_collection.find({
            "status": {"$in": ["Assigned", "Accepted", "On The Way", "Arrived"]}
        }).sort("driver_assigned_at", -1))

        deliveries = []
        for b in active_bookings:
            driver = None
            if b.get("driver_email"):
                driver = drivers_collection.find_one({"email": b["driver_email"]})

            item = {
                "booking_id": str(b["_id"]),
                "customer": b.get("fullname"),
                "address": b.get("address"),
                "status": b.get("status"),
                "driver_name": b.get("driver_name"),
                "truck_number": b.get("truck_number"),
                "customer_lat": b.get("latitude"),
                "customer_lng": b.get("longitude"),
                "quantity": b.get("quantity"),
                "route_deviated": b.get("route_deviated", False)
            }

            if driver and driver.get("current_lat") is not None:
                stale = is_driver_stale(driver)
                item["driver_lat"] = driver["current_lat"]
                item["driver_lng"] = driver["current_lng"]
                item["driver_online"] = not stale
                item["moving_status"] = driver.get("moving_status", "Unknown")
                item["driver_speed"] = driver.get("current_speed")
                item["last_gps_at"] = (
                    driver["last_gps_at"].isoformat() if driver.get("last_gps_at") else None
                )

                if (not stale and item["customer_lat"] is not None
                        and b.get("status") in ("Accepted", "On The Way", "Arrived")):
                    route = osrm_route(
                        driver["current_lat"], driver["current_lng"],
                        b["latitude"], b["longitude"]
                    )
                    item["distance_remaining_km"] = route["distance_km"]
                    item["eta_min"] = route["duration_min"]
            else:
                item["driver_online"] = False

            deliveries.append(item)

        online_drivers = drivers_collection.count_documents({"online_status": "Online"})
        total_drivers = drivers_collection.count_documents({"approval_status": "Approved"})

        return jsonify({
            "success": True,
            "deliveries": deliveries,
            "online_drivers": online_drivers,
            "total_drivers": total_drivers,
            "timestamp": datetime.now().isoformat()
        })
