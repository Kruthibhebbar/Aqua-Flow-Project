"""
Finance Management tests: access control, navigation, bank details,
payout safety (no double-pay, reference required), COD single-decision,
history filters/pagination/export, and the Indian-rupee formatter.
"""

from datetime import datetime, timedelta

from bson.objectid import ObjectId

from tests.helpers import login_as_admin
from finance_management import format_inr

DRIVER = "driver1@example.com"


def _admin(client, app):
    login_as_admin(client, app)


def _seed_driver(app, email=DRIVER, name="Ravi Kumar", phone="9876500001", bank=True):
    doc = {"email": email, "name": name, "phone": phone, "approval_status": "Approved", "password": "HASH"}
    if bank:
        doc["bank_details"] = {
            "account_holder_name": name, "bank_name": "Canara Bank",
            "account_number": "123456789012", "ifsc": "CNRB0001234",
            "approval_status": "Approved",
        }
    app.drivers_collection.insert_one(doc)


def _seed_settlement(app, amount=500, status="Pending", email=DRIVER, when=None):
    """A driver payout request exactly as request_driver_withdrawal() creates it."""
    from payment_ledger import log_transaction
    ledger_id = log_transaction(
        app.payment_transactions_collection, type="withdrawal",
        status="Pending", amount=amount, driver_email=email, gateway="bank_transfer",
    )
    res = app.transactions_collection.insert_one({
        "type": "settlement", "driver_email": email, "amount": amount, "status": status,
        "requested_at": when or datetime.now(), "payment_transaction_id": ledger_id,
    })
    return str(res.inserted_id), ledger_id


def _complete(client, rid, ref="UTR123456789", follow=True):
    return client.post(f"/admin/settlement/{rid}/complete", data={"bank_reference": ref}, follow_redirects=follow)


# ---------------- access control ----------------

def test_finance_pages_require_admin(client):
    for url in ("/finance-management", "/finance-management/api/summary",
                "/finance-management/export/payouts.csv", "/finance-management/export/cash-verifications.csv",
                "/admin/finance/reveal-company-account", "/admin/finance/reveal-driver-account/x@y.com"):
        r = client.get(url)
        assert r.status_code in (302, 401), url
        if r.status_code == 302:
            assert "admin-login" in r.headers["Location"], url


def test_driver_details_requires_admin_and_hides_secrets(client, app):
    _seed_driver(app)
    did = str(app.drivers_collection.find_one({"email": DRIVER})["_id"])
    assert client.get(f"/driver-details/{did}").status_code == 401
    _admin(client, app)
    data = client.get(f"/driver-details/{did}").get_json()
    assert data["success"] is True
    assert "password" not in data["driver"] and "bank_details" not in data["driver"]
    assert client.get("/driver-details/not-an-id").get_json()["success"] is False


# ---------------- navigation ----------------

def test_dashboard_has_single_finance_entry_using_the_real_route(client, app):
    _admin(client, app)
    html = client.get("/admin-dashboard").get_data(as_text=True)
    assert html.count('href="/finance-management"') == 1   # updated in place, not duplicated
    assert "fa-wallet" in html
    assert 'href="/finance-management"' in html


def test_every_finance_page_links_straight_to_admin_dashboard(client, app):
    _admin(client, app)
    _seed_driver(app)
    rid, _ = _seed_settlement(app, status="Completed")
    _complete_ledger = app.payment_transactions_collection  # receipt needs nothing more
    assert _complete_ledger is not None
    pages = ["/finance-management", "/transaction-table", f"/admin/settlement/{rid}/receipt", "/payment_system"]
    for url in pages:
        html = client.get(url).get_data(as_text=True)
        assert 'href="/admin-dashboard"' in html, url
        assert 'id="finDashboardLink"' in html or "fin-nav-dashboard" in html, url
    # the control must not depend on browser history
    page = client.get("/finance-management").get_data(as_text=True)
    link = page[page.index('id="finDashboardLink"') - 120: page.index('id="finDashboardLink"') + 250]
    for banned in ("history.back", "history.go", "document.referrer", "onclick"):
        assert banned not in link


def test_internal_navigation_never_uses_history_api_for_dashboard(client, app):
    _admin(client, app)
    html = client.get("/finance-management").get_data(as_text=True)
    assert "history.back" not in html and "history.go(" not in html and "document.referrer" not in html
    assert "pushState" not in html  # tabs/filters use replaceState / location.replace only


