"""
test_driver_workflow_security.py
=====================================================
Locks down the two things the driver portal must never get wrong:

  1. LOGIN - the emailed OTP can't be skipped, read, or brute-forced.
  2. DELIVERY WORKFLOW - a delivery only moves forward one valid step at a
     time, only for its own driver, and only becomes "Delivered" through the
     customer's OTP (never via a button / API status field), exactly once.
=====================================================
"""

import itertools
import os
import re
from datetime import datetime, timedelta

import bcrypt
import pytest
from bson.objectid import ObjectId

from tests.helpers import login_as_driver

_oid_counter = itertools.count(0x62000000)


def _oid():
    return ObjectId("%08x" % next(_oid_counter) + os.urandom(8).hex())


def _booking(app, email, status, **extra):
    doc = {"_id": _oid(), "user_email": "customer@example.com", "driver_email": email, "status": status,
           "delivery_otp": "482913", "otp_attempts": 0, "delivery_fee": 500, "created_at": datetime.now()}
    doc.update(extra)
    return app.bookings_collection.insert_one(doc).inserted_id


def _status(app, booking_id):
    return app.bookings_collection.find_one({"_id": booking_id})["status"]


# ================================================================ login

@pytest.fixture
def login_env(app, monkeypatch):
    """A real approved driver with a bcrypt password, and a capture of the OTP email."""
    import driver_auth
    sent = {}
    monkeypatch.setattr(driver_auth, "send_mail_capped",
                        lambda app_, mail_, msg, timeout_seconds=6: sent.update(body=msg.body) or True)
    app.drivers_collection.insert_one({
        "email": "login@example.com", "name": "Login Driver", "approval_status": "Approved",
        "online_status": "Offline", "password": bcrypt.hashpw(b"Passw0rd!", bcrypt.gensalt())})
    return sent


def _password_step(client):
    return client.post("/driver-login", data={"email": "login@example.com", "password": "Passw0rd!"})


def _otp(sent):
    return re.search(r"OTP Is: (\d{6})", sent["body"]).group(1)


def _wrong(otp):
    return "000000" if otp != "000000" else "111111"


def test_password_step_alone_does_not_log_the_driver_in(client, login_env):
    response = _password_step(client)
    assert response.status_code == 302 and "/verify-driver-otp" in response.headers["Location"]
    with client.session_transaction() as sess:
        assert "driver_email" not in sess and not sess.get("driver_logged_in")
        assert sess["pending_driver_email"] == "login@example.com"
    assert client.get("/driver-dashboard").status_code == 302          # still sent to log in
    assert client.get("/driver-trips").status_code == 302
    assert client.get("/api/driver/dashboard-live").status_code == 401


def test_driver_is_not_marked_online_before_the_otp_is_verified(client, app, login_env):
    _password_step(client)
    assert app.drivers_collection.find_one({"email": "login@example.com"})["online_status"] == "Offline"


def test_correct_otp_completes_the_login(client, app, login_env):
    _password_step(client)
    response = client.post("/verify-driver-otp", data={"otp": _otp(login_env)})
    assert response.status_code == 302 and "/driver-dashboard" in response.headers["Location"]
    with client.session_transaction() as sess:
        assert sess["driver_email"] == "login@example.com" and sess["driver_logged_in"] is True
        assert "pending_driver_email" not in sess
    assert client.get("/driver-dashboard").status_code == 200
    assert app.drivers_collection.find_one({"email": "login@example.com"})["online_status"] == "Online"


def test_the_otp_is_never_readable_from_the_session_cookie(client, login_env):
    _password_step(client)
    otp = _otp(login_env)
    with client.session_transaction() as sess:
        assert otp not in [str(v) for v in sess.values()]
        assert "driver_otp" not in sess


def test_wrong_otp_does_not_log_in_and_is_counted_on_the_driver_record(client, app, login_env):
    _password_step(client)
    response = client.post("/verify-driver-otp", data={"otp": _wrong(_otp(login_env))})
    assert "error=invalid_otp" in response.headers["Location"]
    assert client.get("/driver-dashboard").status_code == 302
    assert app.drivers_collection.find_one({"email": "login@example.com"})["otp_failed_attempts"] == 1


