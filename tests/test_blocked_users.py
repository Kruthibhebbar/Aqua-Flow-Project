"""
test_blocked_users.py
=====================================================
Covers the account-blocking bug that was fixed on the admin Users
page: an admin blocking a customer must ALSO stop that customer from
placing new orders - not just stop them from logging in fresh.

Each test below is one specific claim about how the app should
behave. If someone changes the code later in a way that breaks any
of these claims, `pytest` will fail loudly and say exactly which one.
=====================================================
"""

from tests.helpers import create_user, login, submit_booking, seed_available_driver


def test_active_user_can_log_in(client, app):
    email, password = create_user(app, blocked=False)

    response = login(client, email, password)

    assert response.status_code == 200
    # a successful login lands on the dashboard, not back on the login page
    assert b"login" not in response.request.path.encode()


def test_blocked_user_cannot_log_in(client, app):
    email, password = create_user(app, blocked=True)

    response = login(client, email, password)

    assert b"suspended" in response.data.lower()


def test_blocked_user_cannot_place_a_booking(client, app):
    """
    This is the important one: it reproduces the exact bug scenario -
    the user logs in WHILE ACTIVE (so they have a valid session), and
    only gets blocked afterwards. Before the fix, the session alone
    was enough to keep booking. After the fix, the block is checked
    on every booking attempt, not just at login.
    """
    email, password = create_user(app, blocked=False)
    login(client, email, password)

    # admin blocks them *after* they're already logged in
    app.users_collection.update_one({"email": email}, {"$set": {"is_blocked": True}})

    response = submit_booking(client)

    assert b"suspended" in response.data.lower()
    assert app.bookings_collection.count_documents({}) == 0


def test_active_user_can_place_a_booking(client, app):
    """The flip side - make sure the fix didn't accidentally block
    everyone, only actually-blocked users."""
    email, password = create_user(app, blocked=False)
    login(client, email, password)
    seed_available_driver(app)

    submit_booking(client)

    assert app.bookings_collection.count_documents({"user_email": email}) == 1
