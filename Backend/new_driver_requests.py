from flask import (
    render_template,
    session,
    redirect,
    url_for,
    jsonify
)

from bson.objectid import ObjectId

from datetime import datetime


def init_new_driver_requests(
    app,
    drivers_collection
):

    # =====================================================
    # NEW DRIVER REQUESTS PAGE
    # =====================================================

    @app.route("/new-driver-requests")
    def new_driver_requests():

        # =========================================
        # ADMIN LOGIN CHECK
        # =========================================

        if "admin_email" not in session:

            return redirect(
                url_for("admin_login")
            )

        # =========================================
        # GET ALL PENDING DRIVERS
        # =========================================

        pending_drivers = list(

            drivers_collection.find({

                "approval_status": "Pending"
            }).sort("created_at", -1)

        )

        # =========================================
        # MARK NOTIFICATION AS SEEN
        # =========================================

        drivers_collection.update_many(

            {
                "approval_status": "Pending",

                "request_seen": {

                    "$ne": True
                }
            },

            {
                "$set": {

                    "request_seen": True
                }
            }
        )

        # =========================================
        # TOTAL NEW REQUESTS
        # =========================================

        total_requests = len(pending_drivers)

        # =========================================
        # RENDER PAGE
        # =========================================

        return render_template(

            "new_driver_requests.html",

            pending_drivers=pending_drivers,

            total_requests=total_requests,

            current_time=datetime.now()
        )

    # =====================================================
    # APPROVE DRIVER
    # =====================================================

    @app.route("/approve-driver-request/<driver_id>")
    def approve_driver_request(driver_id):

        if "admin_email" not in session:

            return redirect(
                url_for("admin_login")
            )

        drivers_collection.update_one(

            {
                "_id": ObjectId(driver_id)
            },

            {
                "$set": {

                    "approval_status": "Approved",

                    "approved_at": datetime.now(),

                    "online_status": "Offline"
                }
            }
        )

        return redirect(
            url_for("new_driver_requests")
        )

    # =====================================================
    # REJECT DRIVER
    # =====================================================

    @app.route("/reject-driver-request/<driver_id>")
    def reject_driver_request(driver_id):

        if "admin_email" not in session:

            return redirect(
                url_for("admin_login")
            )

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
            url_for("new_driver_requests")
        )

    # =====================================================
    # GET LIVE RED DOT COUNT
    # =====================================================

    @app.route("/new-driver-request-count")
    def new_driver_request_count():

        pending_count = drivers_collection.count_documents({

            "approval_status": "Pending",

            "request_seen": {

                "$ne": True
            }
        })

        return jsonify({

            "pending_count": pending_count
        })