# ---------------- page renders with real data ----------------

def test_finance_page_shows_inr_formatted_real_values(client, app):
    _admin(client, app)
    _seed_driver(app)
    app.bookings_collection.insert_one({
        "status": "Delivered", "driver_email": DRIVER, "delivery_fee": 125000,
        "delivered_at": datetime.now(), "payment_status": "Paid (Online)",
    })
    html = client.get("/finance-management").get_data(as_text=True)
    assert "₹1,25,000.00" in html          # today's revenue, Indian grouping
    assert "₹12,500.00" in html            # profit = 10% commission
    assert "₹0.00" in html                 # zero values formatted, not blank


def test_format_inr_edge_cases():
    assert format_inr(125000) == "₹1,25,000.00"
    assert format_inr(0) == "₹0.00"
    assert format_inr(None) == "₹0.00"
    assert format_inr("junk") == "₹0.00"
    assert format_inr(12345678.9) == "₹1,23,45,678.90"
    assert format_inr(-1500) == "-₹1,500.00"


def test_summary_api_returns_formatted_figures(client, app):
    _admin(client, app)
    data = client.get("/finance-management/api/summary").get_json()
    assert data["ok"] is True and data["values"]["profit"] == "₹0.00"


# ---------------- company bank details ----------------

GOOD_BANK = {"bank_name": "Canara Bank", "account_holder": "AquaFlow", "account_number": "123456789012",
             "ifsc": "cnrb0001234", "upi_id": "aquaflow@upi"}


def _save_bank(client, **over):
    return client.post("/admin/bank-details", data={**GOOD_BANK, **over}, headers={"X-Requested-With": "fetch"})


def test_bank_details_save_masks_and_reload(client, app):
    _admin(client, app)
    r = _save_bank(client)
    body = r.get_json()
    assert r.status_code == 200 and body["ok"] is True
    assert body["bank"]["account_number_masked"] == "XXXXXXXX9012"
    assert "123456789012" not in r.get_data(as_text=True)
    stored = app.settings_collection.find_one({"_id": "company_bank_details"})
    assert stored["ifsc"] == "CNRB0001234" and stored["account_number"] == "123456789012"
    page = client.get("/finance-management").get_data(as_text=True)
    assert "XXXXXXXX9012" in page and "123456789012" not in page   # full number never in the page


def test_blank_account_number_keeps_saved_one(client, app):
    _admin(client, app)
    _save_bank(client)
    r = _save_bank(client, account_number="", keep_account_number="1", bank_name="Canara Bank Ltd")
    assert r.get_json()["ok"] is True
    stored = app.settings_collection.find_one({"_id": "company_bank_details"})
    assert stored["account_number"] == "123456789012" and stored["bank_name"] == "Canara Bank Ltd"


def test_bank_details_validation_errors(client, app):
    _admin(client, app)
    cases = [
        ({"ifsc": "BAD"}, "IFSC"),
        ({"upi_id": "nope"}, "UPI"),
        ({"account_number": "12ab"}, "9-18 digits"),
        ({"account_holder": ""}, "holder name is required"),
        ({"bank_name": ""}, "Bank name is required"),
        ({"account_number": "", "ifsc": "", "upi_id": ""}, "bank account details, a UPI ID"),
    ]
    for over, expect in cases:
        r = _save_bank(client, **over)
        assert r.status_code == 400 and expect in r.get_json()["message"], (over, r.get_json())
    assert app.settings_collection.find_one({"_id": "company_bank_details"}) is None  # nothing saved


def test_bank_details_reject_non_admin_and_json_401(client):
    r = client.post("/admin/bank-details", data=GOOD_BANK, headers={"X-Requested-With": "fetch"})
    assert r.status_code == 401
    r = client.post("/admin/bank-details", data=GOOD_BANK)
    assert r.status_code == 302


def test_reveal_endpoints_are_admin_only_and_return_full_number(client, app):
    _seed_driver(app)
    assert client.get(f"/admin/finance/reveal-driver-account/{DRIVER}").status_code == 401
    _admin(client, app)
    _save_bank(client)
    assert client.get("/admin/finance/reveal-company-account").get_json()["account_number"] == "123456789012"
    assert client.get(f"/admin/finance/reveal-driver-account/{DRIVER}").get_json()["account_number"] == "123456789012"
    assert client.get("/admin/finance/reveal-driver-account/ghost@x.com").status_code == 404


