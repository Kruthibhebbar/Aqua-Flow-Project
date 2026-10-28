"""
test_driver_portal_actions.py
=====================================================
Everything in the Driver Operations Suite that CHANGES something:
profile / photo / password, the documents centre (incl. admin review),
support requests, and the CSRF guard. Checks both the happy path and the
ways a driver (or someone else) could try to misuse it.
=====================================================
"""

import io
import itertools
import os
from datetime import datetime, timedelta

import bcrypt
import pytest
from bson.objectid import ObjectId

from tests.helpers import login_as_admin, login_as_driver

PNG = None


def _png_bytes():
    global PNG
    if PNG is None:
        from PIL import Image
        buffer = io.BytesIO()
        Image.new("RGB", (24, 24), (40, 90, 200)).save(buffer, "PNG")
        PNG = buffer.getvalue()
    return PNG


PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"
_oid_counter = itertools.count(0x61000000)


def _oid():
    return ObjectId("%08x" % next(_oid_counter) + os.urandom(8).hex())


@pytest.fixture(autouse=True)
def _isolate(app, tmp_path, monkeypatch):
    for name in ("documents_collection", "driver_activity_collection", "driver_support_requests_collection"):
        getattr(app, name).delete_many({})
    monkeypatch.setitem(app.app.config, "DRIVER_DOCUMENTS_DIR", str(tmp_path / "docs"))
    monkeypatch.setitem(app.app.config, "DRIVER_PHOTOS_DIR", str(tmp_path / "photos"))
    monkeypatch.delenv("SUPPORT_EMAIL", raising=False)
    yield


def _driver(client, app, email="driver@example.com", **extra):
    login_as_driver(client, app, email)
    app.drivers_collection.update_one({"email": email}, {"$set": {
        "name": "Kruthik Hebbar", "truck_number": "KA09G2123", "truck_capacity": "10000",
        "phone": "9876543210", "address": "Lake View Road, Shivamogga",
        "password": bcrypt.hashpw(b"OldPass123", bcrypt.gensalt()), **extra}})
    return email


def _booking(app, email, status="Delivered", **extra):
    doc = {"_id": _oid(), "driver_email": email, "status": status, "delivery_fee": 500, "quantity": "1000",
           "user_email": "c@example.com", "created_at": datetime.now()}
    doc.update(extra)
    return app.bookings_collection.insert_one(doc).inserted_id


def _admin_client(app):
    admin = app.app.test_client()
    login_as_admin(admin, app)
    return admin


# ============================================================ profile

PROFILE_OK = {"phone": "98765 43210", "address": "14 Gandhi Bazaar, Shivamogga", "emergency_contact": "9123456780",
              "experience_years": "6", "license_number": "ka-1420110012345"}


def test_profile_update_persists_and_reports_what_was_stored(client, app):
    email = _driver(client, app)
    body = client.post("/api/driver/profile", json=PROFILE_OK).get_json()
    assert body["success"] is True
    saved = app.drivers_collection.find_one({"email": email})
    assert saved["phone"] == "9876543210"                  # spaces stripped
    assert saved["address"] == "14 Gandhi Bazaar, Shivamogga"
    assert saved["emergency_contact"] == "9123456780" and saved["experience_years"] == 6
    assert saved["license_number"] == "KA-1420110012345"
    assert body["profile"]["phone"] == "9876543210"
    # still there after a "reload"
    assert "9123456780" in client.get("/driver-profile").get_data(as_text=True)


@pytest.mark.parametrize("field,value,fragment", [
    ("phone", "12345", "valid phone"),
    ("phone", "abcdefghij", "valid phone"),
    ("address", "short", "full address"),
    ("address", "x" * 300, "under 250"),
    ("emergency_contact", "9876543210", "different number"),
    ("emergency_contact", "12", "valid phone"),
    ("experience_years", "-3", "between 0 and 60"),
    ("experience_years", "lots", "between 0 and 60"),
    ("license_number", "!!", "5-25"),
])
def test_profile_rejects_bad_values_with_a_field_error(client, app, field, value, fragment):
    email = _driver(client, app)
    response = client.post("/api/driver/profile", json={**PROFILE_OK, field: value})
    body = response.get_json()
    assert response.status_code == 400 and body["success"] is False
    assert fragment in body["errors"][field]
    assert app.drivers_collection.find_one({"email": email})["phone"] == "9876543210"   # nothing saved


