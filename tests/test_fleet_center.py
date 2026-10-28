"""Tests for the Live Fleet Command Center (map page, fleet API, internal pages,
and the one-click Back to Admin Dashboard link)."""

from datetime import datetime, timedelta

import pytest
from bson.objectid import ObjectId

import gps_engine

FLEET_PAGES = [
    "/admin/live_tracking",
    "/admin/live_tracking/delivery/{booking}",
    "/admin/live_tracking/driver/{driver}",
    "/admin/live_tracking/driver/{driver}/trail",
]


def admin_login(client):
    with client.session_transaction() as s:
        s["admin"] = True


@pytest.fixture
def fleet(app, monkeypatch):
    # never touch the real OSRM server from tests
    monkeypatch.setattr(gps_engine, "osrm_route", lambda *a: {
        "distance_km": 4.2, "duration_min": 9, "geometry": [[a[0], a[1]], [a[2], a[3]]], "source": "osrm"})
    gps_engine._ROUTE_CACHE.clear()
    now = datetime.now()
    live = app.drivers_collection.insert_one({
        "name": "Live Larry", "email": "live@x.com", "phone": "9000000001", "truck_number": "KA01AA1111",
        "approval_status": "Approved", "online_status": "Online", "current_lat": 12.97, "current_lng": 77.59,
        "last_gps_at": now, "moving_status": "Moving"}).inserted_id
    stale = app.drivers_collection.insert_one({
        "name": "Stale Sam", "email": "stale@x.com", "phone": "9000000002", "truck_number": "KA02BB2222",
        "approval_status": "Approved", "online_status": "Online", "current_lat": 13.0, "current_lng": 77.6,
        "last_gps_at": now - timedelta(minutes=10)}).inserted_id
    app.drivers_collection.insert_one({
        "name": "No Gps Nina", "email": "nogps@x.com", "phone": "9000000003", "truck_number": "KA03CC3333",
        "approval_status": "Approved", "online_status": "Offline"})
    app.drivers_collection.insert_one({
        "name": "Pending Pete", "email": "pend@x.com", "approval_status": "Pending", "online_status": "Offline"})
    b1 = app.bookings_collection.insert_one({
        "fullname": "Cust One", "address": "1 Main St", "status": "On The Way", "latitude": 12.98, "longitude": 77.6,
        "driver_email": "live@x.com", "driver_name": "Live Larry", "truck_number": "KA01AA1111",
        "driver_assigned_at": now}).inserted_id
    b2 = app.bookings_collection.insert_one({
        "fullname": "Cust Two", "address": "2 Side St", "status": "Accepted", "latitude": 13.01, "longitude": 77.61,
        "driver_email": "stale@x.com", "driver_name": "Stale Sam", "truck_number": "KA02BB2222",
        "driver_assigned_at": now}).inserted_id
    b3 = app.bookings_collection.insert_one({
        "fullname": "Cust Three", "address": "3 Far St", "status": "Assigned",
        "driver_email": "nogps@x.com", "driver_name": "No Gps Nina", "truck_number": "KA03CC3333",
        "driver_assigned_at": now}).inserted_id
    app.bookings_collection.insert_one({"fullname": "Done", "status": "Delivered", "delivered_at": now})
    app.bookings_collection.insert_one({"fullname": "Old", "status": "Delivered", "delivered_at": now - timedelta(days=3)})
    app.tracking_history_collection.insert_one({
        "driver_id": str(live), "latitude": 12.97, "longitude": 77.59, "timestamp": now, "speed": 5.0})
    yield {"live": str(live), "stale": str(stale), "b1": str(b1), "b2": str(b2), "b3": str(b3)}
    app.tracking_history_collection.delete_many({})


# ---------------- access control ----------------

@pytest.mark.parametrize("path", FLEET_PAGES)
def test_pages_redirect_anonymous_users_to_admin_login(client, fleet, path):
    url = path.format(booking=fleet["b1"], driver=fleet["live"])
    resp = client.get(url)
    assert resp.status_code == 302
    assert "admin" in resp.headers["Location"].lower()


def test_apis_reject_anonymous(client, fleet):
    assert client.get("/api/gps/admin-fleet").status_code == 401
    assert client.get(f"/api/fleet/delivery/{fleet['b1']}").status_code == 401


def test_customer_session_is_not_admin(client, fleet):
    with client.session_transaction() as s:
        s["user_email"] = "customer@example.com"
    assert client.get("/admin/live_tracking").status_code == 302
    assert client.get("/api/gps/admin-fleet").status_code == 401


# ---------------- one-click Back to Admin Dashboard ----------------

@pytest.mark.parametrize("path", FLEET_PAGES)
def test_every_fleet_page_has_direct_dashboard_link_without_history_tricks(client, fleet, path):
    admin_login(client)
    url = path.format(booking=fleet["b1"], driver=fleet["live"])
    html = client.get(url).get_data(as_text=True)
    assert 'href="/admin-dashboard"' in html
    assert "Back to Admin Dashboard" in html
    for banned in ("history.back", "history.go", "document.referrer"):
        assert banned not in html


def test_dashboard_link_target_is_the_real_admin_dashboard(client, fleet):
    admin_login(client)
    resp = client.get("/admin-dashboard")
    assert resp.status_code == 200


def test_unknown_or_invalid_ids_return_404(client, fleet):
    admin_login(client)
    assert client.get("/admin/live_tracking/delivery/not-an-id").status_code == 404
    assert client.get(f"/admin/live_tracking/delivery/{ObjectId()}").status_code == 404
    assert client.get("/admin/live_tracking/driver/zzz").status_code == 404
    assert client.get("/api/fleet/delivery/zzz").status_code == 400