def test_qr_upload_rejects_non_images(client, app):
    import io
    _admin(client, app)
    r = client.post("/admin/bank-details", headers={"X-Requested-With": "fetch"},
                    data={**GOOD_BANK, "qr_image": (io.BytesIO(b"not an image"), "qr.png")},
                    content_type="multipart/form-data")
    assert r.status_code == 400 and "valid image" in r.get_json()["message"]
    r = client.post("/admin/bank-details", headers={"X-Requested-With": "fetch"},
                    data={**GOOD_BANK, "qr_image": (io.BytesIO(b"x"), "qr.exe")},
                    content_type="multipart/form-data")
    assert r.status_code == 400 and "PNG, JPG or WEBP" in r.get_json()["message"]


# ---------------- payout safety ----------------

def test_payout_requires_bank_reference(client, app):
    _admin(client, app); _seed_driver(app)
    rid, _ = _seed_settlement(app)
    r = client.post(f"/admin/settlement/{rid}/complete", data={}, follow_redirects=True)
    assert "transaction reference" in r.get_data(as_text=True)
    assert app.transactions_collection.find_one({"_id": ObjectId(rid)})["status"] == "Pending"
    client.post(f"/admin/settlement/{rid}/complete", data={"bank_reference": "ab"})
    assert app.transactions_collection.find_one({"_id": ObjectId(rid)})["status"] == "Pending"


def test_payout_completes_once_and_records_reference(client, app):
    _admin(client, app); _seed_driver(app)
    rid, ledger_id = _seed_settlement(app, amount=700)
    _complete(client, rid, "utr 123".replace(" ", "") + "456")
    doc = app.transactions_collection.find_one({"_id": ObjectId(rid)})
    assert doc["status"] == "Completed" and doc["bank_reference"] == "UTR123456"
    assert doc["paid_by"] == "admin@aquaflow.com" and doc["paid_at"]
    ledger = app.payment_transactions_collection.find_one({"transaction_id": ledger_id})
    assert ledger["status"] == "Withdrawn" and ledger["bank_reference"] == "UTR123456"
    notes = list(app.notifications_collection.find({"driver_email": DRIVER, "type": "settlement_update"}))
    assert len(notes) == 1


def test_payout_cannot_be_paid_twice_or_reversed(client, app):
    _admin(client, app); _seed_driver(app)
    rid, _ = _seed_settlement(app)
    _complete(client, rid, "UTR111111")
    r = _complete(client, rid, "UTR222222")                      # second pay attempt
    assert "already Completed" in r.get_data(as_text=True)
    client.post(f"/admin/settlement/{rid}/reject")               # cannot reject a paid payout
    doc = app.transactions_collection.find_one({"_id": ObjectId(rid)})
    assert doc["status"] == "Completed" and doc["bank_reference"] == "UTR111111"
    assert app.notifications_collection.count_documents({"driver_email": DRIVER, "type": "settlement_update"}) == 1


def test_bank_reference_cannot_be_reused_on_another_payout(client, app):
    _admin(client, app); _seed_driver(app)
    a, _ = _seed_settlement(app); b, _ = _seed_settlement(app, amount=900)
    _complete(client, a, "UTR555555")
    r = _complete(client, b, "utr555555")
    assert "already recorded" in r.get_data(as_text=True)
    assert app.transactions_collection.find_one({"_id": ObjectId(b)})["status"] == "Pending"


def test_rejected_payout_is_final_and_non_settlements_are_untouchable(client, app):
    _admin(client, app); _seed_driver(app)
    rid, _ = _seed_settlement(app)
    client.post(f"/admin/settlement/{rid}/reject")
    _complete(client, rid, "UTR999999")
    assert app.transactions_collection.find_one({"_id": ObjectId(rid)})["status"] == "Rejected"
    other = str(app.transactions_collection.insert_one({"type": "customer_payment", "status": "Pending", "amount": 5}).inserted_id)
    _complete(client, other, "UTR888888")
    assert app.transactions_collection.find_one({"_id": ObjectId(other)})["status"] == "Pending"


