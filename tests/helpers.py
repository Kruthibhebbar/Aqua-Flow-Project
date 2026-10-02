"""
Small helpers shared across test files. Keeping this separate (instead
of copy-pasting into every test file) means if the User document shape
ever changes, there's exactly one place to update.
"""

from datetime import datetime, timedelta
import bcrypt


def create_user(app_module, email="customer@example.com", password="Passw0rd!", blocked=False, name="Test Customer"):
    """Inserts a user directly into the (fake) database, bypassing the
    signup form - tests that aren't specifically testing signup don't
    need to go through it."""
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    app_module.users_collection.insert_one({
        "name": name,
        "email": email,
        "password": hashed,
        "phone": "9876543210",
        "is_blocked": blocked,
        "created_at": datetime.now(),
    })
    return email, password


def login(client, email, password):
    """Submits the real /login form, exactly like a browser would."""
    return client.post(
        "/login",
        data={"email": email, "password": password},
        follow_redirects=True,
    )


VALID_BOOKING_FORM = {
    "fullname": "Test Customer",
    "phone": "9876543210",
    "address": "123 Test Street, Test City",
    "quantity": "1000",
    # Computed when the module loads (never hardcode a calendar date here):
    # the booking route rejects past dates, so a fixed date silently turns
    # every "valid booking" test red the day after it passes.
    "delivery_date": (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"),
    "delivery_time": "10:00 AM",
    "water_type": "Fresh Water",
    "payment_method": "cod",
    # the booking form always sends a pinned map location - without
    # this, calculate_booking_price() has no address to price against
    "latitude": "13.3379",
    "longitude": "75.7594",
}


def seed_available_driver(app_module, lat=13.3379, lng=75.7594):
    """Pricing/deliverability only considers an Approved + Online
    driver with a live location - without at least one of these,
    EVERY booking is correctly rejected as 'not deliverable', even
    with a perfectly valid form. Tests that need a booking to
    actually succeed need to seed one of these first."""
    app_module.drivers_collection.insert_one({
        "name": "Test Driver",
        "approval_status": "Approved",
        "online_status": "Online",
        "current_lat": lat,
        "current_lng": lng,
    })


def submit_booking(client, **overrides):
    """Submits the real /booking form. Pass e.g. quantity="0" to test
    a specific validation rule without retyping the whole form."""
    data = {**VALID_BOOKING_FORM, **overrides}
    return client.post("/booking", data=data, follow_redirects=True)


def login_as_admin(client, app_module, email="admin@aquaflow.com", password="AdminPass1"):
    """Inserts an admin account directly and logs in through the real
    /admin-login form. Admin passwords are stored in plain text in
    this codebase (see admins_collection) - tests match that."""
    app_module.admins_collection.insert_one({"email": email, "password": password})
    client.post("/admin-login", data={"email": email, "password": password}, follow_redirects=True)
    return email


def login_as_driver(client, app_module, email="driver@example.com", approved=True):
    """Drivers authenticate via session['driver_email'] - setting the
    session directly (instead of going through /driver-login, which
    would also try to send a real OTP email) is enough to exercise
    every driver-only route the same way a logged-in driver would."""
    app_module.drivers_collection.insert_one({
        "email": email,
        "name": "Test Driver",
        "approval_status": "Approved" if approved else "Pending",
    })
    with client.session_transaction() as sess:
        sess["driver_email"] = email
    return email
