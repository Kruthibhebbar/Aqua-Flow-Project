"""Rendering tests for the redesigned /profile page."""

from datetime import datetime, timedelta

from tests.helpers import create_user, login


def _seed(app):
    email, pw = create_user(app, name="Asha Rao")
    app.users_collection.update_one(
        {"email": email},
        {"$set": {
            "gender": "Female", "dob": "1999-01-02", "address": "12 Lake Road",
            "favorite_tanker": "2000 L", "preferred_water": "Fresh Water",
            "is_verified": True, "last_login": datetime.now(),
        }},
    )
    return email, pw


def _booking(app, email, status, qty=1000, **extra):
    doc = {
        "user_email": email, "status": status, "quantity": qty,
        "delivery_date": (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d"),
        "address": "Booking Address 1",
        "driver_name": "Not Assigned", "truck_number": "Not Assigned",
    }
    doc.update(extra)
    return app.bookings_collection.insert_one(doc).inserted_id


def test_profile_requires_login(client):
    r = client.get("/profile")
    assert r.status_code == 302


def test_profile_renders_new_design_with_dynamic_stats(client, app):
    email, pw = _seed(app)
    _booking(app, email, "Delivered", qty=1500)
    _booking(app, email, "Delivered", qty=500)
    _booking(app, email, "Cancelled")
    bid = _booking(app, email, "Assigned", driver_name="Ravi", truck_number="KA-01-1234")
    login(client, email, pw)

    html = client.get("/profile").get_data(as_text=True)

    assert "Asha Rao" in html and email in html
    # Personal Information / Water Booking Preferences now live on /account-information
    assert "12 Lake Road" not in html and "Female" not in html and "Fresh Water" not in html
    assert "2,000" in html                      # liters delivered
    assert f"/live-status/{bid}" in html        # tracking link
    assert "Ravi" in html and "KA-01-1234" in html
    assert 'id="photoForm"' in html and 'id="photoInput"' in html
    assert "<h1>Asha Rao</h1>" in html
    for href in ('href="/dashboard"', 'href="/booking"', 'href="/history"',
                 'href="/notification"', 'href="/my-payments"'):
        assert href in html
    assert "Saved Addresses" not in html        # no such feature in the app
    assert "Verified Account" in html
    # uses the Booking History design language
    assert "Space Grotesk" in html            # Booking History typography


def test_profile_empty_next_delivery_state(client, app):
    email, pw = _seed(app)
    login(client, email, pw)
    html = client.get("/profile").get_data(as_text=True)
    assert "No upcoming deliveries scheduled right now." in html
    assert "/live-status/" not in html


def test_profile_hides_unassigned_driver(client, app):
    email, pw = _seed(app)
    _booking(app, email, "Pending")
    login(client, email, pw)
    html = client.get("/profile").get_data(as_text=True)
    assert "Driver:" not in html and "Vehicle:" not in html


def test_profile_bell_shows_unread_dot(client, app):
    email, pw = _seed(app)
    app.notifications_collection.insert_one(
        {"user_email": email, "read": False, "message": "hi", "type": "info",
         "created_at": datetime.now()}
    )
    login(client, email, pw)
    html = client.get("/profile").get_data(as_text=True)
    assert 'class="bell-dot"' in html
    assert "(1 unread)" in html


def test_profile_default_page_does_not_contain_account_cards(client, app):
    """Personal Information / Water Booking Preferences moved to /account-information."""
    email, pw = _seed(app)
    login(client, email, pw)
    html = client.get("/profile").get_data(as_text=True)

    for gone in ("Personal Information", "Water Booking Preferences", "Favorite Tanker", "Preferred Water",
                 "Default delivery address", "Quick Actions", "Account Details", "Date of Birth",
                 "1999-01-02", "Female", "Fresh Water", "12 Lake Road"):
        assert gone not in html, gone
    assert 'class="cols"' not in html and "page-footer" not in html


def test_profile_page_ends_after_next_delivery_card(client, app):
    email, pw = _seed(app)
    _booking(app, email, "Assigned", driver_name="Ravi", truck_number="KA-01-1234")
    login(client, email, pw)
    html = client.get("/profile").get_data(as_text=True)
    body = html[html.index("<body>"):]
    after_delivery = body[body.index('aria-label="Next delivery"'):]
    assert "</section>" in after_delivery
    tail = after_delivery[after_delivery.index("</section>") + len("</section>"):]
    # nothing but the closing container and scripts follow the Next Delivery card
    assert "<section" not in tail and 'class="entity-card' not in tail


def test_profile_links_to_account_information_and_keeps_edit_profile(client, app):
    email, pw = _seed(app)
    login(client, email, pw)
    html = client.get("/profile").get_data(as_text=True)
    assert 'href="/account-information"' in html and "Account Information" in html
    assert 'href="/edit_profile"' in html and "My Payments" in html and "New Booking" in html


# ---------------- Account Information page ----------------

def test_account_information_requires_login(client):
    r = client.get("/account-information")
    assert r.status_code == 302 and r.headers["Location"].endswith("/")


def test_account_information_shows_both_cards_with_real_data(client, app):
    email, pw = _seed(app)
    login(client, email, pw)
    r = client.get("/account-information")
    assert r.status_code == 200
    html = r.get_data(as_text=True)

    assert "Personal Information" in html and "Water Booking Preferences" in html
    assert "Asha Rao" in html
    for value in ("9876543210", "Female", "1999-01-02", "12 Lake Road", "2000 L", "Fresh Water"):
        assert value in html, value
    for label in ("Phone", "Gender", "Date of Birth", "Address", "Verification", "Created", "Last Login",
                  "Favorite Tanker", "Preferred Water", "Default delivery address"):
        assert label in html, label
    assert 'href="/edit_profile"' in html                       # edit links kept
    assert 'href="/profile"' in html and "Back to My Profile" in html
    assert 'aria-current="page">Account Information' in html


def test_account_information_shows_only_the_logged_in_customers_data(client, app):
    email, pw = _seed(app)
    other, _ = create_user(app, email="other@example.com", name="Other Person")
    app.users_collection.update_one({"email": other}, {"$set": {"address": "99 Secret Street", "favorite_tanker": "9999 L"}})
    login(client, email, pw)
    html = client.get("/account-information").get_data(as_text=True)
    assert "Asha Rao" in html and "Other Person" not in html
    assert "99 Secret Street" not in html and "9999 L" not in html


def test_account_information_empty_fields_show_placeholders(client, app):
    email, pw = create_user(app, name="Bare User")
    login(client, email, pw)
    html = client.get("/account-information").get_data(as_text=True)
    assert "Not Added" in html and "Not Selected" in html


def test_account_information_is_read_only_and_does_not_create_records(client, app):
    email, pw = _seed(app)
    login(client, email, pw)
    before = app.users_collection.count_documents({})
    assert client.post("/account-information", data={"phone": "1"}).status_code == 405
    client.get("/account-information"); client.get("/account-information")
    assert app.users_collection.count_documents({}) == before
    assert app.users_collection.find_one({"email": email})["phone"] == "9876543210"


def test_account_information_back_navigation_targets_profile(client, app):
    email, pw = _seed(app)
    login(client, email, pw)
    html = client.get("/account-information").get_data(as_text=True)
    assert 'var HOME_URL = "/profile"' in html


def test_edit_profile_still_saves_and_account_page_reflects_it(client, app):
    email, pw = _seed(app)
    login(client, email, pw)

    form = client.get("/edit_profile")
    assert form.status_code == 200 and "12 Lake Road" in form.get_data(as_text=True)

    r = client.post("/edit_profile", data={
        "phone": "9000011111", "gender": "Male", "dob": "2001-05-06", "address": "7 New Lane",
        "favorite_tanker": "5000 L", "preferred_water": "Mineral Water",
    })
    assert r.status_code == 302                                  # existing behaviour (back to dashboard)

    saved = app.users_collection.find_one({"email": email})
    assert (saved["phone"], saved["address"], saved["favorite_tanker"]) == ("9000011111", "7 New Lane", "5000 L")
    assert app.users_collection.count_documents({"email": email}) == 1

    html = client.get("/account-information").get_data(as_text=True)
    for value in ("9000011111", "Male", "2001-05-06", "7 New Lane", "5000 L", "Mineral Water"):
        assert value in html, value
    assert "12 Lake Road" not in html
    assert "7 New Lane" not in client.get("/profile").get_data(as_text=True)