def test_five_wrong_codes_lock_the_account_even_if_the_cookie_is_discarded(client, app, login_env):
    _password_step(client)
    wrong = _wrong(_otp(login_env))
    for _ in range(4):
        assert client.post("/verify-driver-otp", data={"otp": wrong}).status_code == 302
    locked = client.post("/verify-driver-otp", data={"otp": wrong})
    assert locked.status_code == 429
    fresh = app.app.test_client()                                    # attacker throws the cookie away...
    again = fresh.post("/driver-login", data={"email": "login@example.com", "password": "Passw0rd!"})
    assert again.status_code == 429 and b"Too many" in again.data    # ...but the lock lives on the driver record
    assert "driver_email" not in dict(fresh.get("/driver-dashboard").headers)


def test_the_lock_expires(client, app, login_env):
    app.drivers_collection.update_one({"email": "login@example.com"},
                                      {"$set": {"otp_locked_until": datetime.now() - timedelta(minutes=1)}})
    assert _password_step(client).status_code == 302


def test_resending_the_code_does_not_reset_the_attempt_counter(client, app, login_env):
    _password_step(client)
    client.post("/verify-driver-otp", data={"otp": _wrong(_otp(login_env))})
    client.post("/resend-otp")
    assert app.drivers_collection.find_one({"email": "login@example.com"})["otp_failed_attempts"] == 1
    assert client.post("/verify-driver-otp", data={"otp": _otp(login_env)}).status_code == 302


def test_old_code_stops_working_after_a_resend(client, login_env):
    _password_step(client)
    old = _otp(login_env)
    client.post("/resend-otp")
    new = _otp(login_env)
    if old != new:
        assert "error=invalid_otp" in client.post("/verify-driver-otp", data={"otp": old}).headers["Location"]


def test_an_expired_code_is_refused(client, login_env):
    _password_step(client)
    otp = _otp(login_env)
    with client.session_transaction() as sess:
        sess["otp_generated_at"] = (datetime.now() - timedelta(minutes=11)).timestamp()
    assert "error=expired" in client.post("/verify-driver-otp", data={"otp": otp}).headers["Location"]


def test_otp_page_without_a_pending_login_goes_back_to_login(client):
    assert "/driver-login" in client.get("/verify-driver-otp").headers["Location"]


def test_an_unapproved_driver_cannot_get_a_session(client, app, login_env):
    app.drivers_collection.update_one({"email": "login@example.com"}, {"$set": {"approval_status": "Pending"}})
    response = _password_step(client)
    assert response.status_code == 200 and b"Waiting For Admin Approval" in response.data
    with client.session_transaction() as sess:
        assert "driver_email" not in sess and "pending_driver_email" not in sess


# ===================================================== status transitions

def _set(client, booking_id, status, **extra):
    return client.post(f"/api/update-delivery-status/{booking_id}", json={"status": status, **extra})


@pytest.mark.parametrize("from_status", ["Accepted", "On The Way", "Arrived"])
def test_status_api_refuses_to_mark_delivered_without_the_otp(client, app, from_status):
    email = login_as_driver(client, app)
    booking = _booking(app, email, from_status)
    response = _set(client, booking, "Delivered")
    assert response.status_code == 400 and "OTP" in response.get_json()["message"]
    assert _status(app, booking) == from_status
    assert app.drivers_collection.find_one({"email": email}).get("completed_deliveries", 0) == 0


def test_status_api_allows_exactly_the_two_forward_steps(client, app):
    email = login_as_driver(client, app)
    booking = _booking(app, email, "Accepted")
    assert _set(client, booking, "On The Way", eta="12 min").get_json()["success"] is True
    assert _status(app, booking) == "On The Way"
    assert app.bookings_collection.find_one({"_id": booking})["started_at"]
    assert _set(client, booking, "Arrived").get_json()["success"] is True
    assert _status(app, booking) == "Arrived"
    assert app.bookings_collection.find_one({"_id": booking})["arrived_at"]


@pytest.mark.parametrize("current,target", [
    ("Accepted", "Arrived"), ("Assigned", "On The Way"), ("Assigned", "Arrived"),
    ("Delivered", "Arrived"), ("Delivered", "On The Way"), ("Cancelled", "On The Way"), ("Arrived", "On The Way"),
])
def test_status_api_cannot_skip_or_go_backwards(client, app, current, target):
    email = login_as_driver(client, app)
    booking = _booking(app, email, current)
    response = _set(client, booking, target)
    assert response.status_code == 409
    assert _status(app, booking) == current


