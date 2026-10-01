"""
finance_management.py
======================

The Finance Management admin page.

Driver Management (driver_management.py) owns everything about a
driver as a person/vehicle/employee. This module owns everything
about MONEY moving between AquaFlow and its drivers:

    - the company's own bank/UPI details (where drivers send cash
      they've collected)
    - driver bank-account approvals (must happen before a driver can
      request a payout)
    - driver settlement (payout) requests - money flowing
      company -> driver
    - COD cash-collection verification - money flowing
      driver -> company
    - the full payout payment history / ledger

All the actual business logic already lives in payment_admin.py
(bank details, settlement resolution, payment verification, etc.)
and payment_ledger.py (the unified transaction ledger) - this module
does not duplicate or change any of it. It only gathers everything
those modules already compute into one enriched, finance-focused
view.
"""

from flask import render_template, redirect, url_for, session
from datetime import datetime

from payment_admin import (
    get_bank_details,
    get_driver_settlement_summary,
    get_finance_overview,
    get_ledger_overview,
)


def init_finance_management(
    app,
    drivers_collection,
    bookings_collection,
    settings_collection=None,
    transactions_collection=None,
    payment_transactions_collection=None,
    notifications_collection=None
):

    @app.route("/finance-management")
    def finance_management():

        # =========================================
        # ADMIN LOGIN CHECK
        # =========================================

        if "admin_email" not in session and "admin" not in session:
            return redirect(url_for("admin_login"))

        # =========================================
        # COMPANY BANK / UPI DETAILS
        # Shown here for the admin to edit, and on
        # every driver's dashboard so they know
        # where to send collected cash.
        # =========================================

        bank_details = get_bank_details(settings_collection)

        # =========================================
        # PENDING PAYMENT VERIFICATIONS (COD)
        # every delivery where a driver has
        # submitted a transaction reference /
        # screenshot that admin hasn't checked yet
        # =========================================

        pending_verifications = list(
            bookings_collection.find({
                "payment_submitted": True,
                "payment_submission_status": "Pending Verification"
            }).sort("payment_submitted_at", -1)
        )

        recent_verifications = list(
            bookings_collection.find({
                "payment_submission_status": {"$in": ["Approved", "Rejected"]}
            }).sort("payment_verified_at", -1).limit(15)
        )

        # =========================================
        # PENDING DRIVER BANK-DETAILS APPROVALS
        # withdrawals stay locked for a driver until
        # their bank details are approved here
        # =========================================

        pending_bank_details = list(
            drivers_collection.find({
                "bank_details.approval_status": "Pending"
            })
        )

        # =========================================
        # DRIVER SETTLEMENT (PAYOUT) REQUESTS
        # Enriched with everything the admin needs
        # to actually pay a driver, without having
        # to jump to another page: driver profile,
        # bank/UPI info, verification status,
        # earnings/paid/pending totals, all computed
        # live from MongoDB.
        # =========================================

        pending_settlements = []
        if transactions_collection is not None:
            raw_requests = list(
                transactions_collection.find({
                    "type": "settlement",
                    "status": {"$in": ["Pending", "Processing"]}
                }).sort("requested_at", -1)
            )

            for req in raw_requests:
                driver = None
                if drivers_collection is not None:
                    driver = drivers_collection.find_one(
                        {"email": req.get("driver_email")}
                    )

                bank = (driver or {}).get("bank_details") or {}
                account_number = bank.get("account_number") or ""
                masked_account = (
                    "X" * (len(account_number) - 4) + account_number[-4:]
                    if len(account_number) > 4 else account_number
                )

                settlement_summary = get_driver_settlement_summary(
                    transactions_collection, bookings_collection,
                    req.get("driver_email")
                )

                req["driver_profile"] = driver
                req["driver_photo"] = (driver or {}).get("photo")
                req["driver_name"] = (driver or {}).get("name", req.get("driver_email"))
                req["driver_id"] = str((driver or {}).get("_id", ""))
                req["driver_phone"] = (driver or {}).get("phone")
                req["bank_account_holder"] = bank.get("account_holder_name")
                req["bank_name"] = bank.get("bank_name")
                req["bank_account_number_masked"] = masked_account
                req["bank_account_number_full"] = account_number
                req["bank_ifsc"] = bank.get("ifsc")
                req["bank_upi_id"] = bank.get("upi_id")
                req["bank_verification_status"] = bank.get("approval_status", "Not Submitted")
                req["total_earnings"] = settlement_summary["driver_earned_total"]
                req["total_paid"] = settlement_summary["settled_total"]
                req["current_pending_payout"] = settlement_summary["withdrawable"]

                pending_settlements.append(req)

        # =========================================
        # PAYOUT / SETTLEMENT PAYMENT HISTORY
        # Full audit trail of every completed
        # driver payout, pulled from the unified
        # payment ledger (payment_ledger.py) - the
        # single source of truth for transaction
        # references, payment method and who
        # confirmed it.
        # =========================================

        payment_history = []
        if payment_transactions_collection is not None:
            payment_history = list(
                payment_transactions_collection.find({
                    "type": "withdrawal",
                    "status": "Withdrawn"
                }).sort("updated_at", -1).limit(50)
            )

            # Attach the originating settlement request's _id (if still
            # findable) so the receipt link in the template can point
            # at /admin/settlement/<request_id>/receipt.
            if transactions_collection is not None:
                for txn in payment_history:
                    settlement_doc = transactions_collection.find_one(
                        {"payment_transaction_id": txn.get("transaction_id")}
                    )
                    txn["settlement_request_id"] = (
                        str(settlement_doc["_id"]) if settlement_doc else None
                    )

        # =========================================
        # FINANCE-WIDE OVERVIEW STATS
        # =========================================

        finance = get_finance_overview(
            bookings_collection, drivers_collection, transactions_collection
        )
        ledger_overview = get_ledger_overview(payment_transactions_collection)

        # =========================================
        # RENDER PAGE
        # =========================================

        return render_template(
            "finance_management.html",

            bank_details=bank_details,

            pending_verifications=pending_verifications,
            recent_verifications=recent_verifications,

            pending_bank_details=pending_bank_details,

            pending_settlements=pending_settlements,
            payment_history=payment_history,

            finance=finance,
            ledger_overview=ledger_overview,

            current_time=datetime.now()
        )

    return app
