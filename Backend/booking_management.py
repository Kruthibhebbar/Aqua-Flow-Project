from flask import (
    render_template,
    redirect,
    url_for,
    session,
    flash,  # ✅ Added flash for better user feedback
    jsonify
)

from datetime import datetime
from bson.objectid import ObjectId
from bson.errors import InvalidId  # ✅ Added for error handling


def init_booking_management(
    app,
    bookings_collection,
    drivers_collection  # ✅ ADDED drivers_collection parameter
):

    # =========================================
    # BOOKING MANAGEMENT PAGE
    # =========================================

    @app.route("/booking-management")
    def booking_management():

        # ADMIN SECURITY
        if "admin" not in session:
            return redirect(url_for("admin_login"))

        # GET ALL BOOKINGS
        bookings = list(bookings_collection.find().sort("created_at", -1))

        # ✅ GET ALL APPROVED DRIVERS
        drivers = list(drivers_collection.find({"approval_status": "Approved"}))

        # TOTAL COUNTS
        total_bookings = bookings_collection.count_documents({})
        pending_bookings = bookings_collection.count_documents({"status": "Pending"})
        delivered_bookings = bookings_collection.count_documents({"status": "Delivered"})

        # ✅ IMPROVED: Now includes both "Approved" AND "On The Way"
        active_bookings = bookings_collection.count_documents({
            "status": {"$in": ["Approved", "On The Way"]}
        })

        return render_template(
            "booking_management.html",
            bookings=bookings,
            total_bookings=total_bookings,
            pending_bookings=pending_bookings,
            delivered_bookings=delivered_bookings,
            active_bookings=active_bookings,
            current_time=datetime.now(),
            drivers=drivers  # ✅ ADDED drivers to template
        )

    # =========================================
    # ACCEPT BOOKING
    # =========================================

    @app.route("/accept-booking/<booking_id>")  # ✅ Fixed indentation - now at same level as booking_management
    def accept_booking(booking_id):

        if "admin" not in session:
            return redirect(url_for("admin_login"))

        try:
            # Validate and update the booking
            result = bookings_collection.update_one(
                {"_id": ObjectId(booking_id)},
                {"$set": {"status": "Accepted"}}
            )
            
            if result.modified_count > 0:
                flash("Booking accepted successfully!", "success")
            else:
                flash("Booking not found or already accepted.", "warning")
                
        except InvalidId:
            flash("Invalid booking ID provided.", "error")
        except Exception as e:
            flash(f"An error occurred: {str(e)}", "error")

        return redirect(url_for("booking_management"))

    # =========================================================
    # CONFIRM RECEIPT OF DRIVER-SUBMITTED CASH PAYMENT
    # =========================================================

    @app.route("/confirm-payment/<booking_id>", methods=["POST"])
    def confirm_payment(booking_id):

        if "admin" not in session:
            return jsonify({"success": False, "message": "Unauthorized"}), 401

        try:
            booking = bookings_collection.find_one({"_id": ObjectId(booking_id)})

            if not booking:
                return jsonify({"success": False, "message": "Booking not found"}), 404

            if not booking.get("payment_submitted"):
                return jsonify({"success": False, "message": "No payment submission to confirm"}), 400

            bookings_collection.update_one(
                {"_id": ObjectId(booking_id)},
                {
                    "$set": {
                        "payment_processed": True,
                        "payment_submission_status": "Approved",
                        "payment_confirmed_at": datetime.now(),
                        "payment_confirmed_by": session.get("admin")
                    }
                }
            )

            return jsonify({"success": True, "message": "Payment confirmed as received"})

        except InvalidId:
            return jsonify({"success": False, "message": "Invalid booking ID"}), 400
        except Exception as e:
            return jsonify({"success": False, "message": str(e)}), 500