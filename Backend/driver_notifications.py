from datetime import datetime
from flask import (
    render_template,
    session,
    redirect,
    url_for,
    flash,
    request
)

from bson.objectid import ObjectId


def init_driver_notifications(
    app,
    bookings_collection,
    drivers_collection,
    notifications_collection
):

    # =====================================
    # DRIVER NOTIFICATION PAGE
    # =====================================

    @app.route("/driver-notifications")
    def driver_notifications_page():

        # DRIVER LOGIN CHECK
        if "driver_email" not in session:
            flash("Please login to continue", "warning")
            return redirect(url_for("driver_login"))

        # GET DRIVER - Check if exists
        driver = drivers_collection.find_one({
            "email": session["driver_email"]
        })
        
        if not driver:
            flash("Driver account not found. Please login again.", "danger")
            session.pop("driver_email", None)
            return redirect(url_for("driver_login"))

        # GET PENDING REQUESTS - using correct field names from your booking schema
        pending_bookings = list(
            bookings_collection.find({
                "driver_email": session["driver_email"],
                "driver_response": "Pending"
            })
        )

        accepted_count = bookings_collection.count_documents({
            "driver_email": session["driver_email"],
            "driver_response": "Accepted"
        })

        rejected_count = bookings_collection.count_documents({
            "driver_email": session["driver_email"],
            "driver_response": "Rejected"
        })

        return render_template(
            "driver_notifications.html",
            driver=driver,
            pending_bookings=pending_bookings,
            accepted_count=accepted_count,
            rejected_count=rejected_count
        )

    # =====================================
    # ACCEPT BOOKING (FIXED - sends notification to USER with driver details)
    # =====================================

    @app.route("/accept-booking/<booking_id>", methods=["POST"])
    def driver_accept_booking(booking_id):

        if "driver_email" not in session:
            flash("Please login to continue", "warning")
            return redirect(url_for("driver_login"))

        try:
            # Verify booking exists and belongs to this driver
            booking = bookings_collection.find_one({
                "_id": ObjectId(booking_id),
                "driver_email": session["driver_email"]
            })

            if not booking:
                flash("Booking not found or unauthorized", "danger")
                return redirect(url_for("driver_notifications_page"))

            # Get driver details
            driver = drivers_collection.find_one({
                "email": session["driver_email"]
            })

            # Update the booking - ACCEPTED.
            # NOTE: status goes to "Accepted", not "On The Way" - the
            # driver still needs to press "Start Trip" on the dashboard.
            # This matches /api/accept-booking in driver_dashboard.py so
            # both the notifications page and the main dashboard leave
            # bookings in a consistent state.
            result = bookings_collection.update_one(
                {
                    "_id": ObjectId(booking_id),
                    "driver_email": session["driver_email"]
                },
                {
                    "$set": {
                        "driver_response": "Accepted",
                        "status": "Accepted",
                        "driver_accept_time": datetime.now()
                    }
                }
            )

            if result.modified_count > 0:
                flash("Booking accepted successfully!", "success")
                
                # =====================================================
                # FIX: Send notification to USER with ALL driver details
                # =====================================================
                # Get updated booking after update
                updated_booking = bookings_collection.find_one({
                    "_id": ObjectId(booking_id)
                })
                
                # Build the detailed message for user
                driver_details_message = f"""
🚛 DRIVER ASSIGNED TO YOUR DELIVERY!

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
👨‍✈️ Driver Name: {updated_booking.get('driver_name', driver.get('name', 'Not Available'))}

📞 Phone: {updated_booking.get('driver_phone', driver.get('phone', 'Not Available'))}

🚚 Truck Number: {updated_booking.get('truck_number', driver.get('truck_number', 'Not Available'))}

📍 Current Location: {updated_booking.get('live_location', 'En Route')}

⏱️ Estimated Arrival (ETA): {updated_booking.get('ETA', '30-45 minutes')}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ Your delivery is on the way!
📱 Track live from your dashboard
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

                # Insert notification for USER
                notifications_collection.insert_one({
                    "user_email": updated_booking["user_email"],
                    "title": "✅ Driver Accepted & On The Way!",
                    "message": driver_details_message,
                    "type": "driver_accepted",
                    "status": "success",
                    "icon": "🚛",
                    "read": False,
                    "created_at": datetime.now(),
                    "time": "Just now"
                })
                
                print(f"✅ Notification sent to user: {updated_booking['user_email']} with driver details")
                
            else:
                flash("Failed to accept booking", "danger")

        except Exception as e:
            print(f"Error accepting booking: {e}")
            flash("An error occurred while accepting the booking", "danger")

        return redirect(url_for("driver_notifications_page"))

    # =====================================
    # REJECT BOOKING
    # =====================================

    @app.route("/reject-booking/<booking_id>", methods=["POST"])
    def reject_booking(booking_id):

        if "driver_email" not in session:
            flash("Please login to continue", "warning")
            return redirect(url_for("driver_login"))

        try:
            booking = bookings_collection.find_one({
                "_id": ObjectId(booking_id),
                "driver_email": session["driver_email"]
            })

            if not booking:
                flash("Booking not found or unauthorized", "danger")
                return redirect(url_for("driver_notifications_page"))

            # Unassign this driver entirely so admin can hand the order to
            # someone else - matches /api/reject-booking in
            # driver_dashboard.py so both entry points behave the same way.
            result = bookings_collection.update_one(
                {"_id": ObjectId(booking_id)},
                {
                    "$set": {
                        "status": "Approved",
                        "driver_response": "Rejected",
                        "driver_name": "Not Assigned",
                        "driver_phone": "Not Available",
                        "truck_number": "Not Assigned",
                        "rejected_at": datetime.now()
                    },
                    "$unset": {
                        "driver_email": ""
                    },
                    "$addToSet": {
                        "rejected_by_drivers": session["driver_email"]
                    }
                }
            )

            if result.modified_count > 0:
                flash("Booking rejected", "info")
                
                # Notify user that driver rejected (so admin can reassign)
                notifications_collection.insert_one({
                    "user_email": booking["user_email"],
                    "title": "⚠️ Driver Rejected Assignment",
                    "message": "The assigned driver rejected your booking. Admin will assign another driver shortly.",
                    "type": "driver_rejected",
                    "status": "warning",
                    "icon": "⚠️",
                    "read": False,
                    "created_at": datetime.now(),
                    "time": "Just now"
                })
            else:
                flash("Failed to reject booking", "danger")

        except Exception as e:
            print(f"Error rejecting booking: {e}")
            flash("An error occurred while rejecting the booking", "danger")

        return redirect(url_for("driver_notifications_page"))