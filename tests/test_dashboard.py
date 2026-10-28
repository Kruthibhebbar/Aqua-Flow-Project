"""Customer dashboard: every number comes from the customer's own bookings."""

import json
from datetime import datetime, timedelta

import pytest

import dashboard_stats
from tests.helpers import create_user, login

TODAY = lambda: datetime.now().strftime("%Y-%m-%d")           # noqa: E731
DAY = lambda n: (datetime.now() + timedelta(days=n)).strftime("%Y-%m-%d")  # noqa: E731


def _b(app, email, status="Pending", qty=1000, date=None, **extra):
    doc = {
        "user_email": email, "fullname": "Asha Rao", "status": status, "quantity": qty,
        "water_type": "Fresh Water", "delivery_date": date or DAY(1),
        "delivery_time": "10:00 AM", "created_at": datetime.now(),
        "driver_name": "Not Assigned", "truck_number": "Not Assigned",
    }
    doc.update(extra)
    return app.bookings_collection.insert_one(doc).inserted_id


def _summary(client):
    r = client.get("/api/dashboard/summary")
    assert r.status_code == 200
    return r.get_json()


@pytest.fixture
def me(client, app):
    email, pw = create_user(app, name="Asha Rao")
    login(client, email, pw)
    return email


# ------------------------------------------------------------ empty / access
def test_new_customer_sees_zeros_and_empty_states(client, app, me):
    d = _summary(client)
    assert d["stats"] == {
        "total_bookings": 0, "pending": 0, "active": 0, "delivered": 0, "cancelled": 0,
        "total_liters": 0, "liters_delivered": 0, "today_deliveries": 0, "delivered_today": 0,
    }
    assert d["activity"] == [] and d["upcoming"] is None and d["alerts"] == []
    assert d["chart"] == {"labels": [], "values": []}
    html = client.get("/dashboard").get_data(as_text=True)
    assert "8,000" not in html and "Math.random" not in html


def test_summary_requires_login_and_returns_json_401(client):
    r = client.get("/api/dashboard/summary")
    assert r.status_code == 401 and r.get_json()["error"]


def test_dashboard_page_requires_login(client):
    assert client.get("/dashboard").status_code == 302


