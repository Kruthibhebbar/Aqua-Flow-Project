"""
test_driver_portal.py
=====================================================
The Driver Operations Suite pages: dashboard, My Trips, Earnings (+CSV),
Analytics and Notifications. Focus areas:

  * every page renders for a brand-new driver with almost no data
  * a driver only ever sees their own data
  * numbers are real - no invented ratings/earnings - and the earnings
    figures agree with the admin finance code (same commission rule)
=====================================================
"""

import csv
import io
import itertools
import os
from datetime import datetime, timedelta

import pytest
from bson.objectid import ObjectId

from tests.helpers import login_as_driver
import driver_portal_data as data
import payment_admin

PAGES = [
    "/driver-dashboard", "/driver-trips", "/driver-earnings", "/driver-analytics",
    "/driver-documents", "/driver-profile", "/driver-notifications", "/driver-support",
]


@pytest.fixture(autouse=True)
def _clean_portal_collections(app):
    """conftest doesn't know about collections the portal added/uses."""
    for name in ("documents_collection", "driver_activity_collection", "driver_support_requests_collection"):
        getattr(app, name).delete_many({})
    yield


_oid_counter = itertools.count(0x60000000)


def _oid():
    """Booking references are the first 8 characters of the id (the same
    convention the customer and admin pages use), and those 8 characters are
    a timestamp - so ids made in the same second would share a reference.
    Give every test booking a distinct prefix."""
    return ObjectId("%08x" % next(_oid_counter) + os.urandom(8).hex())


def _main(html):
    """The page body without the shell (the top-bar bell lists recent items)."""
    start = html.index('id="af-main"')
    return html[start:html.index("</main>", start)]


def _booking(app, driver_email, status="Delivered", **overrides):
    now = datetime.now()
    doc = {
        "_id": _oid(),
        "user_email": "customer@example.com", "fullname": "Asha Rao", "phone": "9876543210",
        "driver_email": driver_email, "status": status, "delivery_fee": 500, "quantity": "1000",
        "water_type": "Drinking Water", "address": "12 Lake Road, Shivamogga",
        "delivery_date": now.strftime("%Y-%m-%d"), "delivery_time": "10:00 AM", "created_at": now,
        "latitude": 13.93, "longitude": 75.57, "delivery_otp": "482913",
    }
    if status == "Delivered":
        doc["delivered_at"] = now
    doc.update(overrides)
    return app.bookings_collection.insert_one(doc).inserted_id


def _full_driver(client, app, email="driver@example.com"):
    login_as_driver(client, app, email)
    app.drivers_collection.update_one({"email": email}, {"$set": {
        "name": "Kruthik Hebbar", "truck_number": "KA09G2123", "truck_capacity": "10000",
        "phone": "9876543210", "address": "Lake View Road, Shivamogga", "online_status": "Online",
        "joined_at": datetime(2026, 1, 5)}})
    return email


# ------------------------------------------------------------ basic access

@pytest.mark.parametrize("path", PAGES)
def test_pages_redirect_to_login_when_logged_out(client, path):
    response = client.get(path)
    assert response.status_code == 302
    assert "/driver-login" in response.headers["Location"]


@pytest.mark.parametrize("path", PAGES)
def test_every_page_renders_for_a_brand_new_driver(client, app, path):
    # login_as_driver creates a driver with ONLY email/name/approval_status.
    login_as_driver(client, app)
    response = client.get(path)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "AquaFlow" in html and "Skip to content" in html


@pytest.mark.parametrize("path", [
    "/api/driver/analytics", "/api/driver/notifications", "/api/driver/dashboard-live",
])
def test_json_endpoints_return_401_json_when_logged_out(client, path):
    response = client.get(path)
    assert response.status_code == 401
    assert response.get_json()["success"] is False


# --------------------------------------------------------------- dashboard

def test_dashboard_uses_real_name_and_vehicle(client, app):
    _full_driver(client, app)
    html = client.get("/driver-dashboard").get_data(as_text=True)
    assert "Kruthik" in html
    assert "KA09G2123" in html


def test_dashboard_never_invents_a_rating(client, app):
    email = _full_driver(client, app)
    _booking(app, email)                     # delivered, but no customer_rating stored
    html = client.get("/driver-dashboard").get_data(as_text=True)
    assert "No ratings yet" in html
    assert "4.8" not in html and "5.0" not in html