def test_profile_cannot_change_admin_controlled_fields(client, app):
    email = _driver(client, app)
    client.post("/api/driver/profile", json={**PROFILE_OK, "name": "Hacker", "email": "x@y.z",
                "truck_number": "KA00ZZ0000", "truck_capacity": "99999", "approval_status": "Approved", "online_status": "Online"})
    saved = app.drivers_collection.find_one({"email": email})
    assert saved["name"] == "Kruthik Hebbar" and saved["truck_number"] == "KA09G2123"
    assert saved["truck_capacity"] == "10000" and saved["email"] == email


def test_profile_page_marks_vehicle_fields_as_read_only(client, app):
    _driver(client, app)
    html = client.get("/driver-profile").get_data(as_text=True)
    assert "Managed by AquaFlow" in html and "KA09G2123" in html
    assert 'name="truck_number"' not in html and 'name="truck_capacity"' not in html


def test_profile_requires_login(client):
    assert client.post("/api/driver/profile", json=PROFILE_OK).status_code == 401


def test_photo_upload_saves_and_updates_the_profile(client, app, tmp_path):
    email = _driver(client, app)
    response = client.post("/api/driver/profile/photo", data={"photo": (io.BytesIO(_png_bytes()), "me.png")},
                           content_type="multipart/form-data")
    body = response.get_json()
    assert body["success"] is True and body["photo_url"].startswith("/static/uploads/drivers/drv_")
    stored = app.drivers_collection.find_one({"email": email})["photo"]
    assert stored.startswith("static/uploads/drivers/drv_")
    assert os.listdir(tmp_path / "photos")[0] == stored.rsplit("/", 1)[1]


@pytest.mark.parametrize("name,content,fragment", [
    ("evil.exe", b"MZ\x90\x00", "PNG, JPG or WEBP"),
    ("pic.svg", b"<svg onload=alert(1)/>", "PNG, JPG or WEBP"),
    ("fake.png", b"this is not an image at all", "damaged"),
    ("empty.png", b"", "empty"),
    ("big.png", b"\x89PNG" + b"0" * (5 * 1024 * 1024 + 10), "too large"),
])
def test_photo_upload_rejects_non_images(client, app, name, content, fragment):
    email = _driver(client, app)
    response = client.post("/api/driver/profile/photo", data={"photo": (io.BytesIO(content), name)},
                           content_type="multipart/form-data")
    assert response.status_code == 400
    assert fragment in response.get_json()["message"]
    assert not app.drivers_collection.find_one({"email": email}).get("photo")


def test_password_change_works_and_old_password_stops_working(client, app):
    email = _driver(client, app)
    body = client.post("/api/driver/password", json={
        "current_password": "OldPass123", "new_password": "NewPass456", "confirm_password": "NewPass456"}).get_json()
    assert body["success"] is True
    stored = app.drivers_collection.find_one({"email": email})["password"]
    assert bcrypt.checkpw(b"NewPass456", stored) and not bcrypt.checkpw(b"OldPass123", stored)


@pytest.mark.parametrize("payload,field", [
    ({"current_password": "WRONG", "new_password": "NewPass456", "confirm_password": "NewPass456"}, "current_password"),
    ({"current_password": "OldPass123", "new_password": "short1", "confirm_password": "short1"}, "new_password"),
    ({"current_password": "OldPass123", "new_password": "onlyletters", "confirm_password": "onlyletters"}, "new_password"),
    ({"current_password": "OldPass123", "new_password": "NewPass456", "confirm_password": "Different1"}, "confirm_password"),
    ({"current_password": "OldPass123", "new_password": "OldPass123", "confirm_password": "OldPass123"}, "new_password"),
])
def test_password_change_validation(client, app, payload, field):
    email = _driver(client, app)
    response = client.post("/api/driver/password", json=payload)
    assert response.status_code == 400 and field in response.get_json()["errors"]
    assert bcrypt.checkpw(b"OldPass123", app.drivers_collection.find_one({"email": email})["password"])


