"""
test_signup.py
=====================================================
Covers account creation: form validation on /signup, and the
OTP-verification step on /verify-otp that actually creates the user
in the database.

Note: /signup normally emails a real OTP on success. To keep these
tests fast and offline, the OTP-verification tests set the session
directly (the same session keys /signup would have set) instead of
triggering a real signup - so no email is ever sent by this file.
=====================================================
"""

from tests.helpers import create_user


def test_signup_rejects_short_password(client):
    response = client.post("/signup", data={
        "name": "New User", "email": "new@example.com", "password": "abc",
    })
    assert b"at least 6 characters" in response.data.lower()


def test_signup_rejects_already_registered_email(client, app):
    create_user(app, email="taken@example.com")

    response = client.post("/signup", data={
        "name": "New User", "email": "taken@example.com", "password": "ValidPass1",
    })

    assert b"already registered" in response.data.lower()


def test_verify_otp_with_correct_code_creates_user(client, app):
    with client.session_transaction() as sess:
        sess["signup_name"] = "New User"
        sess["signup_email"] = "newuser@example.com"
        sess["signup_password"] = "ValidPass1"
        sess["signup_otp"] = "123456"

    response = client.post("/verify-otp", data={"otp": "123456"}, follow_redirects=True)

    assert response.status_code == 200
    assert app.users_collection.find_one({"email": "newuser@example.com"}) is not None


def test_verify_otp_with_wrong_code_does_not_create_user(client, app):
    with client.session_transaction() as sess:
        sess["signup_name"] = "New User"
        sess["signup_email"] = "newuser2@example.com"
        sess["signup_password"] = "ValidPass1"
        sess["signup_otp"] = "123456"

    response = client.post("/verify-otp", data={"otp": "000000"})

    assert b"invalid otp" in response.data.lower()
    assert app.users_collection.find_one({"email": "newuser2@example.com"}) is None