def test_dashboard_shows_a_real_rating_when_one_exists(client, app):
    email = _full_driver(client, app)
    _booking(app, email, customer_rating=4)
    _booking(app, email, customer_rating=5)
    html = client.get("/driver-dashboard").get_data(as_text=True)
    assert "4.5" in html and "From 2 ratings" in html


def test_dashboard_earnings_follow_the_commission_rule(client, app):
    email = _full_driver(client, app)
    _booking(app, email, delivery_fee=1000)   # today
    _booking(app, email, delivery_fee=500)    # today
    expected = round(1000 * payment_admin.DRIVER_SHARE_RATE) + round(500 * payment_admin.DRIVER_SHARE_RATE)
    html = client.get("/driver-dashboard").get_data(as_text=True)
    assert f"&#8377;{data.inr(expected)}" in html


def test_dashboard_never_shows_another_drivers_booking(client, app):
    email = _full_driver(client, app)
    other = _booking(app, "someone.else@example.com", status="On The Way", address="SECRET OTHER STREET")
    mine = _booking(app, email, status="On The Way", address="My Own Street 5")
    html = client.get("/driver-dashboard").get_data(as_text=True)
    assert "My Own Street 5" in html and str(mine)[:8] in html
    assert "SECRET OTHER STREET" not in html and str(other)[:8] not in html


def test_dashboard_active_delivery_offers_otp_completion_not_a_deliver_button(client, app):
    email = _full_driver(client, app)
    _booking(app, email, status="On The Way")
    html = client.get("/driver-dashboard").get_data(as_text=True)
    assert 'data-af-action="complete"' in html
    assert 'data-af-action="arrived"' in html
    assert "Mark delivered" not in html
    assert "482913" not in html              # the customer's OTP is never sent to the driver page


def test_dashboard_without_active_delivery_has_no_giant_map_area(client, app):
    _full_driver(client, app)
    html = client.get("/driver-dashboard").get_data(as_text=True)
    assert 'id="af-map"' not in html
    assert "No delivery in progress" in html


def test_dashboard_lists_requests_waiting_for_a_response(client, app):
    email = _full_driver(client, app)
    _booking(app, email, status="Assigned")
    html = client.get("/driver-dashboard").get_data(as_text=True)
    assert "Needs your response" in html
    assert 'data-af-action="accept"' in html and 'data-af-action="decline"' in html


def test_dashboard_live_signature_changes_when_a_booking_is_assigned(client, app):
    email = _full_driver(client, app)
    before = client.get("/api/driver/dashboard-live").get_json()["signature"]
    _booking(app, email, status="Assigned")
    after = client.get("/api/driver/dashboard-live").get_json()["signature"]
    assert before != after


# --------------------------------------------------------------- my trips

def _seed_trips(app, email):
    ids = {}
    ids["delivered"] = _booking(app, email, status="Delivered", delivery_date="2026-03-10")
    ids["active"] = _booking(app, email, status="On The Way", delivery_date="2026-03-12")
    ids["assigned"] = _booking(app, email, status="Assigned", delivery_date="2026-03-14")
    ids["cancelled"] = _booking(app, email, status="Cancelled", delivery_date="2026-03-09")
    return ids


def test_trips_default_view_lists_everything_except_declined(client, app):
    email = _full_driver(client, app)
    ids = _seed_trips(app, email)
    html = client.get("/driver-trips").get_data(as_text=True)
    for key in ids:
        assert str(ids[key])[:8] in html


def test_trips_tab_filters_by_status(client, app):
    email = _full_driver(client, app)
    ids = _seed_trips(app, email)
    html = client.get("/driver-trips?tab=completed").get_data(as_text=True)
    assert str(ids["delivered"])[:8] in html
    assert str(ids["active"])[:8] not in html and str(ids["cancelled"])[:8] not in html


def test_trips_search_by_reference(client, app):
    email = _full_driver(client, app)
    ids = _seed_trips(app, email)
    ref = str(ids["active"])[:8]
    html = client.get(f"/driver-trips?q={ref}").get_data(as_text=True)
    assert ref in html
    assert str(ids["assigned"])[:8] not in html or str(ids["assigned"])[:8] == ref


def test_trips_search_with_no_match_shows_an_empty_state(client, app):
    email = _full_driver(client, app)
    _seed_trips(app, email)
    html = client.get("/driver-trips?q=zzzzzzzz").get_data(as_text=True)
    assert "No trips match these filters" in html


