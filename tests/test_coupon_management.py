"""
Tests for the redesigned Coupon Management page, its admin API and the
server-side redemption rules (expiry, disabled, usage limit, per-customer,
minimum amount, maximum discount, scheduling, race protection).
"""

import csv
import io
from datetime import datetime, timedelta

import pytest

import coupon_management as cm
from tests.helpers import create_user, login, submit_booking, seed_available_driver

NOW = datetime.now


def as_admin(client):
    with client.session_transaction() as s:
        s["admin"] = True


def add_coupon(app, code="SAVE10", **kw):
    doc = {"code": code, "active": True, "discount_type": "percent", "value": 10,
           "created_at": NOW()}
    doc.update(kw)
    return str(app.coupons_collection.insert_one(doc).inserted_id)


def booking(app, email, fee=1000, status="Approved"):
    return app.bookings_collection.insert_one(
        {"user_email": email, "status": status, "delivery_fee": fee}).inserted_id


def apply(client, booking_id, code):
    return client.post(f"/apply-coupon/{booking_id}", json={"code": code}).get_json()


def api_post(client, url, **kw):
    return client.post(url, headers={"X-Requested-With": "fetch"}, **kw)


# ------------------------------------------------------------------
# page + navigation + access control
# ------------------------------------------------------------------

def test_create_page_has_no_coupon_listing_even_when_coupons_exist(client, app):
    add_coupon(app, "REAL10")
    as_admin(client)
    html = client.get("/coupon-management").get_data(as_text=True)
    assert "Create New Coupon" in html and 'id="createForm"' in html
    assert "Create, manage and track promotional offers for AquaFlow customers." in html
    assert "REAL10" not in html                    # no coupon records on the creation page
    for gone in ('id="ledger"', 'id="fQ"', 'id="statusSeg"', 'id="listCount"', "All Coupons"):
        assert gone not in html.replace("View All Coupons", "")
    assert 'data-testid="view-all-coupons"' in html and 'href="/admin/coupons/list"' in html
    assert 'href="/admin-dashboard"' in html


def test_create_page_loads_with_zero_coupons(client, app):
    as_admin(client)
    r = client.get("/coupon-management")
    assert r.status_code == 200
    assert 'id="createCard"' in r.get_data(as_text=True)


def test_list_page_shows_real_coupons_and_stats(client, app):
    add_coupon(app, "REAL10")
    add_coupon(app, "OFF50", discount_type="flat", value=50, active=False)
    as_admin(client)
    r = client.get("/admin/coupons/list")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "REAL10" in html and "OFF50" in html    # embedded from the database, not hardcoded
    for present in ('id="ledger"', 'id="fQ"', 'id="statusSeg"', 'id="fType"', 'id="fFrom"', 'id="fTo"',
                    'id="detailsModal"', 'id="editModal"', 'id="confirmModal"', "Export coupons"):
        assert present in html
    assert 'id="createForm"' not in html           # creation lives on its own page
    assert 'data-testid="back-to-coupon-management"' in html and 'href="/coupon-management"' in html
    assert 'href="/admin-dashboard"' in html and "Back to Admin Dashboard" in html


def test_list_page_with_zero_coupons_renders_empty_state_hooks(client, app):
    as_admin(client)
    r = client.get("/admin/coupons/list")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "No coupons yet" in html                # empty state offers a way back to creation
    assert '"coupons": []' in html


def test_list_page_filters_and_counts_via_api(client, app):
    add_coupon(app, "ACTIVE10")
    add_coupon(app, "OFF50", discount_type="flat", value=50, active=False)
    as_admin(client)
    d = client.get("/api/admin/coupons?status=disabled").get_json()
    assert [c["code"] for c in d["coupons"]] == ["OFF50"]
    assert d["stats"]["total"] == 2 and d["stats"]["active"] == 1 and d["stats"]["disabled"] == 1
    d = client.get("/api/admin/coupons?q=active&type=percent").get_json()
    assert [c["code"] for c in d["coupons"]] == ["ACTIVE10"]