def test_settlement_actions_require_admin(client, app):
    _seed_driver(app)
    rid, _ = _seed_settlement(app)
    r = client.post(f"/admin/settlement/{rid}/complete", data={"bank_reference": "UTR123456"})
    assert r.status_code == 302 and "admin-login" in r.headers["Location"]
    assert app.transactions_collection.find_one({"_id": ObjectId(rid)})["status"] == "Pending"


# ---------------- COD verification ----------------

def _seed_cod(app, amount=800, ref="UPI0001"):
    return str(app.bookings_collection.insert_one({
        "status": "Delivered", "driver_email": DRIVER, "driver_name": "Ravi Kumar", "delivery_fee": amount,
        "payment_submitted": True, "payment_submission_status": "Pending Verification",
        "cash_submitted_amount": amount, "payment_transaction_ref": ref,
        "payment_submitted_at": datetime.now(),
    }).inserted_id)


def test_cod_approval_counts_once(client, app):
    _admin(client, app); _seed_driver(app)
    bid = _seed_cod(app)
    for _ in range(3):
        client.post(f"/admin/verify-payment/{bid}", data={"action": "approve"})
    assert app.drivers_collection.find_one({"email": DRIVER})["total_remitted_to_company"] == 800
    assert app.payment_transactions_collection.count_documents({"type": "cod_collection", "booking_id": bid}) == 1
    assert app.notifications_collection.count_documents({"type": "payment_verified"}) == 1


def test_cod_cannot_flip_decision_and_unknown_action_is_ignored(client, app):
    _admin(client, app); _seed_driver(app)
    bid = _seed_cod(app)
    client.post(f"/admin/verify-payment/{bid}", data={"action": "typo"})
    assert app.bookings_collection.find_one({"_id": ObjectId(bid)})["payment_submission_status"] == "Pending Verification"
    client.post(f"/admin/verify-payment/{bid}", data={"action": "reject", "remarks": "ref mismatch"})
    client.post(f"/admin/verify-payment/{bid}", data={"action": "approve"})
    b = app.bookings_collection.find_one({"_id": ObjectId(bid)})
    assert b["payment_submission_status"] == "Rejected" and b["payment_verification_remarks"] == "ref mismatch"


# ---------------- driver bank approvals ----------------

def test_driver_bank_approval_is_idempotent(client, app):
    _admin(client, app); _seed_driver(app, bank=False)
    app.drivers_collection.update_one({"email": DRIVER}, {"$set": {"bank_details": {
        "account_holder_name": "R", "account_number": "123456789012", "ifsc": "CNRB0001234", "approval_status": "Pending"}}})
    page = client.get("/finance-management").get_data(as_text=True)
    assert "XXXXXXXX9012" in page and "123456789012" not in page
    client.post(f"/admin/driver-bank-details/{DRIVER}/approve")
    client.post(f"/admin/driver-bank-details/{DRIVER}/approve")
    assert app.notifications_collection.count_documents({"type": "bank_details_update"}) == 1


# ---------------- payout history: search / filter / sort / pagination / export ----------------

def _history_rows(app, n=20):
    _seed_driver(app, DRIVER, "Ravi Kumar", "9876500001")
    _seed_driver(app, "d2@example.com", "Meena Shah", "9123400002")
    now = datetime.now()
    for i in range(n):
        email = DRIVER if i % 2 == 0 else "d2@example.com"
        rid, ledger = _seed_settlement(app, amount=100 + i, status="Completed", email=email)
        app.payment_transactions_collection.update_one(
            {"transaction_id": ledger},
            {"$set": {"status": "Withdrawn", "updated_at": now - timedelta(days=i), "bank_reference": f"UTR{1000 + i}"}})


def test_history_is_paginated_and_default_matches_original(client, app):
    _admin(client, app); _history_rows(app, 20)
    page1 = client.get("/finance-management").get_data(as_text=True)
    assert "Showing 1–15 of 20" in page1
    page2 = client.get("/finance-management?h_page=2").get_data(as_text=True)
    assert "Showing 16–20 of 20" in page2


