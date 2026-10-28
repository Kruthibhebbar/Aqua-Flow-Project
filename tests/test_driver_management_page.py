"""
test_driver_management_page.py
=====================================================
Covers the Registered Drivers page: the View Map link must point at a
real, correctly-encoded Google Maps URL for THAT driver's own address,
a missing address must never produce a map link, and click-to-call is
only offered for a plausible plain phone number.
=====================================================
"""

from urllib.parse import quote

from tests.helpers import login_as_admin


def _driver(app, **overrides):
    doc = {
        "name": "Test Driver One",
        "email": "driver1@example.com",
        "phone": "9876543210",
        "address": "12, MG Road & 5th Cross, Bengaluru 560001",
        "truck_number": "KA-01-AB-1234",
        "truck_capacity": 10000,
        "approval_status": "Approved",
        "online_status": "Offline",
    }
    doc.update(overrides)
    app.drivers_collection.insert_one(doc)


def _page(client):
    response = client.get("/driver-management")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_view_map_links_to_the_drivers_own_address(client, app):
    login_as_admin(client, app)
    _driver(app)
    _driver(app, email="d2@example.com", name="Second", address="Plot 9, Koramangala")

    html = _page(client)

    assert ("https://www.google.com/maps/search/?api=1&amp;query="
            + quote("12, MG Road & 5th Cross, Bengaluru 560001", safe="/")) in html
    assert "query=Plot%209%2C%20Koramangala" in html
    assert 'rel="noopener noreferrer"' in html


def test_missing_address_never_gets_a_map_link(client, app):
    login_as_admin(client, app)
    _driver(app, address="   ")

    html = _page(client)

    assert "Driver address is unavailable" in html
    assert "maps/search/?api=1&amp;query=%20" not in html
    assert "google.com/maps" not in html


def test_gps_button_only_when_driver_has_saved_coordinates(client, app):
    login_as_admin(client, app)
    _driver(app, email="a@example.com")
    _driver(app, email="b@example.com", current_lat=12.9716, current_lng=77.5946)

    html = _page(client)

    assert html.count("query=12.9716,77.5946") >= 1
    assert html.count('class="map-btn stale"') == 1   # no timestamp -> never presented as fresh
    assert "Last GPS" in html


def test_fresh_gps_is_labelled_differently_from_an_old_one(client, app):
    from datetime import datetime, timedelta
    login_as_admin(client, app)
    _driver(app, email="fresh@example.com", current_lat=12.9, current_lng=77.5,
            location_updated_at=datetime.now() - timedelta(minutes=2))
    _driver(app, email="old@example.com", current_lat=13.0, current_lng=77.6,
            location_updated_at=datetime.now() - timedelta(days=3))

    html = _page(client)

    assert html.count('class="map-btn live"') == 1
    assert html.count('class="map-btn stale"') == 1
    assert "not live tracking" in html


def test_click_to_call_only_for_plain_valid_numbers(client, app):
    login_as_admin(client, app)
    _driver(app, email="a@example.com", phone="+91 98765 43210")
    _driver(app, email="b@example.com", phone="12")
    _driver(app, email="c@example.com", phone="555 010 9999 ext 4")

    html = _page(client)

    assert 'href="tel:+919876543210"' in html
    assert html.count('href="tel:') == 2  # (link + menu entry) for the one valid number only


def test_apostrophe_in_name_does_not_break_the_page(client, app):
    login_as_admin(client, app)
    _driver(app, name="Ravi O'Neil")

    html = _page(client)

    assert "Ravi O&#39;Neil" in html
