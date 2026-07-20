from flask import (
    render_template,
    redirect,
    url_for,
    session,
    request,
    flash,
    jsonify
)

from bson.objectid import ObjectId
from datetime import datetime


def init_live_tracking(app, bookings_collection):

    # ==================================================
    # LIVE STATUS PAGE
    # ==================================================

    @app.route("/live-status/<booking_id>")
    def live_status(booking_id):

        if "user_email" not in session:
            return redirect(url_for("login"))

        booking = bookings_collection.find_one({
            "_id": ObjectId(booking_id)
        })

        if not booking:
            return "Booking Not Found"

        return render_template(
    "live_tracking.html",
    booking=booking
)

    # ==================================================
    # ASSIGN DRIVER + UPDATE TRACKING
    # ==================================================

    @app.route("/update-tracking/<booking_id>", methods=["POST"])
    def update_tracking(booking_id):

        if "admin" not in session:
            return redirect(url_for("admin_login"))

        driver_name = request.form.get("driver_name")
        driver_phone = request.form.get("driver_phone")
        truck_number = request.form.get("truck_number")
        live_location = request.form.get("live_location")
        ETA = request.form.get("ETA")
        status = request.form.get("status")

        progress_percent = 0

        if status == "Assigned":
            progress_percent = 25

        elif status == "On The Way":
            progress_percent = 65

        elif status == "Delivered":
            progress_percent = 100

        bookings_collection.update_one(

            {
                "_id": ObjectId(booking_id)
            },

            {
                "$set": {

                    "driver_name": driver_name,
                    "driver_phone": driver_phone,
                    "truck_number": truck_number,
                    "live_location": live_location,
                    "ETA": ETA,
                    "status": status,
                    "progress_percent": progress_percent,
                    "last_updated": datetime.now()

                }
            }
        )

        flash("Tracking Updated Successfully")

        return redirect(url_for("admin_dashboard"))

    # ==================================================
    # RESET TRACKING
    # ==================================================

    @app.route("/reset-tracking/<booking_id>")
    def reset_tracking(booking_id):

        if "admin" not in session:
            return redirect(url_for("admin_login"))

        bookings_collection.update_one(

            {
                "_id": ObjectId(booking_id)
            },

            {
                "$unset": {

                    "driver_name": "",
                    "driver_phone": "",
                    "truck_number": "",
                    "live_location": "",
                    "ETA": "",
                    "status": "",
                    "progress_percent": ""

                }
            }
        )

        flash("Tracking Reset Successfully")

        return redirect(url_for("admin_dashboard"))

    # ==================================================
    # COMPLETE DELIVERY
    # ==================================================

    @app.route("/complete-delivery/<booking_id>")
    def complete_delivery(booking_id):

        if "admin" not in session:
            return redirect(url_for("admin_login"))

        bookings_collection.update_one(

            {
                "_id": ObjectId(booking_id)
            },

            {
                "$set": {

                    "status": "Delivered",
                    "ETA": "Delivered",
                    "progress_percent": 100,
                    "last_updated": datetime.now()

                }
            }
        )

        flash("Delivery Completed")

        return redirect(url_for("admin_dashboard"))

    # ==================================================
    # LIVE TRACKING API
    # ==================================================

    @app.route("/tracking-api/<booking_id>")
    def tracking_api(booking_id):

        booking = bookings_collection.find_one({
            "_id": ObjectId(booking_id)
        })

        if not booking:

            return jsonify({
                "success": False
            })

        return jsonify({

            "success": True,

            "booking_id": str(booking["_id"]),

            "customer": booking.get(
                "fullname",
                "Customer"
            ),

            "driver_name": booking.get(
                "driver_name",
                ""
            ),

            "driver_phone": booking.get(
                "driver_phone",
                ""
            ),

            "truck_number": booking.get(
                "truck_number",
                ""
            ),

            "live_location": booking.get(
                "live_location",
                ""
            ),

            "driver_lat": booking.get(
                "driver_lat"
            ),

            "driver_lng": booking.get(
                "driver_lng"
            ),

            "ETA": booking.get(
                "ETA",
                ""
            ),

            "status": booking.get(
                "status",
                ""
            ),

            "progress_percent": booking.get(
                "progress_percent",
                0
            )
        })