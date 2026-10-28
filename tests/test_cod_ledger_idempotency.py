"""
One cash deposit == ONE ledger row for its whole life.

Regression for: a driver deposit showed twice in Recent Financial Activity -
one stale "Waiting Admin Approval" row and one "Verified" row - because
approval INSERTED a second ledger row instead of updating the submission's.
"""

from datetime import datetime, timedelta

from bson.objectid import ObjectId

from tests.helpers import login_as_admin, login_as_driver
from payment_ledger import (
    cod_status_label, find_duplicate_cod_rows, supersede_duplicate_cod_rows,
)

DRIVER = "ravi@example.com"


def _seed(app, delivered=True):
    app.drivers_collection.insert_one({
        "name": "Ravi Kumar", "email": DRIVER, "phone": "9876500001",
        "approval_status": "Approved", "online_status": "Online",
    })
    return str(app.bookings_collection.insert_one({
        "status": "Delivered" if delivered else "Out for Delivery", "driver_email": DRIVER,
        "delivery_fee": 1475, "user_email": "cust@example.com", "delivered_at": datetime.now(),
    }).inserted_id)


def _submit(client, booking_id, ref="UPI0001", amount="1475"):
    return client.post(f"/api/submit-collected-payment/{booking_id}",
                       json={"amount": amount, "method": "UPI", "transaction_ref": ref})


def _rows(app, booking_id=None):
    q = {"type": "cod_collection"}
    if booking_id:
        q["booking_id"] = booking_id
    return list(app.payment_transactions_collection.find(q))


def _submit_as_driver(client, app, booking_id, **kw):
    login_as_driver(client, app, DRIVER)
    return _submit(client, booking_id, **kw)


def _activity_html(client):
    return client.get("/finance-management").get_data(as_text=True)


# ---------------------------------------------------------------- submission

def test_submission_creates_exactly_one_pending_ledger_row(client, app):
    bid = _seed(app)
    assert _submit_as_driver(client, app, bid).get_json()["success"] is True
    rows = _rows(app, bid)
    assert len(rows) == 1 and rows[0]["status"] == "Waiting Admin Approval"
    assert rows[0]["idempotency_key"] == f"cod:{bid}:1"


def test_duplicate_submission_does_not_create_a_second_row(client, app):
    bid = _seed(app)
    first = _submit_as_driver(client, app, bid)
    second = _submit(client, bid)                    # double tap / retry / second tab
    assert first.status_code == 200 and second.status_code == 400
    assert len(_rows(app, bid)) == 1
    assert app.notifications_collection.count_documents({"type": "payment_submission"}) == 1


def test_idempotency_key_blocks_a_raced_second_insert(app):
    from payment_ledger import record_cod_submission
    bid = str(ObjectId())
    a = record_cod_submission(app.payment_transactions_collection, bid, 1, amount=1475, driver_email=DRIVER)
    b = record_cod_submission(app.payment_transactions_collection, bid, 1, amount=1475, driver_email=DRIVER)
    assert a == b and len(_rows(app, bid)) == 1


# ---------------------------------------------------------------- approval

def test_approval_updates_the_same_row_to_verified(client, app):
    bid = _seed(app)
    _submit_as_driver(client, app, bid, ref="UPI-A1")
    original = _rows(app, bid)[0]

    login_as_admin(client, app)
    client.post(f"/admin/verify-payment/{bid}", data={"action": "approve"})

    rows = _rows(app, bid)
    assert len(rows) == 1                                   # NOT a second record
    assert rows[0]["transaction_id"] == original["transaction_id"]
    assert rows[0]["status"] == "Settled"                   # one authoritative status
    assert rows[0]["updated_at"] >= original["updated_at"]
    assert "UPI-A1" in rows[0]["notes"]                     # submission reference preserved


def test_repeated_approval_changes_nothing(client, app):
    bid = _seed(app)
    _submit_as_driver(client, app, bid)
    login_as_admin(client, app)
    for _ in range(3):
        client.post(f"/admin/verify-payment/{bid}", data={"action": "approve"})
    assert len(_rows(app, bid)) == 1
    assert app.drivers_collection.find_one({"email": DRIVER})["total_remitted_to_company"] == 1475
    assert app.notifications_collection.count_documents({"type": "payment_verified"}) == 1


def test_rejection_updates_same_row_and_resubmission_gets_its_own_row(client, app):
    bid = _seed(app)
    _submit_as_driver(client, app, bid, ref="BAD1")
    login_as_admin(client, app)
    client.post(f"/admin/verify-payment/{bid}", data={"action": "reject"})
    rows = _rows(app, bid)
    assert len(rows) == 1 and rows[0]["status"] == "Rejected"

    assert _submit_as_driver(client, app, bid, ref="GOOD2").get_json()["success"] is True
    assert sorted(r["status"] for r in _rows(app, bid)) == ["Rejected", "Waiting Admin Approval"]
    assert {r["idempotency_key"] for r in _rows(app, bid)} == {f"cod:{bid}:1", f"cod:{bid}:2"}


def test_two_deposits_with_the_same_amount_are_both_kept(client, app):
    """Same amount is NOT a duplicate: different bookings are different deposits."""
    b1, b2 = _seed(app), str(app.bookings_collection.insert_one({
        "status": "Delivered", "driver_email": DRIVER, "delivery_fee": 1475, "user_email": "c2@example.com",
        "delivered_at": datetime.now()}).inserted_id)
    _submit_as_driver(client, app, b1)
    _submit(client, b2)
    login_as_admin(client, app)
    client.post(f"/admin/verify-payment/{b1}", data={"action": "approve"})
    rows = _rows(app)
    assert len(rows) == 2
    assert sorted(r["status"] for r in rows) == ["Settled", "Waiting Admin Approval"]


