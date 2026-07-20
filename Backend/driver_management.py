from flask import (
    render_template,
    redirect,
    url_for,
    session,
    request,
    jsonify
)

from bson.objectid import ObjectId

from datetime import datetime

from payment_admin import (
    get_bank_details,
    get_driver_wallet_summary
)


def init_driver_management(
    app,
    drivers_collection,
    bookings_collection,
    settings_collection=None,
    transactions_collection=None
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
            # company right now.
            # ===============================

            driver["wallet"] = get_driver_wallet_summary(
                bookings_collection,
                driver_email
            )

        # =========================================
        # COMPANY BANK / UPI DETAILS
        # (shown on this page for the admin to
        # edit, and on every driver's dashboard so
        # they know where to send collected cash)
        # =========================================

        bank_details = get_bank_details(settings_collection)

        # =========================================
        # PENDING PAYMENT VERIFICATIONS
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

        total_pending_deposit = sum(
            d["wallet"]["pending_deposit"] for d in approved_drivers
        )

        # =========================================
        # PENDING DRIVER SETTLEMENT (WITHDRAWAL)
        # REQUESTS
        # =========================================

        pending_settlements = []
        if transactions_collection is not None:
            pending_settlements = list(
                transactions_collection.find({
                    "type": "settlement",
                    "status": {"$in": ["Pending", "Processing"]}
                }).sort("requested_at", -1)
            )

        # =========================================
        # GET NEW DRIVER REQUESTS
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

            bank_details=bank_details,

            pending_verifications=pending_verifications,

            recent_verifications=recent_verifications,

            total_pending_deposit=total_pending_deposit,

            pending_settlements=pending_settlements,

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
        # GET DRIVER
        # =========================================

        driver = drivers_collection.find_one({

            "_id": ObjectId(driver_id)
        })

        if not driver:

            return jsonify({

                "success": False
            })

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