def test_trips_date_filter_and_swapped_dates(client, app):
    email = _full_driver(client, app)
    ids = _seed_trips(app, email)
    html = client.get("/driver-trips?from=2026-03-11&to=2026-03-13").get_data(as_text=True)
    assert str(ids["active"])[:8] in html and str(ids["delivered"])[:8] not in html
    swapped = client.get("/driver-trips?from=2026-03-13&to=2026-03-11").get_data(as_text=True)
    assert "wrong way round" in swapped and str(ids["active"])[:8] in swapped


def test_trips_ignores_garbage_query_values(client, app):
    _full_driver(client, app)
    assert client.get("/driver-trips?tab=<script>&page=abc&from=nope&sort=zzz").status_code == 200


def test_trips_pagination(client, app):
    email = _full_driver(client, app)
    for i in range(12):
        _booking(app, email, delivery_date=f"2026-02-{i + 1:02d}")
    first = client.get("/driver-trips").get_data(as_text=True)
    second = client.get("/driver-trips?page=2").get_data(as_text=True)
    assert "Page 1 of 2" in first and "Page 2 of 2" in second
    assert "page=2" in first
    assert client.get("/driver-trips?page=99").status_code == 200   # clamped, not an error


def test_trips_declined_tab_shows_minimal_data_only(client, app):
    email = _full_driver(client, app)
    declined = _booking(app, "", status="Approved", rejected_by_drivers=[email], address="PRIVATE ADDRESS 9")
    app.bookings_collection.update_one({"_id": declined}, {"$unset": {"driver_email": ""}})
    html = client.get("/driver-trips?tab=declined").get_data(as_text=True)
    assert str(declined)[:8] in html and "Declined by you" in html
    assert "PRIVATE ADDRESS 9" not in html


def test_trips_show_only_the_drivers_own_bookings(client, app):
    email = _full_driver(client, app)
    other = _booking(app, "other@example.com")
    mine = _booking(app, email)
    html = client.get("/driver-trips").get_data(as_text=True)
    assert str(mine)[:8] in html and str(other)[:8] not in html


def test_trip_detail_for_own_booking_hides_otp_and_shows_contact_while_active(client, app):
    email = _full_driver(client, app)
    booking_id = _booking(app, email, status="On The Way")
    body = client.get(f"/api/driver/trips/{booking_id}").get_json()
    assert body["success"] is True
    trip = body["trip"]
    assert trip["customer_phone"] == "9876543210" and trip["customer_name"] == "Asha Rao"
    assert "482913" not in str(body)
    assert trip["share"] == round(500 * payment_admin.DRIVER_SHARE_RATE)


def test_trip_detail_hides_customer_contact_once_delivered(client, app):
    email = _full_driver(client, app)
    booking_id = _booking(app, email, status="Delivered")
    trip = client.get(f"/api/driver/trips/{booking_id}").get_json()["trip"]
    assert trip["customer_phone"] is None and trip["customer_name"] is None


def test_trip_detail_for_another_drivers_booking_is_404(client, app):
    _full_driver(client, app)
    other = _booking(app, "other@example.com")
    response = client.get(f"/api/driver/trips/{other}")
    assert response.status_code == 404 and response.get_json()["success"] is False


def test_trip_detail_with_invalid_id_is_400(client, app):
    _full_driver(client, app)
    assert client.get("/api/driver/trips/not-an-id").status_code == 400


# ----------------------------------------------------------------- earnings

def test_driver_share_matches_the_admin_wallet_formula(client, app):
    email = _full_driver(client, app)
    orders = [_booking(app, email, delivery_fee=fee) for fee in (333, 500, 1250, 999)]
    delivered = [app.bookings_collection.find_one({"_id": i}) for i in orders]
    wallet = payment_admin.get_driver_wallet_summary(app.bookings_collection, email)
    assert wallet["driver_earned_total"] == sum(data.driver_share(o) for o in delivered)


def test_earnings_page_matches_the_settlement_summary(client, app):
    email = _full_driver(client, app)
    _booking(app, email, delivery_fee=800)
    settlement = payment_admin.get_driver_settlement_summary(
        app.transactions_collection, app.bookings_collection, email)
    html = client.get("/driver-earnings").get_data(as_text=True)
    assert f"&#8377;{data.inr(settlement['driver_earned_total'])}" in html
    assert settlement["driver_earned_total"] == round(800 * payment_admin.DRIVER_SHARE_RATE)