def test_create_then_list_flow(client, app):
    as_admin(client)
    assert create(client, code="FLOW20").get_json()["success"] is True
    assert "FLOW20" not in client.get("/coupon-management").get_data(as_text=True)
    assert "FLOW20" in client.get("/admin/coupons/list").get_data(as_text=True)


def test_classic_form_post_returns_to_the_right_page(client, app):
    as_admin(client)
    r = client.post("/create-coupon", data={"code": "CLASSIC5", "discount_type": "flat", "value": "5"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/coupon-management")
    cid = str(app.coupons_collection.find_one({"code": "CLASSIC5"})["_id"])
    r = client.post(f"/toggle-coupon/{cid}")
    assert r.status_code == 302 and r.headers["Location"].endswith("/admin/coupons/list")


def test_list_page_requires_admin(client, app):
    assert client.get("/admin/coupons/list").status_code == 302


@pytest.mark.parametrize("url", ["/coupon-management", "/admin/coupons/list"])
def test_back_to_dashboard_is_a_direct_link_not_history(client, app, url):
    as_admin(client)
    html = client.get(url).get_data(as_text=True)
    assert 'href="/admin-dashboard"' in html and "Back to Admin Dashboard" in html
    assert 'href="/booking-management"' in html or "Booking Management" in html
    for banned in ("history.back", "history.go", "document.referrer"):
        assert banned not in html


ADMIN_ENDPOINTS = [
    ("get", "/coupon-management"), ("get", "/admin/coupons/list"), ("get", "/api/admin/coupons"), ("get", "/api/admin/coupons/abc"),
    ("get", "/export-coupons"), ("post", "/create-coupon"), ("post", "/update-coupon/abc"),
    ("post", "/toggle-coupon/abc"), ("post", "/delete-coupon/abc"),
]


@pytest.mark.parametrize("method,url", ADMIN_ENDPOINTS)
def test_admin_endpoints_reject_anonymous_and_customers(client, app, method, url):
    anon = getattr(client, method)(url, headers={"X-Requested-With": "fetch"})
    assert anon.status_code in (302, 401)
    email, pw = create_user(app)
    login(client, email, pw)
    cust = getattr(client, method)(url, headers={"X-Requested-With": "fetch"})
    assert cust.status_code in (302, 401)


# ------------------------------------------------------------------
# creation, duplicates, validation
# ------------------------------------------------------------------

def create(client, **form):
    data = {"code": "WELCOME100", "discount_type": "flat", "value": "100"}
    data.update(form)
    return api_post(client, "/create-coupon", data=data)


def test_create_coupon_normalises_code_and_stores_options(client, app):
    as_admin(client)
    r = create(client, code="  welcome100 ", one_time_per_user="on", usage_limit="50", min_amount="300")
    assert r.get_json()["success"] is True
    doc = app.coupons_collection.find_one({})
    assert doc["code"] == "WELCOME100" and doc["one_time_per_user"] is True
    assert doc["usage_limit"] == 50 and doc["min_amount"] == 300 and doc["active"] is True


@pytest.mark.parametrize("second", ["welcome100", "WELCOME100", "  Welcome100  "])
def test_duplicate_codes_rejected_case_insensitively(client, app, second):
    as_admin(client)
    assert create(client).get_json()["success"] is True
    r = create(client, code=second)
    assert r.status_code == 409 and r.get_json()["success"] is False
    assert app.coupons_collection.count_documents({}) == 1


@pytest.mark.parametrize("form,fragment", [
    ({"code": "ab"}, "3-20"),
    ({"code": "bad code!"}, "3-20"),
    ({"discount_type": "bogus"}, "flat or percent"),
    ({"value": ""}, "required"),
    ({"value": "0"}, "greater than 0"),
    ({"value": "-5"}, "greater than 0"),
    ({"value": "abc"}, "number"),
    ({"value": "nan"}, "finite"),
    ({"discount_type": "percent", "value": "101"}, "100%"),
    ({"expiry_at": "2000-01-01T10:00"}, "future"),
    ({"expiry_at": "garbage"}, "valid expiry"),
    ({"usage_limit": "0"}, "at least 1"),
    ({"usage_limit": "2.5"}, "whole number"),
    ({"min_amount": "-1"}, "negative"),
    ({"max_discount": "50"}, "only applies to percentage"),
])
def test_create_validation_is_enforced_on_the_server(client, app, form, fragment):
    as_admin(client)
    r = create(client, **form)
    assert r.status_code == 400
    assert fragment.lower() in r.get_json()["message"].lower()
    assert app.coupons_collection.count_documents({}) == 0


def test_start_must_be_before_expiry(client, app):
    as_admin(client)
    t = NOW()
    r = create(client, starts_at=(t + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M"),
               expiry_at=(t + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M"))
    assert r.status_code == 400 and "before" in r.get_json()["message"].lower()


# ------------------------------------------------------------------
# statuses + stats (each coupon in exactly one status)
# ------------------------------------------------------------------

def seed_one_of_each(app):
    t = NOW()
    add_coupon(app, "ACTIVE1")
    add_coupon(app, "SCHED1", starts_at=t + timedelta(days=2))
    add_coupon(app, "EXPIRED1", expiry_at=t - timedelta(days=1))
    add_coupon(app, "OFF1", active=False, expiry_at=t - timedelta(days=1))   # disabled wins over expired
    add_coupon(app, "FULL1", usage_limit=1)
    app.coupon_redemptions_collection.insert_one({
        "user_email": "a@x.com", "code": "FULL1", "booking_id": str(booking(app, "a@x.com")),
        "redeemed_at": t, "discount": 100})


def test_stats_are_consistent_and_use_real_data(client, app):
    seed_one_of_each(app)
    as_admin(client)
    s = client.get("/api/admin/coupons").get_json()["stats"]
    assert (s["total"], s["active"], s["scheduled"], s["expired"], s["disabled"], s["exhausted"]) == (5, 1, 1, 1, 1, 1)
    assert s["total"] == s["active"] + s["scheduled"] + s["expired"] + s["disabled"] + s["exhausted"]
    assert s["redemptions"] == 1 and s["discount_given"] == 100


def test_cancelled_booking_redemption_does_not_count(client, app):
    add_coupon(app, "LIM1", usage_limit=1)
    app.coupon_redemptions_collection.insert_one({
        "user_email": "a@x.com", "code": "LIM1",
        "booking_id": str(booking(app, "a@x.com", status="Cancelled")), "redeemed_at": NOW(), "discount": 100})
    as_admin(client)
    s = client.get("/api/admin/coupons").get_json()
    assert s["stats"]["redemptions"] == 0 and s["stats"]["discount_given"] == 0
    assert s["coupons"][0]["status"] == "active"          # slot is free again


# ------------------------------------------------------------------
# search / filter / sort
# ------------------------------------------------------------------

def test_search_filter_and_sort(client, app):
    t = NOW()
    add_coupon(app, "FLAT50", discount_type="flat", value=50, created_at=t - timedelta(days=3), expiry_at=t + timedelta(days=5))
    add_coupon(app, "PCT20", discount_type="percent", value=20, created_at=t - timedelta(days=2), expiry_at=t + timedelta(days=30))
    add_coupon(app, "OLD5", discount_type="percent", value=4, created_at=t - timedelta(days=9), expiry_at=t - timedelta(days=1))
    as_admin(client)
    get = lambda q: [c["code"] for c in client.get("/api/admin/coupons?" + q).get_json()["coupons"]]
    assert get("q=flat") == ["FLAT50"]                       # search is case-insensitive
    assert sorted(get("status=expired")) == ["OLD5"]
    assert sorted(get("type=percent")) == ["OLD5", "PCT20"]
    assert get("sort=newest") == ["PCT20", "FLAT50", "OLD5"]
    assert get("sort=oldest") == ["OLD5", "FLAT50", "PCT20"]
    assert get("sort=discount_high") == ["PCT20", "FLAT50", "OLD5"]   # judged on a Rs.1000 booking: 200, 50, 40
    assert get("sort=expiry_soon")[0] == "OLD5"
    frm = (t + timedelta(days=20)).strftime("%Y-%m-%d"); to = (t + timedelta(days=40)).strftime("%Y-%m-%d")
    assert get(f"expiry_from={frm}&expiry_to={to}") == ["PCT20"]
    assert client.get("/api/admin/coupons?status=nope").status_code == 400
    assert client.get("/api/admin/coupons?sort=nope").status_code == 400
    assert client.get("/api/admin/coupons?expiry_from=x").status_code == 400


# ------------------------------------------------------------------
# edit / enable / disable / delete
# ------------------------------------------------------------------

def test_edit_safe_fields_only(client, app):
    cid = add_coupon(app, "EDIT1", usage_limit=10)
    as_admin(client)
    r = api_post(client, f"/update-coupon/{cid}", json={"value": "15", "code": "HACKED", "discount_type": "flat", "active": False})
    assert r.get_json()["success"] is True
    doc = app.coupons_collection.find_one({"code": "EDIT1"})
    assert doc["value"] == 15 and doc["discount_type"] == "percent" and doc["active"] is True   # immutable keys ignored
    assert app.coupons_collection.count_documents({"code": "HACKED"}) == 0


def test_edit_cannot_drop_usage_limit_below_existing_redemptions(client, app):
    cid = add_coupon(app, "EDIT2", usage_limit=5)
    for i in range(3):
        app.coupon_redemptions_collection.insert_one({
            "user_email": f"u{i}@x.com", "code": "EDIT2", "booking_id": str(booking(app, f"u{i}@x.com")), "redeemed_at": NOW(), "discount": 10})
    as_admin(client)
    r = api_post(client, f"/update-coupon/{cid}", json={"usage_limit": "2"})
    assert r.status_code == 400 and "3 redemption" in r.get_json()["message"]
    assert api_post(client, f"/update-coupon/{cid}", json={"usage_limit": "3"}).get_json()["success"] is True


def test_edit_validation_and_not_found(client, app):
    cid = add_coupon(app, "EDIT3")
    as_admin(client)
    assert api_post(client, f"/update-coupon/{cid}", json={"value": "101"}).status_code == 400
    assert api_post(client, f"/update-coupon/{cid}", json={"value": "0"}).status_code == 400
    assert api_post(client, f"/update-coupon/{cid}", json={"nothing": 1}).status_code == 400
    assert api_post(client, "/update-coupon/not-an-id", json={"value": "5"}).status_code == 404


def test_disabled_coupon_cannot_be_redeemed_and_history_is_kept(client, app):
    cid = add_coupon(app, "OFFME")
    email, pw = create_user(app); login(client, email, pw)
    b1 = booking(app, email)
    assert apply(client, b1, "OFFME")["success"] is True
    as_admin(client)
    r = api_post(client, f"/toggle-coupon/{cid}", json={"active": False})
    assert r.get_json()["active"] is False
    assert app.coupon_redemptions_collection.count_documents({"code": "OFFME"}) == 1      # history kept
    b2 = booking(app, email)
    out = apply(client, b2, "OFFME")
    assert out["success"] is False and "inactive" in out["message"].lower()                # enforced on the server
    api_post(client, f"/toggle-coupon/{cid}", json={"active": True})
    assert apply(client, b2, "OFFME")["success"] is True


def test_delete_only_when_never_redeemed(client, app):
    used = add_coupon(app, "USED1"); fresh = add_coupon(app, "FRESH1")
    app.coupon_redemptions_collection.insert_one({"user_email": "a@x.com", "code": "USED1", "booking_id": "x", "redeemed_at": NOW()})
    as_admin(client)
    r = api_post(client, f"/delete-coupon/{used}")
    assert r.status_code == 409 and app.coupons_collection.count_documents({"code": "USED1"}) == 1
    assert api_post(client, f"/delete-coupon/{fresh}").get_json()["success"] is True
    assert app.coupons_collection.count_documents({"code": "FRESH1"}) == 0


# ------------------------------------------------------------------
# details / redemption history / export
# ------------------------------------------------------------------

def test_details_show_real_history_with_masked_customers(client, app):
    cid = add_coupon(app, "HIST1")
    email, pw = create_user(app, email="jane.doe@example.com"); login(client, email, pw)
    b = booking(app, email, 1000)
    assert apply(client, b, "HIST1")["success"] is True
    as_admin(client)
    d = client.get(f"/api/admin/coupons/{cid}").get_json()
    assert d["coupon"]["redemptions"] == 1 and d["coupon"]["discount_given"] == 100
    row = d["redemptions"][0]
    assert row["booking_ref"] == str(b)[-6:].upper() and row["discount"] == 100
    assert "jane.doe@example.com" not in str(d) and row["customer"].startswith("j***")
    assert client.get("/api/admin/coupons/zzz").status_code == 404


def test_csv_export_uses_real_records_respects_filters_and_is_formula_safe(client, app):
    add_coupon(app, "KEEP10")
    add_coupon(app, "=EVIL1", discount_type="flat", value=5)
    cid = add_coupon(app, "OTHER5")
    app.coupon_redemptions_collection.insert_one({"user_email": "a@x.com", "code": "KEEP10", "booking_id": "abcdef123456", "redeemed_at": NOW(), "discount": 42})
    as_admin(client)
    r = client.get("/export-coupons?q=keep")
    assert r.mimetype == "text/csv" and "attachment" in r.headers["Content-Disposition"]
    rows = list(csv.reader(io.StringIO(r.get_data(as_text=True).lstrip("﻿"))))
    # a redemption whose booking record is missing is counted (conservatively) but its discount is never guessed
    assert [x[0] for x in rows[1:]] == ["KEEP10"] and rows[1][5] == "1" and rows[1][6] == "0.0"
    full = client.get("/export-coupons").get_data(as_text=True)
    assert "'=EVIL1" in full and "\n=EVIL1" not in full                              # formula injection neutralised
    red = client.get("/export-coupons?kind=redemptions").get_data(as_text=True)
    assert "KEEP10" in red and "a@x.com" not in red                                  # masked
    assert client.get("/export-coupons?kind=bogus").status_code == 400
    assert cid


# ------------------------------------------------------------------
# redemption rules (server side)
# ------------------------------------------------------------------

def test_expired_and_scheduled_coupons_rejected(client, app):
    t = NOW()
    add_coupon(app, "EXP1", expiry_at=t - timedelta(minutes=1))
    add_coupon(app, "LATER1", starts_at=t + timedelta(hours=1))
    email, pw = create_user(app); login(client, email, pw)
    assert "expired" in apply(client, booking(app, email), "EXP1")["message"].lower()
    assert "isn't active yet" in apply(client, booking(app, email), "LATER1")["message"]


def test_usage_limit_is_enforced(client, app):
    add_coupon(app, "ONLY1", usage_limit=1)
    a, pw = create_user(app, email="a@x.com"); login(client, a, pw)
    assert apply(client, booking(app, a), "ONLY1")["success"] is True
    client.get("/logout")
    b, pw = create_user(app, email="b@x.com"); login(client, b, pw)
    out = apply(client, booking(app, b), "ONLY1")
    assert out["success"] is False and "usage limit" in out["message"].lower()


def test_minimum_amount_and_max_discount_enforced(client, app):
    add_coupon(app, "MIN500", min_amount=500)
    add_coupon(app, "CAP50", value=50, max_discount=80)
    email, pw = create_user(app); login(client, email, pw)
    low = apply(client, booking(app, email, 300), "MIN500")
    assert low["success"] is False and "at least" in low["message"]
    assert apply(client, booking(app, email, 600), "MIN500")["success"] is True
    capped = apply(client, booking(app, email, 1000), "CAP50")        # 50% of 1000 = 500 -> capped at 80
    assert capped["discount"] == 80 and capped["new_amount"] == 920


def test_flat_discount_never_makes_total_negative(client, app):
    add_coupon(app, "BIG500", discount_type="flat", value=500)
    email, pw = create_user(app); login(client, email, pw)
    b = booking(app, email, 300)
    out = apply(client, b, "BIG500")
    assert out["success"] is True and out["new_amount"] == 0 and out["discount"] == 300
    assert app.bookings_collection.find_one({"_id": b})["delivery_fee"] == 0


def test_one_use_per_customer_survives_other_bookings(client, app):
    add_coupon(app, "ONCE1", one_time_per_user=True)
    a, pw = create_user(app, email="a@x.com"); login(client, a, pw)
    assert apply(client, booking(app, a), "ONCE1")["success"] is True
    assert "already used" in apply(client, booking(app, a), "ONCE1")["message"].lower()
    client.get("/logout")
    b, pw = create_user(app, email="b@x.com"); login(client, b, pw)
    assert apply(client, booking(app, b), "ONCE1")["success"] is True      # a different customer is fine


def test_remove_coupon_restores_amount_and_frees_one_time_use(client, app):
    add_coupon(app, "FREE1", one_time_per_user=True)
    email, pw = create_user(app); login(client, email, pw)
    b = booking(app, email, 1000)
    assert apply(client, b, "FREE1")["new_amount"] == 900
    assert client.post(f"/remove-coupon/{b}").get_json()["new_amount"] == 1000
    assert apply(client, b, "FREE1")["success"] is True


def test_legacy_lowercase_coupon_codes_still_work(client, app):
    add_coupon(app, "Welcome100", discount_type="flat", value=100)         # created by an older version
    email, pw = create_user(app); login(client, email, pw)
    assert apply(client, booking(app, email), "welcome100")["success"] is True
    as_admin(client)
    c = client.get("/api/admin/coupons").get_json()["coupons"][0]
    assert c["redemptions"] == 1                                            # analytics join on the normalised code


def test_record_redemption_race_protection(app):
    add_coupon(app, "RACE1", usage_limit=1)
    bk = app.bookings_collection
    ids = [str(bk.insert_one({"user_email": f"{i}@x.com", "status": "Approved"}).inserted_id) for i in range(2)]
    args = dict(coupons_collection=app.coupons_collection, bookings_collection=bk)
    assert cm.record_redemption(app.coupon_redemptions_collection, "0@x.com", "RACE1", ids[0], discount=10, **args) is True
    assert cm.record_redemption(app.coupon_redemptions_collection, "1@x.com", "RACE1", ids[1], discount=10, **args) is False
    assert app.coupon_redemptions_collection.count_documents({"code": "RACE1"}) == 1      # loser rolled back

    add_coupon(app, "RACE2", one_time_per_user=True)
    assert cm.record_redemption(app.coupon_redemptions_collection, "0@x.com", "RACE2", ids[0], coupons_collection=app.coupons_collection) is True
    assert cm.record_redemption(app.coupon_redemptions_collection, "0@x.com", "RACE2", ids[1], coupons_collection=app.coupons_collection) is False


# ------------------------------------------------------------------
# booking-page flow keeps totals consistent
# ------------------------------------------------------------------

def test_booking_with_coupon_discounts_total_and_records_redemption(client, app):
    add_coupon(app, "BOOK10", value=10)
    email, pw = create_user(app); login(client, email, pw); seed_available_driver(app)
    submit_booking(client, coupon_code="book10")
    b = app.bookings_collection.find_one({})
    assert b["coupon"]["code"] == "BOOK10" and b["pre_coupon_amount"] > b["delivery_fee"]
    assert round(b["pre_coupon_amount"] - b["delivery_fee"], 2) == b["coupon"]["discount"]
    red = app.coupon_redemptions_collection.find_one({"code": "BOOK10"})
    assert red["discount"] == b["coupon"]["discount"]


def test_booking_loses_race_for_last_coupon_use_gets_full_price(client, app, monkeypatch):
    add_coupon(app, "LAST1", value=10, usage_limit=1)
    email, pw = create_user(app); login(client, email, pw); seed_available_driver(app)
    monkeypatch.setattr(app_mod(app), "record_redemption", lambda *a, **k: False)   # someone else claimed it first
    submit_booking(client, coupon_code="LAST1")
    b = app.bookings_collection.find_one({})
    assert "coupon" not in b and "pre_coupon_amount" not in b                     # discount taken back off
    assert b["delivery_fee"] > 0


def app_mod(app):
    return app