# ============================================================ documents

def _upload(client, doc_type="aadhaar", content=PDF, filename="id.pdf", **form):
    data = {"document_type": doc_type, "document": (io.BytesIO(content), filename), **form}
    return client.post("/api/driver/documents/upload", data=data, content_type="multipart/form-data")


def _future(days=200):
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")


def test_document_upload_is_stored_privately_and_waits_for_review(client, app, tmp_path):
    email = _driver(client, app)
    body = _upload(client, "driving_license", expiry_date=_future()).get_json()
    assert body["success"] is True and body["document"]["status"] == "Pending Review"
    doc = app.documents_collection.find_one({"driver_email": email, "document_type": "driving_license"})
    assert doc["status"] == "Pending Review" and doc["reviewed_by"] is None
    assert not doc["file_path"].startswith("static/")              # never under the public folder
    stored = os.listdir(tmp_path / "docs")
    assert len(stored) == 1 and stored[0] == doc["stored_name"] and stored[0] != "id.pdf"   # random server-made name
    assert doc["size_bytes"] == len(PDF) and doc["mime_type"] == "application/pdf"


def test_document_upload_tells_admin_there_is_something_to_review(client, app):
    email = _driver(client, app)
    _upload(client, "aadhaar")
    note = app.notifications_collection.find_one({"type": "driver_document"})
    assert note and email in note["message"]


@pytest.mark.parametrize("kwargs,field,fragment", [
    ({"doc_type": "passport"}, "document_type", "Choose which document"),
    ({"filename": "run.exe", "content": b"MZ"}, "document", "PDF, PNG, JPG or WEBP"),
    ({"filename": "x.pdf", "content": b"just text, not a pdf"}, "document", "isn't a real PDF"),
    ({"filename": "x.png", "content": b"just text, not an image"}, "document", "isn't a real image"),
    ({"filename": "x.pdf", "content": b""}, "document", "empty"),
    ({"filename": "x.pdf", "content": b"%PDF-" + b"0" * (5 * 1024 * 1024 + 5)}, "document", "too large"),
    ({"doc_type": "driving_license"}, "expiry_date", "expiry date"),
    ({"doc_type": "driving_license", "expiry_date": "2001-01-01"}, "expiry_date", "in the past"),
    ({"doc_type": "driving_license", "expiry_date": "31/12/2030"}, "expiry_date", "YYYY-MM-DD"),
    ({"doc_type": "vehicle_insurance"}, "expiry_date", "expiry date"),
])
def test_document_upload_validation(client, app, tmp_path, kwargs, field, fragment):
    _driver(client, app)
    extra = {k: v for k, v in kwargs.items() if k == "expiry_date"}
    args = {k: v for k, v in kwargs.items() if k in ("doc_type", "content", "filename")}
    response = _upload(client, **args, **extra)
    assert response.status_code in (400, 413)
    assert fragment in response.get_json()["errors"][field]
    assert app.documents_collection.count_documents({}) == 0
    assert not os.path.exists(tmp_path / "docs") or os.listdir(tmp_path / "docs") == []


def test_mismatched_declared_mime_type_is_rejected(client, app):
    _driver(client, app)
    data = {"document_type": "aadhaar", "document": (io.BytesIO(PDF), "id.pdf", "image/png")}
    response = client.post("/api/driver/documents/upload", data=data, content_type="multipart/form-data")
    assert response.status_code == 400 and "doesn't match" in response.get_json()["errors"]["document"]


def test_document_without_expiry_type_ignores_a_supplied_expiry(client, app):
    email = _driver(client, app)
    _upload(client, "aadhaar", expiry_date=_future())
    assert app.documents_collection.find_one({"driver_email": email})["expiry_date"] is None


def test_optional_insurance_document_can_be_uploaded(client, app):
    _driver(client, app)
    assert _upload(client, "vehicle_insurance", expiry_date=_future(90)).get_json()["success"] is True


