"""
test_driver_support_workflow.py
=====================================================
The full support loop: driver raises a request -> it reaches the admin
(badge on Driver Management, "Driver Support" in the 3-dot menu, inbox) ->
admin replies -> the driver sees the complaint + description + reply in
Notifications > Support -> the driver can follow up.
=====================================================
"""

import re

import pytest

from tests.helpers import login_as_admin, login_as_driver

REQUEST = {"category": "delivery", "subject": "Customer not at address",
           "message": "I reached the address twice and nobody answered the phone."}


@pytest.fixture(autouse=True)
def _clean(app):
    for name in ("driver_support_requests_collection", "notifications_collection", "drivers_collection"):
        getattr(app, name).delete_many({})
    yield


@pytest.fixture
def admin(app):
    c = app.app.test_client()
    login_as_admin(c, app)
    return c


@pytest.fixture
def driver(client, app):
    email = login_as_driver(client, app, "driver@example.com")
    app.drivers_collection.update_one({"email": email}, {"$set": {
        "name": "Kruthik", "phone": "9876543210", "approval_status": "Approved"}})
    return client


def _raise(driver, **over):
    body = driver.post("/api/driver/support", json={**REQUEST, **over}).get_json()
    assert body["success"], body
    return body["ticket"]


def test_new_request_reaches_admin_badge_menu_and_inbox(driver, admin, app):
    ticket = _raise(driver)

    summary = admin.get("/admin/driver-support/summary").get_json()
    assert summary["open"] == 1 and summary["by_driver"] == {"driver@example.com": 1}

    page = admin.get("/driver-management").get_data(as_text=True)
    assert "Driver Support" in page
    assert 'class="sup-dot show"' in page                        # red dot on the 3-dot button
    assert re.search(r'data-sup-badge>1</span>', page)            # count in the menu item
    assert re.search(r"/admin/driver-support\?[^\"]*driver=driver(%40|@)example.com", page)

    inbox = admin.get("/admin/driver-support").get_data(as_text=True)
    assert ticket in inbox and "Customer not at address" in inbox and "nobody answered" in inbox


def test_no_badge_when_nothing_is_open(driver, admin):
    page = admin.get("/driver-management").get_data(as_text=True)
    assert 'class="sup-dot show"' not in page
    assert admin.get("/admin/driver-support/summary").get_json()["open"] == 0


def test_admin_endpoints_require_admin(driver, client):
    anon = client.application.test_client()
    assert anon.get("/admin/driver-support/summary").status_code == 401
    assert anon.post("/admin/driver-support/abc/reply", json={"message": "hi"}).status_code == 401
    assert anon.get("/admin/driver-support").status_code == 302


def test_reply_notifies_driver_with_complaint_description_and_answer(driver, admin, app):
    ticket = _raise(driver)
    rid = str(app.driver_support_requests_collection.find_one({"ticket": ticket})["_id"])

    r = admin.post(f"/admin/driver-support/{rid}/reply", json={"message": "We called the customer; please retry at 5pm."}).get_json()
    assert r["ok"] and r["request"]["status"] == "Answered"

    # no longer needs attention
    assert admin.get("/admin/driver-support/summary").get_json()["open"] == 0

    page = driver.get("/driver-notifications?filter=support").get_data(as_text=True)
    assert ticket in page
    assert "Customer not at address" in page                      # complaint
    assert "nobody answered the phone" in page                     # description
    assert "please retry at 5pm" in page                           # admin reply
    assert "AquaFlow Support replied" in page
    # appears in the bell count too, and under the Support tab only
    other_tab = driver.get("/driver-notifications?filter=payments").get_data(as_text=True)
    main = other_tab[other_tab.index('id="af-main"'):other_tab.index("</main>")]   # the bell in the shell lists recent items
    assert ticket not in main


def test_empty_reply_is_rejected(driver, admin, app):
    _raise(driver)
    rid = str(app.driver_support_requests_collection.find_one({})["_id"])
    assert admin.post(f"/admin/driver-support/{rid}/reply", json={"message": " "}).status_code == 400


def test_send_and_resolve_then_driver_follow_up_reopens(driver, admin, app):
    ticket = _raise(driver)
    rid = str(app.driver_support_requests_collection.find_one({"ticket": ticket})["_id"])

    admin.post(f"/admin/driver-support/{rid}/reply", json={"message": "Done, thanks.", "resolve": True})
    assert app.driver_support_requests_collection.find_one({"ticket": ticket})["status"] == "Resolved"
    note = app.notifications_collection.find_one({"type": "support_reply"})
    assert note["support"]["state"] == "Resolved" and note["driver_email"] == "driver@example.com"

    r = driver.post(f"/api/driver/support/{rid}/reply", json={"message": "It happened again."}).get_json()
    assert r["success"] and r["request"]["status"] == "Open"
    assert [m["by"] for m in r["request"]["replies"]] == ["admin", "driver"]
    assert admin.get("/admin/driver-support/summary").get_json()["open"] == 1


def test_driver_cannot_read_or_reply_to_another_drivers_request(driver, admin, app, client):
    ticket = _raise(driver)
    rid = str(app.driver_support_requests_collection.find_one({"ticket": ticket})["_id"])
    other = app.app.test_client()
    login_as_driver(other, app, "other@example.com")
    assert other.get(f"/api/driver/support/{rid}").status_code == 404
    assert other.post(f"/api/driver/support/{rid}/reply", json={"message": "hello there"}).status_code == 404


def test_driver_page_exposes_clickable_requests_and_opens_by_ticket(driver):
    ticket = _raise(driver)
    html = driver.get(f"/driver-support?ticket={ticket}").get_data(as_text=True)
    assert "data-sp-open=" in html and 'id="sp-req-dialog"' in html
    assert f'window.SP_OPEN_TICKET = "{ticket}"' in html


def test_emergency_alert_counts_for_admin_and_cannot_be_followed_up(driver, admin, app):
    body = driver.post("/api/driver/support/emergency", json={"message": "flat tyre, unsafe road"}).get_json()
    assert body["success"]
    s = admin.get("/admin/driver-support/summary").get_json()
    assert s["open"] == 1 and s["emergency"] == 1
    rid = str(app.driver_support_requests_collection.find_one({"ticket": body["ticket"]})["_id"])
    assert driver.post(f"/api/driver/support/{rid}/reply", json={"message": "anyone there?"}).status_code == 404
