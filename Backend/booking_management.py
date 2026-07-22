from flask import (
    render_template,
    redirect,
    url_for,
    session,
    flash,  # ✅ Added flash for better user feedback
    jsonify,
    request
)

from datetime import datetime, timedelta
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

        # =====================================================
        # DATE WINDOW (bookings can be placed up to 15 days out —
        # see booking() in app.py)
        # =====================================================

        today = datetime.now().date()
        today_str = today.strftime("%Y-%m-%d")
        max_future = today + timedelta(days=15)
        max_future_str = max_future.strftime("%Y-%m-%d")

        # A specific future date the admin picked from the dropdown —
        # if set, that day's orders are shown instead of the main list.
        future_date = request.args.get("future_date", "").strip()

        # =====================================================
        # ALL FUTURE ORDERS (next 15 days) — used to build the
        # dropdown counts and the unseen-order red-dot alert
        # =====================================================

        future_bookings_all = list(bookings_collection.find({
            "delivery_date": {"$gt": today_str, "$lte": max_future_str}
        }))

        future_dates_summary = []
        for i in range(1, 16):
            d = today + timedelta(days=i)
            d_str = d.strftime("%Y-%m-%d")
            day_bookings = [b for b in future_bookings_all if b.get("delivery_date") == d_str]
            if day_bookings:
                future_dates_summary.append({
                    "date": d_str,
                    "label": d.strftime("%d %b"),
                    "count": len(day_bookings),
                    "has_unseen": any(not b.get("admin_seen_future", False) for b in day_bookings)
                })

        unseen_dates = sorted({
            b.get("delivery_date") for b in future_bookings_all
            if not b.get("admin_seen_future", False)
        })
        has_unseen_future = len(unseen_dates) > 0
        next_unseen_date = unseen_dates[0] if unseen_dates else None

        # =====================================================
        # MAIN LIST — either today-or-earlier orders (default), or
        # a specific future date if the admin selected one
        # =====================================================

        if future_date and future_date in {row["date"] for row in future_dates_summary}:
            bookings = list(
                bookings_collection.find({"delivery_date": future_date})
                .sort("created_at", -1)
            )
            # Admin has now seen this date's orders — clear the red dot
            # for it.
            bookings_collection.update_many(
                {"delivery_date": future_date, "admin_seen_future": {"$ne": True}},
                {"$set": {"admin_seen_future": True}}
            )
            viewing_future_date = future_date
        else:
            bookings = list(
                bookings_collection.find({
                    "$or": [
                        {"delivery_date": {"$lte": today_str}},
                        {"delivery_date": {"$exists": False}},
                        {"delivery_date": ""}
                    ]
                }).sort("created_at", -1)
            )
            viewing_future_date = None

        # ✅ GET ALL APPROVED DRIVERS
        drivers = list(drivers_collection.find({"approval_status": "Approved"}))

        # TOTAL COUNTS (company-wide, not limited to the current view)
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
            drivers=drivers,  # ✅ ADDED drivers to template
            today_str=today_str,
            future_dates_summary=future_dates_summary,
            has_unseen_future=has_unseen_future,
            next_unseen_date=next_unseen_date,
            viewing_future_date=viewing_future_date
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