def test_owner_can_view_and_download_with_safe_headers(client, app):
    email = _driver(client, app)
    _upload(client, "aadhaar")
    doc_id = app.documents_collection.find_one({"driver_email": email})["_id"]
    view = client.get(f"/driver-documents/{doc_id}/file")
    assert view.status_code == 200 and view.data == PDF
    assert view.headers["X-Content-Type-Options"] == "nosniff" and "no-store" in view.headers["Cache-Control"]
    assert "attachment" in client.get(f"/driver-documents/{doc_id}/file?download=1").headers["Content-Disposition"]


def test_another_driver_cannot_open_my_document(client, app):
    mine = _driver(client, app)
    _upload(client, "aadhaar")
    doc_id = app.documents_collection.find_one({"driver_email": mine})["_id"]
    intruder = app.app.test_client()
    login_as_driver(intruder, app, "intruder@example.com")
    assert intruder.get(f"/driver-documents/{doc_id}/file").status_code == 404
    assert app.app.test_client().get(f"/driver-documents/{doc_id}/file").status_code == 302   # logged out


def test_drivers_cannot_use_the_admin_document_routes(client, app):
    email = _driver(client, app)
    _upload(client, "aadhaar")
    doc_id = app.documents_collection.find_one({"driver_email": email})["_id"]
    assert client.get(f"/admin/driver-documents/{doc_id}/file").status_code == 302
    response = client.post(f"/admin/driver-documents/{doc_id}/approve", headers={"X-Requested-With": "fetch"})
    assert response.status_code == 401
    assert app.documents_collection.find_one({"_id": doc_id})["status"] == "Pending Review"


def test_the_old_public_static_url_is_blocked(client, app, tmp_path, monkeypatch):
    """A document uploaded before the Documents Centre sits under /static. It must
    stay unreachable by URL (the control request proves static serving works here)."""
    static = tmp_path / "static"
    (static / "uploads" / "driver_documents").mkdir(parents=True)
    (static / "css").mkdir()
    (static / "css" / "ok.css").write_text("a{}")
    (static / "uploads" / "driver_documents" / "legacy.pdf").write_bytes(PDF)
    monkeypatch.setattr(app.app, "static_folder", str(static))
    assert client.get("/static/css/ok.css").status_code == 200
    assert client.get("/static/uploads/driver_documents/legacy.pdf").status_code == 404


def test_a_tampered_file_path_can_never_be_served(client, app, tmp_path):
    email = _driver(client, app)
    secret = tmp_path / "secret.pdf"
    secret.write_bytes(PDF)
    for evil in ("../../../../etc/passwd", "../.env", str(secret), "/etc/passwd"):
        doc_id = app.documents_collection.insert_one({
            "driver_email": email, "document_type": "aadhaar", "file_path": evil, "status": "Pending Review",
            "uploaded_at": datetime.now()}).inserted_id
        assert client.get(f"/driver-documents/{doc_id}/file").status_code == 404
        app.documents_collection.delete_one({"_id": doc_id})


def test_replacing_a_verified_document_goes_back_to_review_and_removes_the_old_file(client, app, tmp_path):
    email = _driver(client, app)
    _upload(client, "aadhaar")
    app.documents_collection.update_one({"driver_email": email}, {"$set": {"status": "Verified", "reviewed_by": "admin"}})
    first_file = os.listdir(tmp_path / "docs")[0]
    _upload(client, "aadhaar", content=_png_bytes(), filename="id.png")
    doc = app.documents_collection.find_one({"driver_email": email})
    assert doc["status"] == "Pending Review" and doc["reviewed_by"] is None
    assert app.documents_collection.count_documents({"driver_email": email}) == 1
    assert os.listdir(tmp_path / "docs") == [doc["stored_name"]] and first_file != doc["stored_name"]