def test_earnings_never_includes_other_drivers(client, app):
    email = _full_driver(client, app)
    _booking(app, email, delivery_fee=1000)
    other = _booking(app, "other@example.com", delivery_fee=77777)
    html = client.get("/driver-earnings").get_data(as_text=True)
    assert str(other)[:8] not in html
    assert data.inr(round(77777 * payment_admin.DRIVER_SHARE_RATE)) not in html


def test_earnings_search_and_date_filters_change_the_rows(client, app):
    email = _full_driver(client, app)
    old = _booking(app, email, delivered_at=datetime(2026, 1, 10, 9, 0), delivery_fee=400)
    new = _booking(app, email, delivered_at=datetime(2026, 2, 20, 9, 0), delivery_fee=600)
    def table(url):    # the "cash to report" list is intentionally unfiltered, so look at the table only
        html = client.get(url).get_data(as_text=True)
        start = html.index('id="deliveries"')
        return html[start:html.index("</section>", start)]

    by_date = table("/driver-earnings?from=2026-02-01&to=2026-02-28")
    assert str(new)[:8] in by_date and str(old)[:8] not in by_date
    by_ref = table(f"/driver-earnings?q={str(old)[:8]}")
    assert str(old)[:8] in by_ref and str(new)[:8] not in by_ref


def test_earnings_page_without_any_data_has_empty_states(client, app):
    login_as_driver(client, app)
    html = client.get("/driver-earnings").get_data(as_text=True)
    assert "No earnings yet" in html and "No payout requests" in html


def test_earnings_page_never_exposes_the_full_bank_account_number(client, app):
    email = _full_driver(client, app)
    app.drivers_collection.update_one({"email": email}, {"$set": {"bank_details": {
        "account_holder_name": "K Hebbar", "account_number": "123456789012", "ifsc": "HDFC0001234",
        "approval_status": "Approved"}}})
    html = client.get("/driver-earnings").get_data(as_text=True)
    assert "123456789012" not in html
    assert "9012" in html


def test_withdraw_form_only_appears_after_bank_approval(client, app):
    email = _full_driver(client, app)
    app.drivers_collection.update_one({"email": email}, {"$set": {"bank_details": {
        "account_holder_name": "K", "account_number": "123456789012", "ifsc": "HDFC0001234",
        "approval_status": "Pending"}}})
    assert "Request withdrawal" not in client.get("/driver-earnings").get_data(as_text=True)
    app.drivers_collection.update_one({"email": email}, {"$set": {"bank_details.approval_status": "Approved"}})
    assert "Request withdrawal" in client.get("/driver-earnings").get_data(as_text=True)


def test_cash_to_report_lists_only_unsubmitted_cash_deliveries(client, app):
    email = _full_driver(client, app)
    cash = _booking(app, email, delivery_fee=700)
    _booking(app, email, delivery_fee=700, payment_status="Paid (Online)")
    _booking(app, email, delivery_fee=700, payment_submitted=True, payment_submission_status="Pending Verification")
    html = client.get("/driver-earnings").get_data(as_text=True)
    assert html.count("data-cash=") == 1 and f'data-cash="{cash}"' in html


def test_csv_statement_contains_earnings_and_payouts_for_this_driver_only(client, app):
    email = _full_driver(client, app)
    mine = _booking(app, email, delivery_fee=1000)
    _booking(app, "other@example.com", delivery_fee=5000)
    app.transactions_collection.insert_one({
        "type": "settlement", "driver_email": email, "amount": 300, "status": "Completed",
        "requested_at": datetime.now(), "bank_reference": "=HYPERLINK(\"evil\")"})
    response = client.get("/driver-earnings/statement.csv")
    assert response.status_code == 200 and response.mimetype == "text/csv"
    assert "attachment" in response.headers["Content-Disposition"]
    rows = list(csv.reader(io.StringIO(response.get_data(as_text=True).lstrip("\ufeff"))))
    assert rows[0] == ["Date", "Type", "Reference", "Details", "Amount (INR)", "Status"]
    body = rows[1:]
    assert len(body) == 2
    earning = [r for r in body if r[1] == "Delivery earning"][0]
    assert earning[2] == f"#{str(mine)[:8]}" and earning[4] == str(round(1000 * payment_admin.DRIVER_SHARE_RATE))
    payout = [r for r in body if r[1] == "Payout request"][0]
    assert payout[4] == "-300"
    assert payout[2].startswith("'=")         # formula injection neutralised


