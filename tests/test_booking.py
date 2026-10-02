"""
test_booking.py
=====================================================
Covers the /booking form's server-side validation (never trust the
browser alone - see the comment in app.py above this validation
block), the "no driver available" deliverability rule, and a
customer cancelling their own not-yet-paid booking.
=====================================================
"""


from tests.helpers import create_user, login, submit_booking, seed_available_driver


def test_booking_requires_a_pinned_map_location(client, app):
    email, password = create_user(app)
    login(client, email, password)
    seed_available_driver(app)

    response = submit_booking(client, latitude="", longitude="")

    assert b"pin your delivery location" in response.data.lower()
    assert app.bookings_collection.count_documents({}) == 0


def test_booking_rejects_invalid_quantity(client, app):
    email, password = create_user(app)
    login(client, email, password)
    seed_available_driver(app)

    response = submit_booking(client, quantity="99999")

    assert b"valid water quantity" in response.data.lower()
    assert app.bookings_collection.count_documents({}) == 0


def test_booking_rejects_invalid_phone_number(client, app):
    email, password = create_user(app)
    login(client, email, password)
    seed_available_driver(app)

    response = submit_booking(client, phone="12345")  # doesn't start 6-9, wrong length

    assert b"valid 10-digit phone" in response.data.lower()


def test_booking_undeliverable_with_no_drivers_available(client, app):
    """No drivers seeded at all - every address should correctly be
    reported as undeliverable rather than silently pricing at 0."""
    email, password = create_user(app)
    login(client, email, password)

    response = submit_booking(client)

    assert app.bookings_collection.count_documents({}) == 0
    # The exact wording comes from pricing_engine.py's "reason" field -
    # this just checks the request was rejected, not silently accepted.
    assert response.status_code == 200


def test_customer_can_cancel_own_awaiting_payment_booking(client, app):
    email, password = create_user(app)
    login(client, email, password)

    booking_id = app.bookings_collection.insert_one({
        "user_email": email,
        "status": "Awaiting Payment",
    }).inserted_id

    client.post(f"/cancel-awaiting-payment/{booking_id}", follow_redirects=True)

    booking = app.bookings_collection.find_one({"_id": booking_id})
    assert booking["status"] == "Cancelled"


def test_customer_cannot_cancel_someone_elses_booking(client, app):
    email, password = create_user(app, email="me@example.com")
    login(client, email, password)

    other_booking_id = app.bookings_collection.insert_one({
        "user_email": "someone-else@example.com",
        "status": "Awaiting Payment",
    }).inserted_id

    client.post(f"/cancel-awaiting-payment/{other_booking_id}", follow_redirects=True)

    booking = app.bookings_collection.find_one({"_id": other_booking_id})
    assert booking["status"] == "Awaiting Payment"  # unchanged