def test_admin_can_approve_and_the_driver_is_told(client, app):
    email = _driver(client, app)
    _upload(client, "aadhaar")
    doc_id = app.documents_collection.find_one({"driver_email": email})["_id"]
    admin = _admin_client(app)
    body = admin.post(f"/admin/driver-documents/{doc_id}/approve", headers={"X-Requested-With": "fetch"}).get_json()
    assert body["ok"] is True
    doc = app.documents_collection.find_one({"_id": doc_id})
    assert doc["status"] == "Verified" and doc["reviewed_by"] == "admin@aquaflow.com" and doc["reviewed_at"]
    note = app.notifications_collection.find_one({"driver_email": email, "type": "document_review"})
    assert "verified" in note["title"].lower()
    assert "Verified" in client.get("/driver-documents").get_data(as_text=True)
    assert admin.get(f"/admin/driver-documents/{doc_id}/file").data == PDF         # admin can open it


def test_admin_rejection_needs_a_reason_and_the_driver_sees_it(client, app):
    email = _driver(client, app)
    _upload(client, "aadhaar")
    doc_id = app.documents_collection.find_one({"driver_email": email})["_id"]
    admin = _admin_client(app)
    url = f"/admin/driver-documents/{doc_id}/reject"
    no_reason = admin.post(url, headers={"X-Requested-With": "fetch"})
    assert no_reason.status_code == 400 and app.documents_collection.find_one({"_id": doc_id})["status"] == "Pending Review"
    ok = admin.post(url, data={"reason": "Photo is blurry"}, headers={"X-Requested-With": "fetch"})
    assert ok.get_json()["ok"] is True
    html = client.get("/driver-documents").get_data(as_text=True)
    assert "Rejected" in html and "Photo is blurry" in html
    assert "Photo is blurry" in app.notifications_collection.find_one({"driver_email": email, "type": "document_review"})["message"]


def test_a_reviewed_document_cannot_be_flipped_again(client, app):
    email = _driver(client, app)
    _upload(client, "aadhaar")
    doc_id = app.documents_collection.find_one({"driver_email": email})["_id"]
    admin = _admin_client(app)
    admin.post(f"/admin/driver-documents/{doc_id}/approve", headers={"X-Requested-With": "fetch"})
    again = admin.post(f"/admin/driver-documents/{doc_id}/reject", data={"reason": "changed my mind"}, headers={"X-Requested-With": "fetch"})
    assert again.status_code == 409
    assert app.documents_collection.find_one({"_id": doc_id})["status"] == "Verified"


def test_admin_review_rejects_unknown_actions_and_ids(client, app):
    admin = _admin_client(app)
    assert admin.post(f"/admin/driver-documents/{ObjectId()}/approve", headers={"X-Requested-With": "fetch"}).status_code == 404
    assert admin.post("/admin/driver-documents/zzz/approve", headers={"X-Requested-With": "fetch"}).status_code == 404
    assert admin.post(f"/admin/driver-documents/{ObjectId()}/delete", headers={"X-Requested-With": "fetch"}).status_code == 400


def test_expired_documents_show_as_expired_and_warn_once(client, app):
    email = _driver(client, app)
    app.documents_collection.insert_one({
        "driver_email": email, "document_type": "driving_license", "status": "Verified",
        "file_path": "x", "uploaded_at": datetime.now(), "expiry_date": "2020-01-01"})
    app.documents_collection.insert_one({
        "driver_email": email, "document_type": "vehicle_insurance", "status": "Verified",
        "file_path": "x", "uploaded_at": datetime.now(), "expiry_date": _future(10)})
    html = client.get("/driver-documents").get_data(as_text=True)
    assert "Expired" in html and "10 days left" in html
    client.get("/driver-documents")
    client.get("/driver-dashboard")
    notes = list(app.notifications_collection.find({"driver_email": email, "type": "document_expiry"}))
    assert len(notes) == 2                                       # one per document, not one per page view


def test_legacy_documents_without_new_fields_still_render(client, app):
    email = _driver(client, app)
    app.documents_collection.insert_one({
        "driver_email": email, "document_type": "vehicle_rc", "status": "Pending",
        "file_path": "static/uploads/driver_documents/old.pdf", "uploaded_at": datetime.now()})
    html = client.get("/driver-documents").get_data(as_text=True)
    assert "Pending Review" in html and "couldn't be found" in html


def test_documents_page_empty_state_lists_every_type_as_not_uploaded(client, app):
    login_as_driver(client, app)
    html = client.get("/driver-documents").get_data(as_text=True)
    assert html.count("Not uploaded") >= 4 and "Driving licence" in html and "Vehicle insurance" in html