def test_history_search_by_name_phone_and_reference(client, app):
    _admin(client, app); _history_rows(app, 6)
    by_name = client.get("/finance-management?h_q=meena").get_data(as_text=True)
    assert "of 3" in by_name and "Meena Shah" in by_name
    by_phone = client.get("/finance-management?h_q=98765000").get_data(as_text=True)
    assert "of 3" in by_phone
    by_ref = client.get("/finance-management?h_q=UTR1003").get_data(as_text=True)
    assert "of 1" in by_ref and "UTR1003" in by_ref
    none = client.get("/finance-management?h_q=zzzz").get_data(as_text=True)
    assert "No payments match your filters" in none
    assert "of 6" not in client.get("/finance-management?h_q=%5B%28").get_data(as_text=True)  # regex chars are escaped, no crash


def test_history_status_date_and_sort_filters(client, app):
    _admin(client, app); _history_rows(app, 6)
    rid, ledger = _seed_settlement(app, amount=50, status="Rejected")
    app.payment_transactions_collection.update_one({"transaction_id": ledger}, {"$set": {"status": "Rejected"}})
    assert "of 1" in client.get("/finance-management?h_status=rejected").get_data(as_text=True)
    assert "of 7" in client.get("/finance-management?h_status=all").get_data(as_text=True)
    assert "No payments match" in client.get("/finance-management?h_status=processing").get_data(as_text=True)
    d = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    assert "of 3" in client.get(f"/finance-management?h_from={d}").get_data(as_text=True)
    def history_panel(url):
        html = client.get(url).get_data(as_text=True)
        return html[html.index('id="panel-history"'):]
    html = history_panel("/finance-management?h_sort=amount_desc")
    assert html.index("₹105.00") < html.index("₹100.00")
    html = history_panel("/finance-management?h_sort=date_asc")
    assert html.index("UTR1005") < html.index("UTR1000")


def test_processing_payouts_are_labelled_processing_not_paid(client, app):
    _admin(client, app); _seed_driver(app)
    rid, _ = _seed_settlement(app)
    client.post(f"/admin/settlement/{rid}/approve")
    assert app.transactions_collection.find_one({"_id": ObjectId(rid)})["status"] == "Processing"
    assert "Processing" in client.get("/finance-management?h_status=processing").get_data(as_text=True)
    assert "No payments match" in client.get("/finance-management?h_status=paid").get_data(as_text=True)
    client.post(f"/admin/settlement/{rid}/approve")          # approving twice is a no-op
    assert app.notifications_collection.count_documents({"type": "settlement_update"}) == 1


def test_csv_exports_honour_filters_and_neutralise_formulas(client, app):
    _admin(client, app); _history_rows(app, 6)
    app.drivers_collection.update_one({"email": "d2@example.com"}, {"$set": {"name": "=HYPERLINK(\"x\")"}})
    r = client.get("/finance-management/export/payouts.csv?h_q=UTR1003")
    text = r.get_data(as_text=True)
    assert r.mimetype == "text/csv" and text.startswith("\ufeff")
    assert text.count("\n") == 2 and "UTR1003" in text and "UTR1002" not in text
    full = client.get("/finance-management/export/payouts.csv").get_data(as_text=True)
    assert "'=HYPERLINK" in full and ",=HYPERLINK" not in full
    bid = _seed_cod(app)
    client.post(f"/admin/verify-payment/{bid}", data={"action": "approve"})
    cash = client.get("/finance-management/export/cash-verifications.csv").get_data(as_text=True)
    assert "UPI0001" in cash and "Approved" in cash


# ---------------- activity + reconciliation ----------------

def test_recent_activity_and_cash_holders_use_real_records(client, app):
    _admin(client, app); _seed_driver(app)
    app.bookings_collection.insert_one({"status": "Delivered", "driver_email": DRIVER, "delivery_fee": 640,
                                        "delivered_at": datetime.now()})
    _seed_cod(app, amount=800, ref="REFX9")
    html = client.get("/finance-management").get_data(as_text=True)
    assert "Awaiting verification" not in html and "REFX9" in html
    assert "Ravi Kumar" in html and "₹1,440.00" in html     # 640 + 800 outstanding


def test_section_failure_shows_error_state_not_zeros(client, app):
    import finance_management as fm
    _admin(client, app)
    original = fm.get_finance_overview
    def boom(*a, **k): raise RuntimeError("db down")
    fm.get_finance_overview = boom
    try:
        html = client.get("/finance-management").get_data(as_text=True)
    finally:
        fm.get_finance_overview = original
    assert "is-error" in html and "Couldn't load this figure" in html