@pytest.mark.parametrize("target", ["Cancelled", "Approved", "Assigned", "Accepted", "", "pwned", "delivered"])
def test_status_api_rejects_statuses_a_driver_may_not_set(client, app, target):
    email = login_as_driver(client, app)
    booking = _booking(app, email, "On The Way")
    assert _set(client, booking, target).status_code == 400
    assert _status(app, booking) == "On The Way"


def test_status_api_ignores_a_double_submit(client, app):
    email = login_as_driver(client, app)
    booking = _booking(app, email, "Accepted")
    assert _set(client, booking, "On The Way").status_code == 200
    assert _set(client, booking, "On The Way").status_code == 409


def test_status_api_cannot_touch_another_drivers_delivery(client, app):
    login_as_driver(client, app)
    theirs = _booking(app, "other@example.com", "Accepted")
    assert _set(client, theirs, "On The Way").status_code == 404
    assert _status(app, theirs) == "Accepted"


def test_status_api_input_hardening(client, app):
    email = login_as_driver(client, app)
    assert _set(client, "not-an-id", "Arrived").status_code == 400
    booking = _booking(app, email, "Accepted")
    assert client.post(f"/api/update-delivery-status/{booking}", data="garbage",
                       content_type="text/plain").status_code == 400
    assert app.app.test_client().post(f"/api/update-delivery-status/{booking}", json={"status": "Arrived"}).status_code == 401


def test_arrived_route_is_post_only_and_only_valid_from_on_the_way(client, app):
    email = login_as_driver(client, app)
    booking = _booking(app, email, "On The Way")
    assert client.get(f"/arrived-location/{booking}").status_code == 405      # a link can't change state
    assert _status(app, booking) == "On The Way"
    assert client.post(f"/arrived-location/{booking}").get_json()["success"] is True
    assert _status(app, booking) == "Arrived"
    note = app.notifications_collection.find_one({"user_email": "customer@example.com", "type": "arrived"})
    assert note is not None


@pytest.mark.parametrize("status", ["Accepted", "Assigned", "Delivered", "Cancelled"])
def test_arrived_route_cannot_rewrite_other_statuses(client, app, status):
    email = login_as_driver(client, app)
    booking = _booking(app, email, status)
    assert client.post(f"/arrived-location/{booking}").status_code == 409
    assert _status(app, booking) == status


def test_start_delivery_gives_a_helpful_message_for_each_wrong_state(client, app):
    email = login_as_driver(client, app)
    assigned = _booking(app, email, "Assigned")
    response = client.post(f"/start-delivery/{assigned}")
    assert response.status_code == 400 and "accept this order" in response.get_json()["message"]
    accepted = _booking(app, email, "Accepted")
    assert client.post(f"/start-delivery/{accepted}").get_json()["success"] is True
    again = client.post(f"/start-delivery/{accepted}")
    assert again.status_code == 400 and "already been started" in again.get_json()["message"]
    assert client.post(f"/start-delivery/{_booking(app, 'other@example.com', 'Accepted')}").status_code == 404


def test_online_offline_get_links_no_longer_change_state(client, app):
    email = login_as_driver(client, app)
    app.drivers_collection.update_one({"email": email}, {"$set": {"online_status": "Online"}})
    assert client.get("/driver-offline").status_code in (404, 405)
    assert app.drivers_collection.find_one({"email": email})["online_status"] == "Online"


def test_driver_status_api_validates_its_input(client, app):
    email = login_as_driver(client, app)
    assert client.post("/api/driver-status", json={"status": "Busy"}).status_code == 400
    assert client.post("/api/driver-status", json={}).status_code == 400
    assert client.post("/api/driver-status", json={"status": "offline"}).get_json()["status"] == "Offline"
    assert app.drivers_collection.find_one({"email": email})["online_status"] == "Offline"
    assert client.post("/api/driver-status", json={"status": "Online"}).get_json()["status"] == "Online"


# ===================================================== OTP completion

def _verify(client, booking_id, otp):
    return client.post(f"/verify-delivery-otp/{booking_id}", data={"otp": otp})


@pytest.mark.parametrize("status", ["Assigned", "Accepted", "Cancelled"])
def test_otp_cannot_complete_a_delivery_that_has_not_started(client, app, status):
    email = login_as_driver(client, app)
    booking = _booking(app, email, status)
    response = _verify(client, booking, "482913")
    assert response.status_code == 400 and response.get_json()["success"] is False
    assert _status(app, booking) == status
    assert app.bookings_collection.find_one({"_id": booking})["otp_attempts"] == 0