# ---------------------------------------------------------------- displayed status

def test_activity_table_shows_verified_immediately_and_one_row(client, app):
    bid = _seed(app)
    _submit_as_driver(client, app, bid, ref="UPI-SHOW")
    login_as_admin(client, app)

    html = _activity_html(client)                       # pending: listed once, as Pending
    assert html.count("UPI-SHOW") >= 1
    assert "Waiting Admin Approval" not in html
    assert app.payment_transactions_collection.count_documents({"booking_id": bid}) == 1

    client.post(f"/admin/verify-payment/{bid}", data={"action": "approve"})
    panel = _activity_html(client).split('id="panel-overview"')[1].split('id="panel-payouts"')[0]
    assert panel.count("Cash deposit") == 1
    assert "Verified" in panel and "Pending" not in panel


def test_status_labels_come_from_one_vocabulary():
    assert cod_status_label("Waiting Admin Approval") == "Pending"
    assert cod_status_label("Settled") == "Verified"
    assert cod_status_label("Rejected") == "Rejected"


# ---------------------------------------------------------------- repairing existing duplicates

def _legacy_pair(app, bid, amount=1475, driver=DRIVER):
    """What the OLD code wrote: pending row + a second Settled row, no idempotency keys."""
    now = datetime.now()
    coll = app.payment_transactions_collection
    coll.insert_one({"transaction_id": "TXN53B50BAC8D38", "type": "cod_collection", "status": "Waiting Admin Approval",
                     "amount": amount, "booking_id": bid, "driver_email": driver, "notes": "UPI ref: R77",
                     "created_at": now - timedelta(minutes=2), "updated_at": now - timedelta(minutes=2)})
    coll.insert_one({"transaction_id": "TXN33BFF7E08C03", "type": "cod_collection", "status": "Settled",
                     "amount": amount, "booking_id": bid, "driver_email": driver,
                     "created_at": now - timedelta(minutes=1), "updated_at": now - timedelta(minutes=1)})


def test_finds_and_retires_only_the_stale_duplicate_without_deleting(app):
    coll = app.payment_transactions_collection
    b_dup, b_other = str(ObjectId()), str(ObjectId())
    _legacy_pair(app, b_dup)
    # a genuine, separate pending deposit of the same amount on another booking
    coll.insert_one({"transaction_id": "TXNREALPENDING", "type": "cod_collection", "status": "Waiting Admin Approval",
                     "amount": 1475, "booking_id": b_other, "driver_email": DRIVER,
                     "created_at": datetime.now(), "updated_at": datetime.now()})
    before = coll.count_documents({})

    pairs = find_duplicate_cod_rows(coll)
    assert [(p["stale"]["transaction_id"], p["kept"]["transaction_id"]) for p in pairs] == \
           [("TXN53B50BAC8D38", "TXN33BFF7E08C03")]

    assert supersede_duplicate_cod_rows(coll, pairs) == 1
    assert coll.count_documents({}) == before                           # nothing deleted
    assert coll.find_one({"transaction_id": "TXN53B50BAC8D38"})["status"] == "Superseded"
    kept = coll.find_one({"transaction_id": "TXN33BFF7E08C03"})
    assert kept["status"] == "Settled" and kept["notes"] == "UPI ref: R77"   # reference carried over
    assert coll.find_one({"transaction_id": "TXNREALPENDING"})["status"] == "Waiting Admin Approval"
    assert find_duplicate_cod_rows(coll) == []                          # idempotent re-run
    assert supersede_duplicate_cod_rows(coll, pairs) == 0


def test_same_booking_but_different_amount_is_not_treated_as_duplicate(app):
    _legacy_pair(app, str(ObjectId()))
    coll = app.payment_transactions_collection
    coll.update_one({"transaction_id": "TXN33BFF7E08C03"}, {"$set": {"amount": 999}})
    assert find_duplicate_cod_rows(coll) == []


def test_activity_hides_superseded_but_keeps_the_verified_row(client, app):
    app.drivers_collection.insert_one({"name": "Ravi Kumar", "email": DRIVER, "approval_status": "Approved"})
    _legacy_pair(app, str(ObjectId()))
    login_as_admin(client, app)
    before = _activity_html(client).split('id="panel-overview"')[1].split('id="panel-payouts"')[0]
    assert before.count("Cash deposit") == 2 and "Verified" in before    # the screenshot's problem

    coll = app.payment_transactions_collection
    supersede_duplicate_cod_rows(coll, find_duplicate_cod_rows(coll))
    after = _activity_html(client).split('id="panel-overview"')[1].split('id="panel-payouts"')[0]
    assert after.count("Cash deposit") == 1 and "Verified" in after and "Superseded" not in after


def test_cli_dry_run_changes_nothing_and_apply_resolves(app, capsys):
    from tools.resolve_duplicate_cod_ledger import main
    coll = app.payment_transactions_collection
    _legacy_pair(app, str(ObjectId()))
    main([], coll=coll)
    assert "Dry run" in capsys.readouterr().out
    assert coll.count_documents({"status": "Superseded"}) == 0
    main(["--apply"], coll=coll)
    assert coll.count_documents({"status": "Superseded"}) == 1
