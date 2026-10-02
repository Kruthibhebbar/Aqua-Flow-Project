"""
test_driver_delivery.py
=====================================================
Covers /verify-delivery-otp - the mechanism described in the README
as the core anti-fraud measure: a driver can only mark a delivery
"Delivered" by entering the OTP the CUSTOMER received by email, not
by pressing a button. This checks the correct-code, wrong-code, and
brute-force-lockout paths all actually work as designed.
=====================================================
"""

from tests.helpers import login_as_driver


def _seed_booking(app, driver_email, otp="482913", attempts=0):
    return app.bookings_collection.insert_one({
        "user_email": "customer@example.com",
        "driver_email": driver_email,
        "status": "On The Way",
        "delivery_otp": otp,
        "otp_attempts": attempts,
        "delivery_fee": 500,
    }).inserted_id


def test_correct_otp_marks_booking_delivered(client, app):
    driver_email = login_as_driver(client, app)
    booking_id = _seed_booking(app, driver_email, otp="482913")

    response = client.post(f"/verify-delivery-otp/{booking_id}", data={"otp": "482913"})
    data = response.get_json()

    assert data["success"] is True
    booking = app.bookings_collection.find_one({"_id": booking_id})
    assert booking["status"] == "Delivered"


def test_correct_otp_credits_driver_earnings(client, app):
    driver_email = login_as_driver(client, app)
    booking_id = _seed_booking(app, driver_email, otp="482913")

    client.post(f"/verify-delivery-otp/{booking_id}", data={"otp": "482913"})

    driver = app.drivers_collection.find_one({"email": driver_email})
    assert driver["completed_deliveries"] == 1
    assert driver["total_earnings"] == 500


def test_wrong_otp_does_not_complete_delivery(client, app):
    driver_email = login_as_driver(client, app)
    booking_id = _seed_booking(app, driver_email, otp="482913")

    response = client.post(f"/verify-delivery-otp/{booking_id}", data={"otp": "000000"})
    data = response.get_json()

    assert data["success"] is False
    booking = app.bookings_collection.find_one({"_id": booking_id})
    assert booking["status"] == "On The Way"  # unchanged
    assert booking["otp_attempts"] == 1  # counted as a failed attempt


def test_too_many_wrong_attempts_locks_out_further_tries(client, app):
    driver_email = login_as_driver(client, app)
    # already at 5 failed attempts from earlier tries
    booking_id = _seed_booking(app, driver_email, otp="482913", attempts=5)

    # even the CORRECT code should now be refused - the lockout is on
    # attempt count, not on guessing right
    response = client.post(f"/verify-delivery-otp/{booking_id}", data={"otp": "482913"})
    data = response.get_json()

    assert data["success"] is False
    assert "too many" in data["message"].lower()
    booking = app.bookings_collection.find_one({"_id": booking_id})
    assert booking["status"] != "Delivered"


def test_driver_cannot_verify_otp_for_someone_elses_booking(client, app):
    login_as_driver(client, app, email="driver-a@example.com")
    # booking belongs to a different driver
    booking_id = _seed_booking(app, "driver-b@example.com", otp="482913")

    response = client.post(f"/verify-delivery-otp/{booking_id}", data={"otp": "482913"})
    data = response.get_json()

    assert data["success"] is False
    assert response.status_code == 404
