from tests.helpers import create_user, login


def test_wrong_password_is_rejected(client, app):
    email, _ = create_user(app, password="CorrectPass1")

    response = login(client, email, "TotallyWrongPassword")

    assert b"wrong password" in response.data.lower()


def test_unknown_email_is_rejected(client, app):
    response = login(client, "nobody-registered@example.com", "whatever123")

    assert b"account not found" in response.data.lower()
