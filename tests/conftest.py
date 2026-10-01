"""
conftest.py
=====================================================
Shared setup for every test in this folder. pytest loads this file
automatically - nothing here needs to be imported by hand.

THE BIG IDEA
------------
app.py connects to a real MongoDB database. We don't want tests to
touch your real database (slow, and it would leave fake test users
in your actual customer list). So before app.py is ever imported, we
swap out pymongo.MongoClient for mongomock.MongoClient - a fake,
in-memory MongoDB that behaves the same way but lives only in RAM
and disappears the moment the test finishes.

Because app.py does `client = pymongo.MongoClient(...)` at import
time, the swap has to happen BEFORE `import app` runs - that's why
this file patches pymongo first, then imports app afterwards.
=====================================================
"""

import os
import sys

# --- fake settings so app.py doesn't crash on missing real config ---
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-real-use")
os.environ.setdefault("MONGO_URI", "mongodb://localhost/aquaflow_test")
os.environ.setdefault("MAIL_USERNAME", "test@example.com")
os.environ.setdefault("MAIL_PASSWORD", "test-password")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "test-razorpay-secret")

import mongomock
import pymongo
pymongo.MongoClient = mongomock.MongoClient  # <-- the actual swap

import pytest

import app as app_module  # noqa: E402  (import after the patch, on purpose)


@pytest.fixture
def client():
    """
    A fake browser that can hit your Flask routes without running a
    real server. Use it like: client.get('/login'),
    client.post('/booking', data={...}).
    """
    app_module.app.config["TESTING"] = True
    app_module.app.config["WTF_CSRF_ENABLED"] = False
    with app_module.app.test_client() as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def clean_database():
    """
    Runs before AND after every single test automatically (that's
    what autouse=True means) so one test's fake data never leaks
    into the next test.
    """
    _COLLECTIONS = [
        "users_collection", "bookings_collection", "notifications_collection",
        "drivers_collection", "admins_collection", "coupons_collection",
        "coupon_redemptions_collection", "wallet_transactions_collection",
        "wallet_topups_collection", "earnings_collection",
        "payment_transactions_collection",
    ]

    def _wipe():
        for name in _COLLECTIONS:
            getattr(app_module, name).delete_many({})

    _wipe()
    yield
    _wipe()


@pytest.fixture
def app():
    """Gives a test direct access to app.py's module - collections,
    helper functions, etc - when it needs to set up data directly
    instead of going through a route."""
    return app_module
