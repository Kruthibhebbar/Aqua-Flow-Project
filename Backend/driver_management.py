from flask import (
    render_template,
    redirect,
    url_for,
    session,
    jsonify
)

from bson.objectid import ObjectId

from datetime import datetime

from payment_admin import (
    get_driver_wallet_summary,
    get_driver_settlement_summary
)


def init_driver_management(
    app,
    drivers_collection,
    bookings_collection,
    settings_collection=None,
    transactions_collection=None,
    documents_collection=None,
    support_collection=None
):

    # =====================================================
    # DRIVER MANAGEMENT PAGE
    # =====================================================

    @app.route("/driver-management")
    def driver_management():

        # =========================================
        # ADMIN LOGIN CHECK
        # =========================================

        if "admin_email" not in session:

            return redirect(
                url_for("admin_login")
            )

        # =========================================
        # GET APPROVED DRIVERS
        # =========================================

        approved_drivers = list(

            drivers_collection.find({

                "approval_status": "Approved"
            })

        )

        # =========================================
        # CALCULATE LIVE DRIVER DATA
        # =========================================

        for driver in approved_drivers:

            driver_email = driver.get("email")

            # ===============================
            # TOTAL DELIVERIES
            # ===============================

            total_deliveries = bookings_collection.count_documents({

                "driver_email": driver_email,

                "delivery_status": "Delivered"
            })

            # ===============================
            # TOTAL ACCEPTED ORDERS
            # ===============================

            total_orders_accepted = bookings_collection.count_documents({

                "driver_email": driver_email
            })

            # ===============================
            # ACTIVE ORDERS
            # ===============================

            active_orders = bookings_collection.count_documents({

                "driver_email": driver_email,

                "delivery_status": {

                    "$ne": "Delivered"
                }
            })

            # ===============================
            # SAVE TEMP DATA
            # ===============================

            driver["total_deliveries"] = total_deliveries

            driver["total_orders_accepted"] = total_orders_accepted

            driver["active_orders"] = active_orders

            # ===============================
            # REAL-MONEY WALLET: how much cash
            # this driver has collected from
            # customers and still owes the
            # company right now. (Finance-side
            # detail - full breakdown now lives
            # on the Finance Management page.)
            # ===============================

            driver["wallet"] = get_driver_wallet_summary(
                bookings_collection,
                driver_email
            )

            # ===============================
            # PENDING PAYOUT: the actual amount
            # still payable TO this driver right
            # now (earned commission minus what's
            # already been settled/in-flight,
            # minus any cash they still haven't
            # remitted). Computed live from
            # MongoDB every time - never
            # hardcoded. This is the driver-
            # centric number that belongs on
            # THIS page (the opposite direction
            # of the finance-side "pending
            # deposit" above).
            # ===============================

            driver["pending_payout"] = get_driver_settlement_summary(
                transactions_collection,
                bookings_collection,
                driver_email
            )["withdrawable"]

            # ===============================
            # UPLOADED DOCUMENTS (for the
            # admin document viewer on this
            # driver's card)
            # ===============================

            driver_documents = {}

            if documents_collection is not None:
                for doc in documents_collection.find({"driver_email": driver_email}):
                    driver_documents[doc.get("document_type")] = {
                        "id": str(doc["_id"]),
                        "status": doc.get("status", "Pending"),
                        "expiry_date": doc.get("expiry_date"),
                        "rejection_reason": doc.get("rejection_reason"),
                        "file_path": doc.get("file_path"),
                        "uploaded_at": (
                            doc["uploaded_at"].strftime("%d %b %Y")
                            if doc.get("uploaded_at") else None
                        )
                    }

            driver["documents"] = driver_documents

        # =========================================
        # GET NEW DRIVER REQUESTS
        # (Finance-side data - company bank details,
        # cash verification, settlement requests and
        # driver bank approvals - now lives entirely
        # on the Finance Management page.)
        # =========================================

        new_driver_requests = list(

            drivers_collection.find({

                "approval_status": "Pending"
            })

        )

        # =========================================
        # TOTAL COUNTS
        # =========================================

        total_drivers = drivers_collection.count_documents({

            "approval_status": "Approved"
        })

        online_drivers = drivers_collection.count_documents({

            "approval_status": "Approved",

            "online_status": "Online"
        })

        offline_drivers = drivers_collection.count_documents({

            "approval_status": "Approved",

            "online_status": {

                "$ne": "Online"
            }
        })

        pending_requests = drivers_collection.count_documents({

            "approval_status": "Pending"
        })

        # =========================================
        # TOTAL PENDING PAYOUTS (driver-centric
        # money-owed total, shown as a quick stat
        # on this page - the mirror-image number,
        # money owed the other way, lives on the
        # Finance Management page)
        # =========================================

        total_pending_payouts = sum(
            d["pending_payout"] for d in approved_drivers
        )

        # =========================================
        # OPEN DRIVER SUPPORT REQUESTS (red badges)
        # =========================================

        support_open = {}
        if support_collection is not None:
            try:
                for row in support_collection.aggregate([
                    {"$match": {"status": "Open"}},
                    {"$group": {"_id": "$driver_email", "n": {"$sum": 1}}},
                ]):
                    if row["_id"]:
                        support_open[row["_id"]] = row["n"]
            except Exception:
                support_open = {}

        # =========================================
        # RENDER PAGE
        # =========================================

        return render_template(

            "driver_management.html",

            approved_drivers=approved_drivers,

            new_driver_requests=new_driver_requests,

            total_drivers=total_drivers,

            online_drivers=online_drivers,

            offline_drivers=offline_drivers,

            pending_requests=pending_requests,

            total_pending_payouts=total_pending_payouts,

            support_open=support_open,

            support_open_total=sum(support_open.values()),

            current_time=datetime.now()
        )

    # =====================================================
    # APPROVE DRIVER
    # =====================================================

    @app.route("/approve-driver/<driver_id>")
    def approve_driver(driver_id):

        # =========================================
        # ADMIN CHECK
        # =========================================

        if "admin_email" not in session:

            return redirect(
                url_for("admin_login")
            )

        # =========================================
        # UPDATE DRIVER
        # =========================================

        drivers_collection.update_one(

            {
                "_id": ObjectId(driver_id)
            },

            {
                "$set": {

                    "approval_status": "Approved",

                    "approved_at": datetime.now()
                }
            }
        )

        return redirect(
            url_for("driver_management")
        )

    # =====================================================
    # REJECT DRIVER
    # =====================================================

    @app.route("/reject-driver/<driver_id>")
    def reject_driver(driver_id):

        # =========================================
        # ADMIN CHECK
        # =========================================

        if "admin_email" not in session:

            return redirect(
                url_for("admin_login")
            )

        # =========================================
        # UPDATE DRIVER
        # =========================================

        drivers_collection.update_one(

            {
                "_id": ObjectId(driver_id)
            },

            {
                "$set": {

                    "approval_status": "Rejected",

                    "rejected_at": datetime.now()
                }
            }
        )

        return redirect(
            url_for("driver_management")
        )

    # =====================================================
    # DELETE DRIVER
    # =====================================================

    @app.route("/delete-driver/<driver_id>")
    def delete_driver(driver_id):

        # =========================================
        # ADMIN CHECK
        # =========================================

        if "admin_email" not in session:

            return redirect(
                url_for("admin_login")
            )

        # =========================================
        # DELETE DRIVER
        # =========================================

        drivers_collection.delete_one({

            "_id": ObjectId(driver_id)
        })

        return redirect(
            url_for("driver_management")
        )

    # =====================================================
    # DRIVER DETAILS API
    # =====================================================

    @app.route("/driver-details/<driver_id>")
    def driver_details(driver_id):

        # =========================================
        # ADMIN ONLY
        # This used to be open to anyone who could guess a driver id and
        # returned the whole driver document (password hash, bank details).
        # =========================================

        if "admin_email" not in session and "admin" not in session:

            return jsonify({

                "success": False,
                "message": "Unauthorized"
            }), 401

        # =========================================
        # GET DRIVER
        # =========================================

        try:
            driver = drivers_collection.find_one({

                "_id": ObjectId(driver_id)
            })
        except Exception:
            driver = None

        if not driver:

            return jsonify({

                "success": False
            })

        # Never send credentials or banking details to the browser from
        # here - the finance page only needs the profile fields below.
        for secret_field in ("password", "bank_details"):
            driver.pop(secret_field, None)

        # =========================================
        # GET DRIVER BOOKINGS
        # =========================================

        bookings = list(

            bookings_collection.find({

                "driver_email": driver.get("email")
            })

        )

        # =========================================
        # CONVERT OBJECT IDS
        # =========================================

        driver["_id"] = str(driver["_id"])

        for booking in bookings:

            booking["_id"] = str(booking["_id"])

        # =========================================
        # RETURN JSON
        # =========================================

        return jsonify({

            "success": True,

            "driver": driver,

            "bookings": bookings
        })