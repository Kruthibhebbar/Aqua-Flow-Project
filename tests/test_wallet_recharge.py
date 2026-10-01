"""
test_wallet_recharge.py
=====================================================
Covers /wallet/recharge/verify. This doesn't call the real Razorpay
API - instead it computes the SAME signature Razorpay itself would
send (HMAC-SHA256 of "order_id|payment_id", signed with the secret
key - see wallet_recharge.py), so this genuinely exercises the real
verification logic instead of trusting a mock.
=====================================================
"""

import hashlib
import hmac
import os

from tests.helpers import create_user, login


def _sign(order_id, payment_id, secret=None):
    secret = secret or os.environ["RAZORPAY_KEY_SECRET"]
    return hmac.new(
        secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256
    ).hexdigest()


def _seed_topup(app, user_email, amount=500, order_id="order_test123"):
    return app.wallet_topups_collection.insert_one({
        "user_email": user_email,
        "amount": amount,
        "razorpay_order_id": order_id,
        "status": "created",
    }).inserted_id


def test_correct_signature_credits_the_wallet(client, app):
    email, password = create_user(app)
    login(client, email, password)
    topup_id = _seed_topup(app, email, amount=500)

    signature = _sign("order_test123", "pay_test456")
    response = client.post("/wallet/recharge/verify", json={
        "topup_id": str(topup_id),
        "razorpay_order_id": "order_test123",
        "razorpay_payment_id": "pay_test456",
        "razorpay_signature": signature,
    })
    data = response.get_json()

    assert data["success"] is True
    user = app.users_collection.find_one({"email": email})
    assert user.get("wallet_balance") == 500


def test_forged_signature_is_rejected_and_credits_nothing(client, app):
    email, password = create_user(app)
    login(client, email, password)
    topup_id = _seed_topup(app, email, amount=500)

    response = client.post("/wallet/recharge/verify", json={
        "topup_id": str(topup_id),
        "razorpay_order_id": "order_test123",
        "razorpay_payment_id": "pay_test456",
        "razorpay_signature": "totally-made-up-signature",
    })
    data = response.get_json()

    assert data["success"] is False
    user = app.users_collection.find_one({"email": email})
    assert user.get("wallet_balance", 0) == 0


def test_already_paid_topup_is_not_credited_twice(client, app):
    """Simulates Razorpay (or a flaky network) retrying the same
    callback twice - the wallet should only ever be credited once."""
    email, password = create_user(app)
    login(client, email, password)
    topup_id = app.wallet_topups_collection.insert_one({
        "user_email": email,
        "amount": 500,
        "razorpay_order_id": "order_test123",
        "status": "paid",  # already processed by an earlier call
    }).inserted_id
    app.users_collection.update_one({"email": email}, {"$set": {"wallet_balance": 500}})

    signature = _sign("order_test123", "pay_test456")
    response = client.post("/wallet/recharge/verify", json={
        "topup_id": str(topup_id),
        "razorpay_order_id": "order_test123",
        "razorpay_payment_id": "pay_test456",
        "razorpay_signature": signature,
    })
    data = response.get_json()

    assert data.get("already_credited") is True
    user = app.users_collection.find_one({"email": email})
    assert user["wallet_balance"] == 500  # NOT 1000 - not credited a second time