def test_admin_driver_management_page_links_documents_through_the_protected_route(client, app):
    email = _driver(client, app, approval_status="Approved")
    _upload(client, "aadhaar")
    doc_id = app.documents_collection.find_one({"driver_email": email})["_id"]
    admin = _admin_client(app)
    html = admin.get("/driver-management").get_data(as_text=True)
    assert f"/admin/driver-documents/{doc_id}/file" in html
    assert "/static/uploads/driver_documents" not in html
    assert f"reviewDriverDoc('{doc_id}','approve')" in html


# ============================================================== support

SUPPORT_OK = {"category": "delivery", "subject": "Customer not answering", "message": "I reached the address but nobody answers the phone."}


def test_support_request_is_saved_before_it_is_called_sent(client, app):
    email = _driver(client, app)
    body = client.post("/api/driver/support", json=SUPPORT_OK).get_json()
    assert body["success"] is True and body["ticket"].startswith("SR-") and body["emailed"] is False
    assert "emailed" not in body["message"]                       # no SUPPORT_EMAIL set: must not claim it was
    saved = app.driver_support_requests_collection.find_one({"ticket": body["ticket"]})
    assert saved["driver_email"] == email and saved["status"] == "Open" and saved["category"] == "delivery"
    assert app.notifications_collection.find_one({"type": "driver_support"})
    assert body["ticket"] in client.get("/driver-support").get_data(as_text=True)


def test_support_email_is_only_claimed_when_it_really_went_out(client, app, monkeypatch):
    import driver_portal_secondary as secondary
    _driver(client, app)
    monkeypatch.setenv("SUPPORT_EMAIL", "help@example.com")
    monkeypatch.setattr(secondary, "send_mail_capped", lambda *a, **k: True)
    assert "also been emailed" in client.post("/api/driver/support", json=SUPPORT_OK).get_json()["message"]
    monkeypatch.setattr(secondary, "send_mail_capped", lambda *a, **k: False)
    assert "emailed" not in client.post("/api/driver/support", json=SUPPORT_OK).get_json()["message"]


@pytest.mark.parametrize("override,field", [
    ({"category": "nonsense"}, "category"), ({"category": ""}, "category"),
    ({"subject": "hi"}, "subject"), ({"message": "too short"}, "message"),
    ({"message": "x" * 1001}, "message"),
])
def test_support_validation(client, app, override, field):
    _driver(client, app)
    response = client.post("/api/driver/support", json={**SUPPORT_OK, **override})
    assert response.status_code == 400 and field in response.get_json()["errors"]
    assert app.driver_support_requests_collection.count_documents({}) == 0


def test_support_booking_reference_must_belong_to_the_driver(client, app):
    email = _driver(client, app)
    mine = _booking(app, email)
    theirs = _booking(app, "other@example.com")
    bad = client.post("/api/driver/support", json={**SUPPORT_OK, "booking_ref": str(theirs)[:8]})
    assert bad.status_code == 400 and "booking_ref" in bad.get_json()["errors"]
    good = client.post("/api/driver/support", json={**SUPPORT_OK, "booking_ref": "#" + str(mine)[:8]}).get_json()
    assert app.driver_support_requests_collection.find_one({"ticket": good["ticket"]})["booking_id"] == str(mine)


def test_support_is_rate_limited(client, app):
    _driver(client, app)
    for _ in range(5):
        assert client.post("/api/driver/support", json=SUPPORT_OK).status_code == 200
    assert client.post("/api/driver/support", json=SUPPORT_OK).status_code == 429
    assert app.driver_support_requests_collection.count_documents({}) == 5


def test_emergency_alert_is_recorded_with_location_and_is_honest_about_scope(client, app):
    email = _driver(client, app, current_lat=13.93, current_lng=75.57)
    body = client.post("/api/driver/support/emergency", json={"message": "Tyre burst on the highway"}).get_json()
    assert body["success"] is True and "does not contact emergency services" in body["message"]
    record = app.driver_support_requests_collection.find_one({"ticket": body["ticket"]})
    assert record["priority"] == "high" and record["last_known_location"]["lat"] == 13.93
    legacy = app.notifications_collection.find_one({"type": "emergency", "driver_email": email})
    assert legacy["message"] == "Tyre burst on the highway" and legacy["priority"] == "high"
    assert client.post("/api/driver/support/emergency", json={}).status_code == 429          # cooldown