def test_a_delivery_can_only_be_completed_once_so_earnings_are_not_doubled(client, app):
    email = login_as_driver(client, app)
    booking = _booking(app, email, "On The Way")
    assert _verify(client, booking, "482913").get_json()["success"] is True
    second = _verify(client, booking, "482913")
    assert second.status_code == 409 and "already" in second.get_json()["message"]
    driver = app.drivers_collection.find_one({"email": email})
    assert driver["completed_deliveries"] == 1 and driver["total_earnings"] == 500
    assert app.earnings_collection.count_documents({"booking_id": str(booking)}) == 1


def test_wrong_codes_are_counted_atomically_and_report_remaining_attempts(client, app):
    email = login_as_driver(client, app)
    booking = _booking(app, email, "Arrived")
    for expected_left in (4, 3, 2, 1):
        message = _verify(client, booking, "000000").get_json()["message"]
        assert f"{expected_left} attempts remaining" in message
    last = _verify(client, booking, "000000").get_json()["message"]
    assert "0 attempts remaining" in last
    locked = _verify(client, booking, "482913")                       # even the RIGHT code is refused now
    assert locked.status_code == 400 and "too many" in locked.get_json()["message"].lower()
    assert _status(app, booking) == "Arrived"
    assert app.bookings_collection.find_one({"_id": booking})["otp_attempts"] == 5


def test_completion_resets_attempts_and_tolerates_stray_whitespace(client, app):
    email = login_as_driver(client, app)
    booking = _booking(app, email, "On The Way", otp_attempts=3)
    assert _verify(client, booking, " 482913 ").get_json()["success"] is True
    saved = app.bookings_collection.find_one({"_id": booking})
    assert saved["status"] == "Delivered" and saved["otp_attempts"] == 0 and saved["delivered_at"]


def test_otp_endpoint_input_hardening(client, app):
    email = login_as_driver(client, app)
    assert _verify(client, "nope", "123456").status_code == 400
    assert _verify(client, _booking(app, "other@example.com", "On The Way"), "482913").status_code == 404
    booking = _booking(app, email, "On The Way")
    assert _verify(client, booking, "").get_json()["success"] is False
    assert _verify(client, booking, "4829130").get_json()["success"] is False       # longer than the real code
    assert app.app.test_client().post(f"/verify-delivery-otp/{booking}", data={"otp": "482913"}).status_code == 401
    assert _status(app, booking) == "On The Way"


def test_otp_is_only_generated_for_live_deliveries(client, app):
    email = login_as_driver(client, app)
    for status in ("Delivered", "Cancelled", "Assigned"):
        booking = _booking(app, email, status, delivery_otp=None)
        response = client.post(f"/generate-delivery-otp/{booking}")
        assert response.status_code == 400
        assert app.bookings_collection.find_one({"_id": booking}).get("delivery_otp") is None


# ===================================== legacy routes can't undo a delivery

def test_legacy_accept_cannot_pull_a_delivered_booking_back_to_accepted(client, app):
    email = login_as_driver(client, app)
    booking = _booking(app, email, "Delivered")
    client.post(f"/accept-booking/{booking}")
    assert _status(app, booking) == "Delivered"


def test_legacy_reject_cannot_unassign_a_delivered_or_foreign_booking(client, app):
    email = login_as_driver(client, app)
    done = _booking(app, email, "Delivered")
    client.post(f"/reject-booking/{done}")
    assert _status(app, done) == "Delivered"
    assert app.bookings_collection.find_one({"_id": done})["driver_email"] == email
    foreign = _booking(app, "other@example.com", "Assigned")
    client.post(f"/reject-booking/{foreign}")
    assert app.bookings_collection.find_one({"_id": foreign})["driver_email"] == "other@example.com"


def test_new_api_accept_and_reject_still_work_for_an_assigned_booking(client, app):
    email = login_as_driver(client, app)
    accept = _booking(app, email, "Assigned")
    assert client.post(f"/api/accept-booking/{accept}").get_json()["success"] is True
    assert _status(app, accept) == "Accepted"
    reject = _booking(app, email, "Assigned")
    assert client.post(f"/api/reject-booking/{reject}").get_json()["success"] is True
    assert _status(app, reject) == "Approved"