def test_trail_query_params_are_validated_and_clamped(client, fleet):
    admin_login(client)
    base = f"/admin/live_tracking/driver/{fleet['live']}/trail"
    assert client.get(base + "?hours=abc").status_code == 400
    assert client.get(base + "?limit=1e9").status_code == 400
    assert client.get(base + "?hours=99999&limit=99999999").status_code == 200


# ---------------- fleet API data ----------------

def test_fleet_stats_come_from_real_data(client, fleet):
    admin_login(client)
    j = client.get("/api/gps/admin-fleet").get_json()
    s = j["stats"]
    assert s["registered_drivers"] == 4
    assert s["approved_drivers"] == 3
    assert s["online_drivers"] == 1          # Stale Sam is flipped offline by the 30 s health monitor
    assert s["offline_drivers"] == 2
    assert s["active_deliveries"] == 3
    assert s["completed_today"] == 1         # the 3-day-old delivery is excluded
    assert s["location_issues"] == 2         # stale + no gps
    assert j["total_drivers"] == 3 and j["online_drivers"] == 1   # legacy keys kept


def test_freshness_is_based_on_real_timestamps(client, fleet):
    admin_login(client)
    by_ref = {d["booking_id"]: d for d in client.get("/api/gps/admin-fleet").get_json()["deliveries"]}
    assert by_ref[fleet["b1"]]["location_freshness"] == "live"
    assert by_ref[fleet["b2"]]["location_freshness"] == "stale"
    assert by_ref[fleet["b3"]]["location_freshness"] == "unavailable"
    # missing GPS must never produce invented coordinates
    assert "driver_lat" not in by_ref[fleet["b3"]]
    # stale driver: last known position kept, but no live ETA is computed
    assert by_ref[fleet["b2"]]["driver_lat"] == 13.0 and "eta_min" not in by_ref[fleet["b2"]]
    assert by_ref[fleet["b1"]]["eta_min"] == 9 and by_ref[fleet["b1"]]["route_source"] == "osrm"


def test_fleet_api_does_not_leak_customer_phone_or_driver_email(client, fleet, app):
    app.bookings_collection.update_one({}, {"$set": {"phone": "5550001111", "user_email": "secret@x.com"}})
    admin_login(client)
    raw = client.get("/api/gps/admin-fleet").get_data(as_text=True)
    assert "5550001111" not in raw and "secret@x.com" not in raw and "live@x.com" not in raw


def test_osrm_routes_are_cached_between_polls(client, fleet, monkeypatch):
    calls = []
    monkeypatch.setattr(gps_engine, "osrm_route", lambda *a: calls.append(a) or {
        "distance_km": 1, "duration_min": 2, "geometry": [], "source": "osrm"})
    gps_engine._ROUTE_CACHE.clear()
    admin_login(client)
    client.get("/api/gps/admin-fleet"); client.get("/api/gps/admin-fleet")
    assert len(calls) == 1


def test_delivery_api_route_available_and_honest_unavailable_states(client, fleet):
    admin_login(client)
    ok = client.get(f"/api/fleet/delivery/{fleet['b1']}").get_json()["delivery"]["route"]
    assert ok["available"] and ok["eta_min"] == 9 and ok["geometry"]
    stale = client.get(f"/api/fleet/delivery/{fleet['b2']}").get_json()["delivery"]["route"]
    assert not stale["available"] and "stale" in stale["reason"].lower()
    nogps = client.get(f"/api/fleet/delivery/{fleet['b3']}").get_json()["delivery"]["route"]
    assert not nogps["available"]


def test_osrm_outage_is_reported_not_faked(client, fleet, monkeypatch):
    monkeypatch.setattr(gps_engine, "osrm_route", lambda *a: {
        "distance_km": 3.0, "duration_min": 6, "geometry": [], "source": "fallback"})
    gps_engine._ROUTE_CACHE.clear()
    admin_login(client)
    r = client.get(f"/api/fleet/delivery/{fleet['b1']}").get_json()["delivery"]["route"]
    assert not r["available"] and "unreachable" in r["reason"]


# ---------------- map page ----------------

def test_map_page_uses_openstreetmap_tiles_and_no_api_key_provider(client, fleet):
    admin_login(client)
    html = client.get("/admin/live_tracking").get_data(as_text=True)
    js = client.get("/static/js/fleet-map.js").get_data(as_text=True)
    assert "https://tile.openstreetmap.org/{z}/{x}/{y}.png" in js
    assert "openstreetmap.org/copyright" in js
    for provider in ("cartocdn", "mapbox", "stadiamaps", "maptiler", "googleapis.com/maps"):
        assert provider not in html and provider not in js
    assert "invalidateSize" in js


def test_driver_and_trail_pages_show_real_recorded_data(client, fleet):
    admin_login(client)
    html = client.get(f"/admin/live_tracking/driver/{fleet['live']}").get_data(as_text=True)
    assert "Live Larry" in html and "KA01AA1111" in html
    trail = client.get(f"/admin/live_tracking/driver/{fleet['live']}/trail").get_data(as_text=True)
    assert "12.97000" in trail
    nogps = client.get(f"/admin/live_tracking/delivery/{fleet['b3']}").get_data(as_text=True)
    assert "No GPS location received" in nogps or "unavailable" in nogps