def test_admin_support_queue_lists_and_resolves(client, app):
    _driver(client, app)
    ticket = client.post("/api/driver/support", json=SUPPORT_OK).get_json()["ticket"]
    rid = app.driver_support_requests_collection.find_one({"ticket": ticket})["_id"]
    assert app.app.test_client().get("/admin/driver-support").status_code == 302         # not an admin
    assert client.post(f"/admin/driver-support/{rid}/resolve").status_code == 401         # a driver isn't an admin
    admin = _admin_client(app)
    assert ticket in admin.get("/admin/driver-support").get_data(as_text=True)
    assert admin.post(f"/admin/driver-support/{rid}/resolve").get_json()["ok"] is True
    assert admin.post(f"/admin/driver-support/{rid}/resolve").status_code == 409          # already resolved
    assert "Resolved" in client.get("/driver-support").get_data(as_text=True)
    assert ticket not in admin.get("/admin/driver-support").get_data(as_text=True)        # default view = Open only


def test_support_page_faq_quotes_the_real_commission_rate(client, app):
    import payment_admin
    login_as_driver(client, app)
    html = client.get("/driver-support").get_data(as_text=True)
    assert f"{round(payment_admin.DRIVER_SHARE_RATE * 100)}% of each delivery fee" in html
    assert "112" in html and "does not contact emergency services" in html


# ================================================================== CSRF

def _csrf_on(monkeypatch, app):
    monkeypatch.setitem(app.app.config, "WTF_CSRF_ENABLED", True)


def _token(client):
    client.get("/driver-profile")
    with client.session_transaction() as sess:
        return sess["_driver_csrf"]


def test_state_changes_without_a_csrf_token_are_refused(client, app, monkeypatch):
    email = _driver(client, app)
    _csrf_on(monkeypatch, app)
    response = client.post("/api/driver/profile", json=PROFILE_OK)
    assert response.status_code == 400 and response.get_json()["csrf"] is True
    assert app.drivers_collection.find_one({"email": email})["phone"] == "9876543210"


def test_wrong_csrf_token_is_refused_and_right_one_is_accepted(client, app, monkeypatch):
    _driver(client, app)
    token = _token(client)
    _csrf_on(monkeypatch, app)
    assert client.post("/api/driver/profile", json=PROFILE_OK, headers={"X-CSRF-Token": "nope"}).status_code == 400
    assert client.post("/api/driver/profile", json=PROFILE_OK, headers={"X-CSRF-Token": token}).status_code == 200


def test_csrf_token_in_a_form_field_is_accepted(client, app, monkeypatch):
    _driver(client, app)
    token = _token(client)
    _csrf_on(monkeypatch, app)
    response = client.post("/driver/bank-details", data={"csrf_token": token, "account_holder_name": "K H",
                           "account_number": "123456789012", "ifsc": "HDFC0001234"})
    assert response.status_code == 302
    assert client.post("/driver/bank-details", data={"account_holder_name": "K H"}).status_code == 400


def test_existing_delivery_endpoints_are_protected_too(client, app, monkeypatch):
    email = _driver(client, app)
    booking = _booking(app, email, status="On The Way", delivery_otp="482913")
    _csrf_on(monkeypatch, app)
    response = client.post(f"/verify-delivery-otp/{booking}", data={"otp": "482913"})
    assert response.status_code == 400
    assert app.bookings_collection.find_one({"_id": booking})["status"] == "On The Way"


def test_csrf_does_not_apply_to_reads_or_to_other_portals(client, app, monkeypatch):
    _driver(client, app)
    _csrf_on(monkeypatch, app)
    assert client.get("/driver-dashboard").status_code == 200
    anonymous = app.app.test_client()
    assert anonymous.post("/login", data={"email": "a@b.c", "password": "x"}).status_code != 400
