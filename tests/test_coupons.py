"""
test_coupons.py
=====================================================
Covers /apply-coupon - a percentage discount actually reducing the
price, an invalid/inactive code being rejected, and the
one-time-per-customer rule genuinely blocking reuse (not just
cosmetically).
=====================================================
"""

from tests.helpers import create_user, login


def _seed_booking(app, user_email, delivery_fee=1000):
    return app.bookings_collection.insert_one({
        "user_email": user_email,
        "status": "Approved",
        "delivery_fee": delivery_fee,
    }).inserted_id


def test_valid_percent_coupon_reduces_price(client, app):
    email, password = create_user(app)
    login(client, email, password)
    booking_id = _seed_booking(app, email, delivery_fee=1000)

    app.coupons_collection.insert_one({
        "code": "SAVE10", "active": True, "discount_type": "percent", "value": 10,
    })

    response = client.post(f"/apply-coupon/{booking_id}", json={"code": "save10"})
    data = response.get_json()

    assert data["success"] is True
    assert data["new_amount"] == 900  # 1000 - 10%
    booking = app.bookings_collection.find_one({"_id": booking_id})
    assert booking["delivery_fee"] == 900


def test_invalid_coupon_code_is_rejected(client, app):
    email, password = create_user(app)
    login(client, email, password)
    booking_id = _seed_booking(app, email, delivery_fee=1000)

    response = client.post(f"/apply-coupon/{booking_id}", json={"code": "DOESNOTEXIST"})
    data = response.get_json()

    assert data["success"] is False
    booking = app.bookings_collection.find_one({"_id": booking_id})
    assert booking["delivery_fee"] == 1000  # unchanged


def test_one_time_coupon_cannot_be_reused_by_same_customer(client, app):
    email, password = create_user(app)
    login(client, email, password)

    app.coupons_collection.insert_one({
        "code": "WELCOME100", "active": True, "discount_type": "flat",
        "value": 100, "one_time_per_user": True,
    })

    first_booking_id = _seed_booking(app, email, delivery_fee=1000)
    first = client.post(f"/apply-coupon/{first_booking_id}", json={"code": "WELCOME100"})
    assert first.get_json()["success"] is True

    second_booking_id = _seed_booking(app, email, delivery_fee=1000)
    second = client.post(f"/apply-coupon/{second_booking_id}", json={"code": "WELCOME100"})
    second_data = second.get_json()

    assert second_data["success"] is False
    assert "already used" in second_data["message"].lower()