def test_summary_error_returns_json_500(client, app, me, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr("app.build_dashboard", boom)
    r = client.get("/api/dashboard/summary")
    assert r.status_code == 500 and "error" in r.get_json()


# ------------------------------------------------------------------ counting
def test_counts_and_liters_follow_the_rules(client, app, me):
    _b(app, me, "Pending", 1000)
    _b(app, me, "On The Way", 2000)
    _b(app, me, "Assigned", 500)
    _b(app, me, "Delivered", 1500)
    _b(app, me, "Delivered", "2,000")             # string quantity from older data
    _b(app, me, "Cancelled", 5000)                # cancelled liters not counted
    _b(app, me, "Awaiting Payment", 9000)         # unpaid attempt: not a booking
    _b(app, me, "Cancelled", 7000, auto_cancelled_reason="never paid")
    _b(app, me, "Pending", "oops")                # bad quantity -> 0, no crash
    s = _summary(client)["stats"]
    assert (s["pending"], s["active"], s["delivered"], s["cancelled"]) == (2, 2, 2, 1)
    assert s["total_bookings"] == 7
    assert s["total_liters"] == 1000 + 2000 + 500 + 1500 + 2000
    assert s["liters_delivered"] == 3500


def test_two_identical_bookings_are_two_bookings_with_distinct_feed_entries(client, app, me):
    _b(app, me, "Pending", 1000, date=DAY(2))
    _b(app, me, "Pending", 1000, date=DAY(2))
    d = _summary(client)
    assert d["stats"]["pending"] == 2 and d["stats"]["total_liters"] == 2000
    ids = [a["id"] for a in d["activity"]]
    assert len(ids) == 2 and len(set(ids)) == 2


def test_status_change_is_reflected_without_double_counting(client, app, me):
    bid = _b(app, me, "Pending", 1000)
    assert _summary(client)["stats"]["pending"] == 1
    app.bookings_collection.update_one({"_id": bid}, {"$set": {"status": "Delivered"}})
    s = _summary(client)["stats"]
    assert (s["pending"], s["delivered"], s["total_bookings"], s["total_liters"]) == (0, 1, 1, 1000)
    app.bookings_collection.update_one({"_id": bid}, {"$set": {"status": "Cancelled"}})
    s = _summary(client)["stats"]
    assert (s["delivered"], s["cancelled"], s["total_liters"], s["total_bookings"]) == (0, 1, 0, 1)


def test_other_customers_data_is_never_included(client, app, me):
    other, _ = create_user(app, email="other@example.com", name="Someone Else")
    _b(app, other, "Pending", 9999, fullname="Someone Else", date=TODAY())
    _b(app, me, "Pending", 100)
    d = _summary(client)
    assert d["stats"]["total_liters"] == 100 and d["stats"]["pending"] == 1
    assert all(a["name"] != "Someone Else" for a in d["activity"])
    html = client.get("/dashboard").get_data(as_text=True)
    assert "9999" not in html and "Someone Else" not in html


# ------------------------------------------------------------ today's count
def test_todays_deliveries_counts_scheduled_today_only(client, app, me):
    _b(app, me, "Pending", date=TODAY())
    _b(app, me, "On The Way", date=TODAY())
    _b(app, me, "Delivered", date=TODAY())        # delivered today: reported separately
    _b(app, me, "Cancelled", date=TODAY())        # not a delivery
    _b(app, me, "Pending", date=DAY(1))           # tomorrow
    _b(app, me, "Pending", date=DAY(-1))          # yesterday
    _b(app, me, "Pending", date="not-a-date")     # bad data ignored
    s = _summary(client)["stats"]
    assert s["today_deliveries"] == 2 and s["delivered_today"] == 1


def test_today_boundary_uses_the_supplied_clock(app):
    col = app.bookings_collection
    col.insert_one({"user_email": "x@e.com", "status": "Pending", "quantity": 10,
                    "delivery_date": "2026-03-10", "created_at": datetime(2026, 3, 1)})
    just_before = datetime(2026, 3, 9, 23, 59, 59)
    just_after = datetime(2026, 3, 10, 0, 0, 0)
    assert dashboard_stats.build_booking_summary(col, "x@e.com", just_before)["stats"]["today_deliveries"] == 0
    assert dashboard_stats.build_booking_summary(col, "x@e.com", just_after)["stats"]["today_deliveries"] == 1
    late = dashboard_stats.build_booking_summary(col, "x@e.com", datetime(2026, 3, 11, 0, 0, 1))
    assert late["stats"]["today_deliveries"] == 0
    assert any("past the scheduled date" in a["text"] for a in late["alerts"])


def test_app_timezone_setting_changes_today(monkeypatch):
    monkeypatch.setenv("APP_TIMEZONE", "Pacific/Kiritimati")      # UTC+14
    a = dashboard_stats.app_now()
    monkeypatch.setenv("APP_TIMEZONE", "Pacific/Pago_Pago")       # UTC-11
    b = dashboard_stats.app_now()
    assert (a - b) > timedelta(hours=24)
    monkeypatch.setenv("APP_TIMEZONE", "Not/AZone")               # bad value: safe fallback
    assert abs(dashboard_stats.app_now() - datetime.now()) < timedelta(seconds=5)


# ------------------------------------------------------------- feed / upcoming
def test_feed_is_newest_first_limited_and_excludes_unpaid(client, app, me):
    for i in range(14):
        _b(app, me, "Pending", 100 + i, created_at=datetime.now() - timedelta(hours=14 - i))
    _b(app, me, "Awaiting Payment", 1)
    d = _summary(client)
    assert len(d["activity"]) == 10 and d["activity_total"] == 14
    qty = [a["quantity"] for a in d["activity"]]
    assert qty == sorted(qty, reverse=True) and qty[0] == 113
    assert all(a["status"] != "Awaiting Payment" for a in d["activity"])
    assert "View all activity" in client.get("/dashboard").get_data(as_text=True)


def test_upcoming_delivery_is_soonest_scheduled_with_real_milestones(client, app, me):
    _b(app, me, "Pending", 500, date=DAY(5))
    soon = _b(app, me, "Assigned", 2000, date=DAY(1), driver_name="Ravi", truck_number="KA01AB1234",
              approved_at=datetime.now(), driver_assigned_at=datetime.now())
    _b(app, me, "Delivered", 700, date=DAY(0))
    u = _summary(client)["upcoming"]
    assert u["id"] == str(soon) and u["quantity"] == 2000 and u["driver_name"] == "Ravi"
    done = {s["label"]: s["done"] for s in u["timeline"]}
    assert done == {"Booked": True, "Approved": True, "Driver assigned": True, "On the way": False, "Delivered": False}


def test_unassigned_driver_is_blank_not_placeholder_text(client, app, me):
    _b(app, me, "Pending")
    assert _summary(client)["upcoming"]["driver_name"] == ""


# ------------------------------------------------------------- water quality
def _reading(app, **kw):
    doc = {"ph": 7.4, "tds": 120, "turbidity": 0.4, "measured_at": datetime.now() - timedelta(minutes=30),
           "source": "Plant sensor A"}
    doc.update(kw)
    app.water_quality_readings_collection.insert_one(doc)


def test_no_sensor_shows_reference_ranges_not_measurements(client, app, me):
    w = _summary(client)["water_quality"]
    assert w["state"] == "none" and "values" not in w and w["reference"]["ph"]
    html = client.get("/dashboard").get_data(as_text=True)
    assert "Reference ranges" in html and "not a live measurement" in html


def test_fresh_valid_reading_is_live_with_source_and_time(client, app, me):
    _reading(app)
    w = _summary(client)["water_quality"]
    assert w["state"] == "live" and w["values"] == {"ph": 7.4, "tds": 120.0, "turbidity": 0.4}
    assert w["source"] == "Plant sensor A" and w["measured_at"]


def test_stale_reading_is_not_shown_as_current(client, app, me):
    _reading(app, measured_at=datetime.now() - timedelta(hours=72))
    w = _summary(client)["water_quality"]
    assert w["state"] == "stale" and "values" not in w and w["message"] == "No recent measurement available."


@pytest.mark.parametrize("bad", [{"ph": 99}, {"ph": -1}, {"tds": "120"}, {"turbidity": float("nan")},
                                 {"tds": True}, {"turbidity": None}, {"measured_at": "yesterday"}])
def test_invalid_readings_are_rejected(client, app, me, bad):
    _reading(app, **bad)
    w = _summary(client)["water_quality"]
    assert w["state"] in ("none", "stale") and "values" not in w


def test_newest_reading_wins(client, app, me):
    _reading(app, ph=6.6, measured_at=datetime.now() - timedelta(hours=3))
    _reading(app, ph=7.9, measured_at=datetime.now() - timedelta(minutes=5))
    assert _summary(client)["water_quality"]["values"]["ph"] == 7.9


# ------------------------------------------------------ page is data-driven
def test_page_embeds_real_data_and_has_no_fake_simulation(client, app, me):
    _b(app, me, "Delivered", 2500, date=TODAY())
    html = client.get("/dashboard").get_data(as_text=True)
    assert "Math.random" not in html and "setInterval" not in html
    assert "premium grade" not in html and "ultra pure" not in html and "crystal clarity" not in html
    start = html.index("var INITIAL = ") + len("var INITIAL = ")
    payload = json.JSONDecoder().raw_decode(html[start:])[0]
    assert payload["stats"]["total_liters"] == 2500 and payload["stats"]["delivered"] == 1
    assert 'id="refreshBtn"' in html and "/api/dashboard/summary" in html and "document.hidden" in html


def test_page_escapes_hostile_names_in_embedded_data(client, app, me):
    _b(app, me, "Pending", fullname="</script><script>alert(1)</script>")
    html = client.get("/dashboard").get_data(as_text=True)
    assert "</script><script>alert(1)" not in html


# ------------------------------------------- redesign: table / weekly chart
def test_bookings_table_rows_are_own_newest_first_with_real_refs(client, app, me):
    other, _ = create_user(app, email="other2@example.com", name="Other Person")
    _b(app, other, "Pending", 777, fullname="Other Person")
    old = _b(app, me, "Delivered", 500, created_at=datetime.now() - timedelta(days=3))
    new = _b(app, me, "Pending", 1500, created_at=datetime.now() - timedelta(hours=1))
    _b(app, me, "Awaiting Payment", 42)                      # unpaid attempt: never listed
    rows = _summary(client)["bookings"]
    assert [r["id"] for r in rows] == [str(new), str(old)]
    assert all(r["ref"].startswith("AF-") and len(r["ref"]) == 9 for r in rows)
    assert rows[0]["ref"] == "AF-" + str(new)[-6:].upper()
    assert {r["quantity"] for r in rows} == {500, 1500}


def test_invoice_number_is_used_as_reference_when_issued(client, app, me):
    _b(app, me, "Delivered", 500, invoice_number="INV-2026-000007")
    assert _summary(client)["bookings"][0]["ref"] == "INV-2026-000007"


def test_weekly_booking_chart_is_zero_filled_and_counts_real_bookings(client, app, me):
    d = _summary(client)["bookings_chart"]
    assert len(d["labels"]) == 12 and d["values"] == [0] * 12          # empty -> all zeros
    _b(app, me, "Pending", created_at=datetime.now())
    _b(app, me, "Cancelled", created_at=datetime.now())
    _b(app, me, "Delivered", created_at=datetime.now() - timedelta(weeks=2))
    _b(app, me, "Awaiting Payment", created_at=datetime.now())          # not a booking
    v = _summary(client)["bookings_chart"]["values"]
    assert v[-1] == 2 and v[-3] == 1 and sum(v) == 3


def test_upcoming_delivery_carries_address_and_ref(client, app, me):
    bid = _b(app, me, "Pending", date=DAY(1), address="12 Lake Road, Mudigere")
    u = _summary(client)["upcoming"]
    assert u["address"] == "12 Lake Road, Mudigere" and u["ref"] == "AF-" + str(bid)[-6:].upper()


def test_redesigned_page_has_sidebar_links_and_unread_badge(client, app, me):
    app.notifications_collection.insert_one({"user_email": me, "read": False, "message": "hi", "created_at": datetime.now()})
    app.notifications_collection.insert_one({"user_email": "someone@else.com", "read": False, "message": "x", "created_at": datetime.now()})
    html = client.get("/dashboard").get_data(as_text=True)
    for href in ("/booking", "/history", "/profile", "/wallet", "/my-payments", "/notification", "/quick_rebook", "/logout"):
        assert f'href="{href}"' in html
    assert 'aria-label="Notifications (1 unread)"' in html          # only MY unread count
    assert 'id="recentBody"' in html and 'id="activityChart"' in html and 'id="performanceChart"' in html