def test_csv_statement_requires_login(client):
    assert client.get("/driver-earnings/statement.csv").status_code == 302


# --------------------------------------------------------------- analytics

def _analytics(client, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return client.get("/api/driver/analytics?" + query)


def test_analytics_empty_state_for_driver_without_deliveries(client, app):
    login_as_driver(client, app)
    body = _analytics(client, range="30d").get_json()
    assert body["success"] and body["empty"] is True
    assert body["kpis"]["deliveries"] == 0
    assert body["kpis"]["completion_rate"] is None and body["kpis"]["rating"] is None
    assert all(p["deliveries"] == 0 and p["earnings"] == 0 for p in body["series"])


def test_analytics_counts_only_this_drivers_real_deliveries(client, app):
    email = _full_driver(client, app)
    _booking(app, email, delivery_fee=1000, quantity="2000")
    _booking(app, email, delivery_fee=500, quantity="1000")
    _booking(app, "other@example.com", delivery_fee=9999, quantity="9999")
    kpis = _analytics(client, range="7d").get_json()["kpis"]
    assert kpis["deliveries"] == 2 and kpis["litres"] == 3000
    assert kpis["earnings"] == round(1000 * payment_admin.DRIVER_SHARE_RATE) + round(500 * payment_admin.DRIVER_SHARE_RATE)


def test_analytics_series_adds_up_to_the_totals(client, app):
    email = _full_driver(client, app)
    for days_ago in (0, 1, 1, 3):
        _booking(app, email, delivered_at=datetime.now() - timedelta(days=days_ago))
    body = _analytics(client, range="7d").get_json()
    assert sum(p["deliveries"] for p in body["series"]) == body["kpis"]["deliveries"] == 4
    assert sum(p["earnings"] for p in body["series"]) == body["kpis"]["earnings"]
    assert len(body["series"]) == 7


def test_analytics_completion_and_cancellation_use_scheduled_dates(client, app):
    email = _full_driver(client, app)
    today = datetime.now().strftime("%Y-%m-%d")
    _booking(app, email, status="Delivered", delivery_date=today)
    _booking(app, email, status="Delivered", delivery_date=today)
    _booking(app, email, status="Delivered", delivery_date=today)
    _booking(app, email, status="Cancelled", delivery_date=today)
    kpis = _analytics(client, range="7d").get_json()["kpis"]
    assert kpis["completion_rate"] == 75 and kpis["cancelled"] == 1 and kpis["cancellation_rate"] == 25


def test_analytics_average_delivery_time_needs_real_timestamps(client, app):
    email = _full_driver(client, app)
    now = datetime.now()
    _booking(app, email, started_at=now - timedelta(minutes=40), delivered_at=now)
    _booking(app, email)                                   # no started_at: must not be guessed
    kpis = _analytics(client, range="7d").get_json()["kpis"]
    assert kpis["avg_delivery_minutes"] == 40 and kpis["avg_delivery_samples"] == 1


def test_analytics_previous_period_delta_only_with_real_history(client, app):
    email = _full_driver(client, app)
    _booking(app, email)
    assert _analytics(client, range="7d").get_json()["kpis"]["deliveries_delta"] is None
    _booking(app, email, delivered_at=datetime.now() - timedelta(days=9))
    assert _analytics(client, range="7d").get_json()["kpis"]["deliveries_delta"] == 0


def test_analytics_rating_series_only_when_ratings_exist(client, app):
    email = _full_driver(client, app)
    _booking(app, email)
    assert _analytics(client, range="7d").get_json()["ratings"] == []
    _booking(app, email, customer_rating=4)
    body = _analytics(client, range="7d").get_json()
    assert body["kpis"]["rating"] == 4.0 and len(body["ratings"]) == 1


def test_analytics_custom_range_and_bucket(client, app):
    email = _full_driver(client, app)
    _booking(app, email, delivered_at=datetime(2026, 2, 10, 12, 0))
    body = _analytics(client, range="custom", **{"from": "2026-02-01", "to": "2026-02-28", "bucket": "week"}).get_json()
    assert body["range"]["bucket"] == "week" and body["kpis"]["deliveries"] == 1


@pytest.mark.parametrize("params,fragment", [
    ({"range": "bogus"}, "Unknown date range"),
    ({"range": "custom"}, "both a start and an end"),
    ({"range": "custom", "from": "2026-03-10", "to": "2026-03-01"}, "before the end date"),
    ({"range": "custom", "from": "2020-01-01", "to": "2026-01-01"}, "up to 366 days"),
    ({"range": "custom", "from": "2026-01-01", "to": "2999-01-01"}, "future"),
    ({"range": "custom", "from": "garbage", "to": "2026-01-01"}, "both a start and an end"),
])
def test_analytics_rejects_invalid_ranges_with_a_clear_message(client, app, params, fragment):
    login_as_driver(client, app)
    response = _analytics(client, **params)
    assert response.status_code == 400
    assert fragment in response.get_json()["message"]


# ----------------------------------------------------------- notifications

def _note(app, email, **overrides):
    doc = {"driver_email": email, "type": "delivery_completed", "title": "Delivery Completed",
           "message": "Done", "read": False, "created_at": datetime.now()}
    doc.update(overrides)
    return app.notifications_collection.insert_one(doc).inserted_id


def test_viewing_notifications_does_not_silently_mark_them_read(client, app):
    email = _full_driver(client, app)
    note_id = _note(app, email)
    client.get("/driver-notifications")
    assert app.notifications_collection.find_one({"_id": note_id})["read"] is False


def test_mark_single_notification_read_updates_count(client, app):
    email = _full_driver(client, app)
    first, _second = _note(app, email), _note(app, email)
    body = client.post(f"/api/driver/notifications/{first}/read").get_json()
    assert body["success"] and body["unread"] == 1
    assert app.notifications_collection.find_one({"_id": first})["read"] is True


def test_cannot_mark_another_drivers_notification_read(client, app):
    _full_driver(client, app)
    theirs = _note(app, "other@example.com")
    response = client.post(f"/api/driver/notifications/{theirs}/read")
    assert response.status_code == 404
    assert app.notifications_collection.find_one({"_id": theirs})["read"] is False


def test_invalid_notification_id_is_rejected(client, app):
    _full_driver(client, app)
    assert client.post("/api/driver/notifications/xyz/read").status_code == 400


def test_mark_all_read_only_touches_this_driver(client, app):
    email = _full_driver(client, app)
    mine = [_note(app, email) for _ in range(3)]
    theirs = _note(app, "other@example.com")
    assert client.post("/api/driver/notifications/read-all").get_json()["unread"] == 0
    assert all(app.notifications_collection.find_one({"_id": i})["read"] for i in mine)
    assert app.notifications_collection.find_one({"_id": theirs})["read"] is False


def test_notification_filters_and_unread_badge(client, app):
    email = _full_driver(client, app)
    _note(app, email, type="settlement_requested", title="Payout asked")
    _note(app, email, type="document_review", title="Doc verified", read=True)
    payments = _main(client.get("/driver-notifications?filter=payments").get_data(as_text=True))
    assert "Payout asked" in payments and "Doc verified" not in payments
    unread = _main(client.get("/driver-notifications?filter=unread").get_data(as_text=True))
    assert "Payout asked" in unread and "Doc verified" not in unread
    assert client.get("/api/driver/notifications").get_json()["unread"] == 1


def test_emergency_records_are_not_shown_in_the_inbox(client, app):
    email = _full_driver(client, app)
    _note(app, email, type="emergency", title="SOS body", message="SOS body", status="unread")
    html = client.get("/driver-notifications").get_data(as_text=True)
    assert "SOS body" not in html
    assert client.get("/api/driver/notifications").get_json()["items"] == []


def test_notifications_never_leak_other_drivers(client, app):
    _full_driver(client, app)
    _note(app, "other@example.com", title="OTHER DRIVER SECRET")
    assert "OTHER DRIVER SECRET" not in client.get("/driver-notifications").get_data(as_text=True)
    assert "OTHER DRIVER SECRET" not in client.get("/driver-dashboard").get_data(as_text=True)


def test_notifications_empty_state(client, app):
    login_as_driver(client, app)
    assert "No notifications yet" in client.get("/driver-notifications").get_data(as_text=True)
