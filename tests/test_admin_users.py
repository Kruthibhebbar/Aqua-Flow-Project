"""
test_admin_users.py
=====================================================
Covers the admin-only endpoints behind the Users page: the
block/unblock toggle and the "send message" action, plus making sure
a non-admin session can't call either one directly.
=====================================================
"""

from tests.helpers import create_user, login_as_admin


def test_toggle_block_requires_admin_session(client, app):
    email, _ = create_user(app)
    user_id = app.users_collection.find_one({"email": email})["_id"]

    response = client.post(f"/admin/users/{user_id}/toggle-block")

    assert response.status_code == 401
    user = app.users_collection.find_one({"_id": user_id})
    assert user["is_blocked"] is False  # untouched


def test_admin_can_block_an_active_user(client, app):
    login_as_admin(client, app)
    email, _ = create_user(app, blocked=False)
    user_id = app.users_collection.find_one({"email": email})["_id"]

    response = client.post(f"/admin/users/{user_id}/toggle-block")
    data = response.get_json()

    assert data["success"] is True
    assert data["is_blocked"] is True
    assert app.users_collection.find_one({"_id": user_id})["is_blocked"] is True


def test_admin_can_unblock_a_blocked_user(client, app):
    login_as_admin(client, app)
    email, _ = create_user(app, blocked=True)
    user_id = app.users_collection.find_one({"email": email})["_id"]

    response = client.post(f"/admin/users/{user_id}/toggle-block")
    data = response.get_json()

    assert data["is_blocked"] is False
    # unblocking notifies the user - confirms that side effect happened too
    assert app.notifications_collection.count_documents({"user_email": email}) == 1


def test_admin_can_message_a_user(client, app):
    login_as_admin(client, app)
    email, _ = create_user(app)
    user_id = app.users_collection.find_one({"email": email})["_id"]

    response = client.post(
        f"/admin/users/{user_id}/message",
        data={"message": "Your delivery is on the way!"},
    )
    data = response.get_json()

    assert data["success"] is True
    notification = app.notifications_collection.find_one({"user_email": email})
    assert notification["message"] == "Your delivery is on the way!"


def test_admin_cannot_send_an_empty_message(client, app):
    login_as_admin(client, app)
    email, _ = create_user(app)
    user_id = app.users_collection.find_one({"email": email})["_id"]

    response = client.post(f"/admin/users/{user_id}/message", data={"message": "   "})
    data = response.get_json()

    assert data["success"] is False
    assert app.notifications_collection.count_documents({}) == 0
