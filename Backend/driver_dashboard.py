from flask import (
    render_template,
    session,
    redirect,
    url_for,
    request,
    jsonify,
    flash,
    abort
)
from bson.objectid import ObjectId
from datetime import datetime, timedelta
from functools import wraps
from flask_mail import Message
import random
import logging
from typing import Dict, Any, List, Optional

from payment_admin import (
    get_bank_details_masked as get_bank_details,
    get_driver_wallet_summary,
    get_driver_settlement_summary,
    save_payment_screenshot
)
 
# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
 
# Global variables to hold collections (defined at module level)
_drivers_collection = None
_bookings_collection = None
_notifications_collection = None
_earnings_collection = None
_documents_collection = None
_settings_collection = None
_transactions_collection = None
_mail = None
 
# =====================================================
# DECORATORS FOR AUTHENTICATION
# =====================================================
 
def driver_login_required(f):
    """Decorator to ensure driver is logged in"""
    # Routes the frontend calls via fetch()/AJAX and expects a JSON body
    # back even when auth fails - everything else (plain <form> posts like
    # profile update, document upload, emergency message) still expects a
    # normal redirect + flash message.
    _JSON_ROUTE_PREFIXES = (
        "/api/",
        "/start-delivery/",
        "/verify-delivery-otp/",
        "/generate-delivery-otp/",
        "/update-delivery-status/",
    )

    @wraps(f)
    def decorated_function(*args, **kwargs):
        wants_json = request.path.startswith(_JSON_ROUTE_PREFIXES)

        if "driver_email" not in session:
            if wants_json:
                return jsonify({"success": False, "message": "Your session has expired. Please log in again.", "sessionExpired": True}), 401
            flash("Please login to access this page", "warning")
            return redirect(url_for("driver_login"))

        # Check if driver still exists in database
        driver = _drivers_collection.find_one({"email": session["driver_email"]})
        if not driver:
            session.clear()
            if wants_json:
                return jsonify({"success": False, "message": "Driver account not found. Please log in again.", "sessionExpired": True}), 401
            flash("Driver account not found", "danger")
            return redirect(url_for("driver_login"))

        return f(*args, **kwargs)
    return decorated_function
 
def init_driver_dashboard(
    app,
    drivers_collection,
    bookings_collection,
    notifications_collection,
    earnings_collection=None,
    documents_collection=None,
    mail=None,
    settings_collection=None,
    transactions_collection=None
):
    # Set global variables
    global _drivers_collection, _bookings_collection, _notifications_collection
    global _earnings_collection, _documents_collection, _mail, _settings_collection
    global _transactions_collection
    
    _drivers_collection = drivers_collection
    _bookings_collection = bookings_collection
    _notifications_collection = notifications_collection
    _earnings_collection = earnings_collection
    _documents_collection = documents_collection
    _mail = mail
    _settings_collection = settings_collection
    _transactions_collection = transactions_collection
    
    # =====================================================
    # HELPER FUNCTIONS
    # =====================================================
    
    def calculate_driver_earnings(driver_email: str) -> Dict[str, Any]:
        """Calculate comprehensive earnings for driver"""
        # Get all completed deliveries
        completed_deliveries = list(_bookings_collection.find({
            "driver_email": driver_email,
            "status": "Delivered"
        }))
        
        total_earnings = sum([
            order.get("delivery_fee", 500) + order.get("tip_amount", 0)
            for order in completed_deliveries
        ])
        
        # Calculate pending payments (delivered but not paid)
        pending_payments = sum([
            order.get("delivery_fee", 500)
            for order in completed_deliveries
            if not order.get("payment_processed", False)
        ])
        
        # Calculate approved payments
        approved_payments = sum([
            order.get("delivery_fee", 500)
            for order in completed_deliveries
            if order.get("payment_processed", False)
        ])
        
        # Calculate weekly earnings
        today = datetime.now()
        start_of_week = today - timedelta(days=today.weekday())
        weekly_earnings = {
            "Mon": 0, "Tue": 0, "Wed": 0, "Thu": 0, "Fri": 0, "Sat": 0, "Sun": 0
        }
        
        for order in completed_deliveries:
            delivered_at = order.get("delivered_at")
            if delivered_at and isinstance(delivered_at, datetime):
                day_name = delivered_at.strftime("%a")
                if day_name in weekly_earnings:
                    weekly_earnings[day_name] += order.get("delivery_fee", 500)
        
        return {
            "total_earnings": total_earnings,
            "pending_payments": pending_payments,
            "approved_payments": approved_payments,
            "weekly_earnings": weekly_earnings,
            "completed_count": len(completed_deliveries)
        }
    
    def update_driver_stats(driver_email: str):
        """Update driver statistics"""
        driver = _drivers_collection.find_one({"email": driver_email})
        if not driver:
            return None
        
        earnings_data = calculate_driver_earnings(driver_email)
        
        # Get all orders for this driver
        all_orders = list(_bookings_collection.find({"driver_email": driver_email}))
        
        completed_orders = [o for o in all_orders if o.get("status") == "Delivered"]
        active_orders = [o for o in all_orders if o.get("status") not in ["Delivered", "Cancelled"]]
        
        # Calculate average rating from completed orders
        ratings = [o.get("customer_rating", 0) for o in completed_orders if o.get("customer_rating")]
        avg_rating = sum(ratings) / len(ratings) if ratings else 5.0
        
        _drivers_collection.update_one(
            {"email": driver_email},
            {
                "$set": {
                    "total_earnings": earnings_data["total_earnings"],
                    "pending_payments": earnings_data["pending_payments"],
                    "approved_payments": earnings_data["approved_payments"],
                    "total_deliveries": len(all_orders),
                    "completed_deliveries": len(completed_orders),
                    "active_deliveries": len(active_orders),
                    "average_rating": round(avg_rating, 1),
                    "last_stats_update": datetime.now()
                }
            }
        )
        
        return earnings_data
    
    # =====================================================
    # DRIVER DASHBOARD
    # =====================================================
    
    @app.route("/driver-dashboard")
    @driver_login_required
    def driver_dashboard():
        try:
            driver = _drivers_collection.find_one({"email": session["driver_email"]})
            if not driver:
                flash("Driver profile not found", "danger")
                return redirect(url_for("driver_login"))
            
            # Update driver stats before displaying
            earnings_data = update_driver_stats(session["driver_email"])
            
            # Get assigned orders with proper sorting
            assigned_orders = list(
                _bookings_collection.find({
                    "driver_email": session["driver_email"]
                }).sort("created_at", -1)
            )
            
            # Separate orders by status
            active_orders_list = [o for o in assigned_orders 
                                if o.get("status") not in ["Delivered", "Cancelled"]]
            
            # Add delivery_status to each active order for template compatibility
            for order in active_orders_list:
                status = order.get("status", "Pending")
                if status == "On The Way":
                    order["delivery_status"] = "On The Way"
                elif status == "Arrived":
                    order["delivery_status"] = "Arrived"
                elif status == "Delivered":
                    order["delivery_status"] = "Delivered"
                else:
                    order["delivery_status"] = "Pending"
            
            completed_orders_list = [o for o in assigned_orders 
                                   if o.get("status") == "Delivered"]
            cancelled_orders_list = [o for o in assigned_orders 
                                   if o.get("status") == "Cancelled"]

            # ---------------------------------------------------------
            # Orders that are still waiting on this driver's Accept /
            # Reject decision. These must NOT show a map or a Start
            # button - only Accept / Reject.
            # ---------------------------------------------------------
            pending_decision_orders = [
                o for o in active_orders_list if o.get("status") == "Assigned"
            ]

            # ---------------------------------------------------------
            # The order (if any) that should drive the live map. Only
            # orders the driver has actually accepted (Accepted / On
            # The Way / Arrived) qualify - never a still-pending
            # "Assigned" order.
            # ---------------------------------------------------------
            in_progress_statuses = ["Accepted", "On The Way", "Arrived"]
            current_delivery_order = next(
                (o for o in active_orders_list if o.get("status") in in_progress_statuses),
                None
            )

            # ---------------------------------------------------------
            # Completed deliveries that were paid in cash and still
            # need the driver to submit / remit the collected amount
            # to the company.
            # ---------------------------------------------------------
            cash_pending_orders = [
                o for o in completed_orders_list
                if not o.get("payment_submitted")
                and not o.get("payment_processed")
                and o.get("payment_status") != "Paid (Online)"
            ]

            # ---------------------------------------------------------
            # Real-money reconciliation with the company: how much cash
            # this driver has collected, how much is already submitted
            # and awaiting admin verification, how much admin has
            # confirmed received, and how much is still owed right now.
            # ---------------------------------------------------------
            wallet = get_driver_wallet_summary(_bookings_collection, session["driver_email"])
            bank_details = get_bank_details(_settings_collection)

            # Submissions the driver has already made, most recent first,
            # so they can see what's pending / approved / rejected.
            payment_submissions = [
                o for o in completed_orders_list if o.get("payment_submitted")
            ]
            payment_submissions.sort(
                key=lambda o: o.get("payment_submitted_at") or datetime.min,
                reverse=True
            )
            payment_submissions = payment_submissions[:10]
            
            # Calculate today's deliveries
            today = datetime.now().date()
            today_deliveries = len([
                o for o in assigned_orders
                if o.get("delivery_date") and 
                isinstance(o["delivery_date"], datetime) and
                o["delivery_date"].date() == today
            ])
            
            # Get emergency messages (last 5)
            emergency_messages = list(
                _notifications_collection.find({
                    "driver_email": session["driver_email"],
                    "type": "emergency"
                }).sort("created_at", -1).limit(5)
            )
            
            # Get unread notification count
            unread_count = _notifications_collection.count_documents({
                "driver_email": session["driver_email"],
                "read": False
            })
            
            # =============================================
            # DEBUG PRINTS - ADDED BACK WITH CORRECT VARIABLES
            # =============================================
            print("=" * 50)
            print("LOGGED IN DRIVER:", session.get("driver_email"))
            print("TOTAL ORDERS:", len(assigned_orders))
            print("ACTIVE ORDERS:", len(active_orders_list))
            print("COMPLETED ORDERS:", len(completed_orders_list))
            print("-" * 30)
            
            for order in assigned_orders:
                print("ORDER ID:", order.get("_id"))
                print("DRIVER EMAIL:", order.get("driver_email"))
                print("STATUS:", order.get("status"))
                print("-" * 30)
            
            print("=" * 50)
            # =============================================
            
            return render_template(
                "driver_dashboard.html",
                driver=driver,
                assigned_orders=assigned_orders,
                active_orders=active_orders_list,
                completed_orders=completed_orders_list,
                cancelled_orders=cancelled_orders_list,
                pending_decision_orders=pending_decision_orders,
                current_delivery_order=current_delivery_order,
                cash_pending_orders=cash_pending_orders,
                wallet=wallet,
                bank_details=bank_details,
                payment_submissions=payment_submissions,
                total_orders=len(assigned_orders),
                active_orders_count=len(active_orders_list),
                completed_orders_count=len(completed_orders_list),
                cancelled_orders_count=len(cancelled_orders_list),
                total_earnings=earnings_data["total_earnings"] if earnings_data else 0,
                pending_payments=earnings_data["pending_payments"] if earnings_data else 0,
                approved_payments=earnings_data["approved_payments"] if earnings_data else 0,
                weekly_earnings=earnings_data["weekly_earnings"] if earnings_data else {"Mon": 0, "Tue": 0, "Wed": 0, "Thu": 0, "Fri": 0, "Sat": 0, "Sun": 0},
                emergency_messages=emergency_messages,
                today_deliveries=today_deliveries,
                unread_notifications=unread_count,
                current_time=datetime.now()
            )
        except Exception as e:
            logger.error(f"Error in driver_dashboard: {str(e)}")
            flash("An error occurred loading the dashboard", "danger")
            return redirect(url_for("driver_login"))
    
    # =====================================================
    # DRIVER PROFILE
    # =====================================================
    
    @app.route("/driver-profile")
    @driver_login_required
    def driver_profile():
        driver = _drivers_collection.find_one({"email": session["driver_email"]})
        return render_template("driver_profile.html", driver=driver)
    
    @app.route("/update-driver-profile", methods=["POST"])
    @driver_login_required
    def update_driver_profile():
        try:
            update_data = {
                "phone": request.form.get("phone"),
                "truck_number": request.form.get("truck_number"),
                "license_number": request.form.get("license_number"),
                "experience_years": int(request.form.get("experience_years", 0)),
                "updated_at": datetime.now()
            }
            
            # Remove None values
            update_data = {k: v for k, v in update_data.items() if v is not None}
            
            # Handle photo upload if present
            if "photo" in request.files:
                photo = request.files["photo"]
                if photo and photo.filename:
                    update_data["photo"] = photo.filename
            
            _drivers_collection.update_one(
                {"email": session["driver_email"]},
                {"$set": update_data}
            )
            
            flash("Profile updated successfully", "success")
        except Exception as e:
            logger.error(f"Profile update error: {str(e)}")
            flash("Error updating profile", "danger")
        
        return redirect(url_for("driver_profile"))
    
    # =====================================================
    # DRIVER ACTIVE ORDERS
    # =====================================================
    
    @app.route("/driver-active-orders")
    @driver_login_required
    def driver_active_orders():
        active_orders = list(
            _bookings_collection.find({
                "driver_email": session["driver_email"],
                "status": {"$nin": ["Delivered", "Cancelled"]}
            }).sort("created_at", -1)
        )
        
        return render_template(
            "driver_active_orders.html",
            active_orders=active_orders
        )
    
    # =====================================================
    # DRIVER COMPLETED ORDERS
    # =====================================================
    
    @app.route("/driver-completed-orders")
    @driver_login_required
    def driver_completed_orders():
        completed_orders = list(
            _bookings_collection.find({
                "driver_email": session["driver_email"],
                "status": "Delivered"
            }).sort("delivered_at", -1)
        )
        
        return render_template(
            "driver_completed_orders.html",
            completed_orders=completed_orders
        )
    
    # =====================================================
    # DELIVERY STATUS UPDATE (AJAX Enhanced)
    # =====================================================
    
    @app.route("/api/update-delivery-status/<booking_id>", methods=["POST"])
    @driver_login_required
    def api_update_delivery_status(booking_id):
        try:
            data = request.get_json()
            new_status = data.get("status")
            live_location = data.get("live_location")
            eta = data.get("eta")
            
            update_fields = {
                "status": new_status,
                "last_updated": datetime.now()
            }
            
            if live_location:
                update_fields["live_location"] = live_location
            if eta:
                update_fields["eta"] = eta
            
            if new_status == "On The Way" and not data.get("started_at"):
                update_fields["started_at"] = datetime.now()
            elif new_status == "Arrived":
                update_fields["arrived_at"] = datetime.now()
            elif new_status == "Delivered":
                update_fields["delivered_at"] = datetime.now()
            
            result = _bookings_collection.update_one(
                {"_id": ObjectId(booking_id), "driver_email": session["driver_email"]},
                {"$set": update_fields}
            )
            
            if result.modified_count > 0:
                # Update driver stats
                update_driver_stats(session["driver_email"])
                return jsonify({"success": True, "message": "Status updated"})
            else:
                return jsonify({"success": False, "message": "No changes made"}), 400
                
        except Exception as e:
            logger.error(f"Status update error: {str(e)}")
            return jsonify({"success": False, "message": str(e)}), 500
    
    # =====================================================
    # START DELIVERY
    # =====================================================
    
    @app.route("/start-delivery/<booking_id>", methods=["POST"])
    @driver_login_required
    def start_delivery(booking_id):
        try:
            try:
                oid = ObjectId(booking_id)
            except Exception:
                logger.error(f"Start delivery: invalid booking_id received: {booking_id!r}")
                return jsonify({"success": False, "message": "Invalid booking ID"}), 400

            booking = _bookings_collection.find_one({
                "_id": oid,
                "driver_email": session["driver_email"]
            })

            if not booking:
                logger.error(f"Start delivery: no booking found for id={booking_id} driver={session.get('driver_email')}")
                return jsonify({
                    "success": False,
                    "message": "Booking not found or not assigned to you"
                }), 404

            current_status = str(booking.get("status", "")).strip()
            if current_status.lower() != "accepted":
                return jsonify({
                    "success": False,
                    "message": f"Please accept this order before starting the delivery (current status: {current_status or 'Unknown'})"
                }), 400

            result = _bookings_collection.update_one(
                {"_id": oid, "driver_email": session["driver_email"]},
                {
                    "$set": {
                        "status": "On The Way",
                        "started_at": datetime.now()
                    }
                }
            )

            if result.modified_count > 0 or result.matched_count > 0:
                if _notifications_collection is not None:
                    try:
                        _notifications_collection.insert_one({
                            "user_email": booking["user_email"],
                            "title": "Delivery Started",
                            "message": "Driver started delivery for your booking.",
                            "type": "on_way",
                            "status": "warning",
                            "icon": "🚛",
                            "read": False,
                            "created_at": datetime.now()
                        })
                    except Exception as notify_err:
                        logger.error(f"Start delivery notification error: {str(notify_err)}")

                return jsonify({
                    "success": True,
                    "message": "Delivery started successfully"
                })

            return jsonify({
                "success": False,
                "message": "Unable to start delivery"
            })

        except Exception as e:
            logger.error(f"Start delivery error: {str(e)}")
            return jsonify({
                "success": False,
                "message": "Error starting delivery"
            })
    
    # =====================================================
    # ARRIVED LOCATION
    # =====================================================
    
    @app.route("/arrived-location/<booking_id>")
    @driver_login_required
    def arrived_location(booking_id):
        try:
            result = _bookings_collection.update_one(
                {"_id": ObjectId(booking_id), "driver_email": session["driver_email"]},
                {
                    "$set": {
                        "status": "Arrived",
                        "arrived_at": datetime.now()
                    }
                }
            )
            
            if result.modified_count > 0:
                flash("You have arrived at the location", "success")
            else:
                flash("Unable to update status", "warning")
                
        except Exception as e:
            logger.error(f"Arrived location error: {str(e)}")
            flash("Error updating status", "danger")
        
        return redirect(url_for("driver_dashboard"))
    
    # =====================================================
    # PUSH DRIVER'S REAL GPS LOCATION (from browser geolocation)
    # =====================================================

    @app.route("/api/update-driver-location/<booking_id>", methods=["POST"])
    @driver_login_required
    def update_driver_location(booking_id):
        try:
            data = request.get_json(silent=True) or {}
            lat = data.get("lat")
            lng = data.get("lng")

            if lat is None or lng is None:
                return jsonify({"success": False, "message": "lat/lng required"}), 400

            try:
                lat = float(lat)
                lng = float(lng)
            except (TypeError, ValueError):
                return jsonify({"success": False, "message": "lat/lng must be numbers"}), 400

            booking = _bookings_collection.find_one({
                "_id": ObjectId(booking_id),
                "driver_email": session["driver_email"]
            })

            if not booking:
                return jsonify({"success": False, "message": "Booking not found or not assigned to you"}), 404

            _bookings_collection.update_one(
                {"_id": ObjectId(booking_id)},
                {
                    "$set": {
                        "driver_lat": lat,
                        "driver_lng": lng,
                        "live_location": "En route - live GPS updating",
                        "location_updated_at": datetime.now()
                    }
                }
            )

            return jsonify({"success": True})

        except Exception as e:
            logger.error(f"Update driver location error: {str(e)}")
            return jsonify({"success": False, "message": "Error updating location"}), 500

    # =====================================================
    # ACCEPT BOOKING (driver dashboard - single source of truth)
    # =====================================================

    @app.route("/api/accept-booking/<booking_id>", methods=["POST"])
    @driver_login_required
    def api_accept_booking(booking_id):
        try:
            try:
                oid = ObjectId(booking_id)
            except Exception:
                logger.error(f"Accept booking: invalid booking_id received: {booking_id!r}")
                return jsonify({"success": False, "message": "Invalid booking ID"}), 400

            booking = _bookings_collection.find_one({
                "_id": oid,
                "driver_email": session["driver_email"]
            })

            if not booking:
                logger.error(f"Accept booking: no booking found for id={booking_id} driver={session.get('driver_email')}")
                return jsonify({"success": False, "message": "Booking not found or not assigned to you"}), 404

            current_status = str(booking.get("status", "")).strip()
            if current_status.lower() != "assigned":
                logger.error(f"Accept booking: booking {booking_id} has status {current_status!r}, expected Assigned")
                return jsonify({"success": False, "message": f"This order can no longer be accepted (current status: {current_status or 'Unknown'})"}), 400

            result = _bookings_collection.update_one(
                {"_id": oid, "driver_email": session["driver_email"]},
                {
                    "$set": {
                        "status": "Accepted",
                        "driver_response": "Accepted",
                        "driver_accept_time": datetime.now()
                    }
                }
            )

            if result.modified_count > 0 or result.matched_count > 0:
                if _notifications_collection is not None and booking.get("user_email"):
                    driver = _drivers_collection.find_one({"email": session["driver_email"]})
                    try:
                        _notifications_collection.insert_one({
                            "user_email": booking["user_email"],
                            "title": "Driver Accepted Your Order",
                            "message": f"{driver.get('name', 'Your driver') if driver else 'Your driver'} has accepted your delivery and will start shortly.",
                            "type": "driver_accepted",
                            "status": "success",
                            "icon": "✅",
                            "read": False,
                            "created_at": datetime.now()
                        })
                    except Exception as notify_err:
                        logger.error(f"Accept booking notification error: {str(notify_err)}")

                return jsonify({"success": True, "message": "Order accepted"})

            return jsonify({"success": False, "message": "Unable to accept order"}), 400

        except Exception as e:
            logger.exception(f"Accept booking error for booking_id={booking_id}: {str(e)}")
            return jsonify({"success": False, "message": f"Error accepting order: {str(e)}"}), 500

    # =====================================================
    # REJECT BOOKING (driver dashboard - single source of truth)
    # =====================================================

    @app.route("/api/reject-booking/<booking_id>", methods=["POST"])
    @driver_login_required
    def api_reject_booking(booking_id):
        try:
            try:
                oid = ObjectId(booking_id)
            except Exception:
                logger.error(f"Reject booking: invalid booking_id received: {booking_id!r}")
                return jsonify({"success": False, "message": "Invalid booking ID"}), 400

            booking = _bookings_collection.find_one({
                "_id": oid,
                "driver_email": session["driver_email"]
            })

            if not booking:
                logger.error(f"Reject booking: no booking found for id={booking_id} driver={session.get('driver_email')}")
                return jsonify({"success": False, "message": "Booking not found or not assigned to you"}), 404

            current_status = str(booking.get("status", "")).strip()
            if current_status.lower() != "assigned":
                logger.error(f"Reject booking: booking {booking_id} has status {current_status!r}, expected Assigned")
                return jsonify({"success": False, "message": f"This order can no longer be rejected (current status: {current_status or 'Unknown'})"}), 400

            # Unassign this driver so admin can reassign to someone else,
            # and record who rejected it so they aren't re-assigned by mistake.
            result = _bookings_collection.update_one(
                {"_id": oid},
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

            if result.modified_count > 0 or result.matched_count > 0:
                if _notifications_collection is not None:
                    try:
                        if booking.get("user_email"):
                            _notifications_collection.insert_one({
                                "user_email": booking["user_email"],
                                "title": "Reassigning Your Delivery",
                                "message": "Your assigned driver was unavailable. We're assigning a new driver shortly.",
                                "type": "driver_rejected",
                                "status": "warning",
                                "icon": "⚠️",
                                "read": False,
                                "created_at": datetime.now()
                            })
                        # Notify admin so the booking can be reassigned
                        _notifications_collection.insert_one({
                            "admin": True,
                            "title": "Driver Rejected an Order",
                            "message": f"Booking #{str(booking['_id'])[:8]} needs a new driver assignment.",
                            "type": "reassignment_needed",
                            "status": "warning",
                            "icon": "🔁",
                            "read": False,
                            "booking_id": str(booking["_id"]),
                            "created_at": datetime.now()
                        })
                    except Exception as notify_err:
                        logger.error(f"Reject booking notification error: {str(notify_err)}")

                return jsonify({"success": True, "message": "Order rejected and sent back for reassignment"})

            return jsonify({"success": False, "message": "Unable to reject order"}), 400

        except Exception as e:
            logger.exception(f"Reject booking error for booking_id={booking_id}: {str(e)}")
            return jsonify({"success": False, "message": f"Error rejecting order: {str(e)}"}), 500

    # =====================================================
    # SUBMIT COLLECTED CASH PAYMENT TO COMPANY
    # =====================================================

    @app.route("/api/submit-collected-payment/<booking_id>", methods=["POST"])
    @driver_login_required
    def submit_collected_payment(booking_id):
        """
        Driver reports a REAL money transfer they made to the company's
        bank/UPI account for cash they collected from a customer.
        Accepts multipart/form-data so a transaction reference AND an
        optional payment-proof screenshot can be attached - this is what
        the admin will actually check against the bank statement before
        approving it in verify_driver_payment().
        """
        try:
            # Support both classic JSON (amount only - kept for backward
            # compatibility) and the richer multipart form the dashboard
            # now submits (amount + method + transaction ref + screenshot).
            if request.content_type and "multipart/form-data" in request.content_type:
                amount = request.form.get("amount")
                method = (request.form.get("method") or "UPI").strip()
                transaction_ref = (request.form.get("transaction_ref") or "").strip()
                note = (request.form.get("note") or "").strip()
                screenshot_file = request.files.get("screenshot")
            else:
                data = request.get_json(silent=True) or {}
                amount = data.get("amount")
                method = (data.get("method") or "UPI").strip()
                transaction_ref = (data.get("transaction_ref") or "").strip()
                note = (data.get("note") or "").strip()
                screenshot_file = None

            booking = _bookings_collection.find_one({
                "_id": ObjectId(booking_id),
                "driver_email": session["driver_email"]
            })

            if not booking:
                return jsonify({"success": False, "message": "Booking not found or not assigned to you"}), 404

            if booking.get("status") != "Delivered":
                return jsonify({"success": False, "message": "Only delivered orders can have payment submitted"}), 400

            if booking.get("payment_submitted"):
                return jsonify({"success": False, "message": "Payment already submitted for this order"}), 400

            if not transaction_ref:
                return jsonify({"success": False, "message": "Please enter the UPI / bank transaction reference number"}), 400

            try:
                amount = float(amount)
                if amount < 0:
                    raise ValueError
            except (TypeError, ValueError):
                amount = booking.get("delivery_fee", 500)

            screenshot_path = save_payment_screenshot(app, screenshot_file) if screenshot_file else None

            update_fields = {
                "payment_submitted": True,
                "cash_submitted_amount": amount,
                "payment_submission_method": method,
                "payment_transaction_ref": transaction_ref,
                "payment_submission_note": note,
                "payment_submission_status": "Pending Verification",
                "payment_submitted_at": datetime.now()
            }
            if screenshot_path:
                update_fields["payment_screenshot"] = screenshot_path

            _bookings_collection.update_one(
                {"_id": ObjectId(booking_id)},
                {"$set": update_fields}
            )

            driver = _drivers_collection.find_one({"email": session["driver_email"]})
            if _notifications_collection is not None:
                try:
                    _notifications_collection.insert_one({
                        "admin": True,
                        "title": "Driver Submitted Collected Payment",
                        "message": (
                            f"{driver.get('name', session['driver_email']) if driver else session['driver_email']} "
                            f"sent ₹{amount:,.0f} via {method} (ref: {transaction_ref}) for booking "
                            f"#{str(booking['_id'])[:8]}. Please verify against your bank statement."
                        ),
                        "type": "payment_submission",
                        "status": "info",
                        "icon": "💰",
                        "read": False,
                        "booking_id": str(booking["_id"]),
                        "driver_email": session["driver_email"],
                        "amount": amount,
                        "created_at": datetime.now()
                    })
                except Exception as notify_err:
                    logger.error(f"Payment submission notification error: {str(notify_err)}")

            return jsonify({
                "success": True,
                "message": "Payment submission recorded. Admin will verify it against the bank statement and confirm receipt."
            })

        except Exception as e:
            logger.error(f"Submit collected payment error: {str(e)}")
            return jsonify({"success": False, "message": "Error submitting payment"}), 500

    # =====================================================
    # DELIVERY OTP GENERATION
    # =====================================================
    
    @app.route("/generate-delivery-otp/<booking_id>")
    @driver_login_required
    def generate_delivery_otp(booking_id):
        """
        This endpoint does NOT create an OTP for the driver to read off
        their own screen - that would let the driver verify himself
        without the customer, which defeats the whole point of the OTP.
 
        The real OTP is generated ONCE, at booking-approval time (see
        approve_booking in app.py), and is emailed only to the customer.
        This endpoint just confirms that OTP exists for this booking. If
        it is somehow missing (e.g. an older booking approved before this
        flow existed), it generates one now and emails it to the
        customer - never to the driver.
        """
        try:
            # Verify the booking belongs to this driver
            booking = _bookings_collection.find_one({
                "_id": ObjectId(booking_id),
                "driver_email": session["driver_email"]
            })
            
            if not booking:
                return jsonify({"success": False, "message": "Booking not found"}), 404
            
            existing_otp = booking.get("delivery_otp")
            
            if existing_otp:
                # OTP already sent to the customer at approval time.
                # Do NOT regenerate it and do NOT send it back to the driver.
                return jsonify({
                    "success": True,
                    "message": "OTP was already sent to the customer's email. Please ask the customer for it."
                })
            
            # Fallback: no OTP exists yet for this booking (legacy booking).
            # Generate one, store it, and email it to the CUSTOMER only.
            otp = f"{random.randint(100000, 999999)}"
            
            _bookings_collection.update_one(
                {"_id": ObjectId(booking_id)},
                {
                    "$set": {
                        "delivery_otp": otp,
                        "otp_generated_at": datetime.now(),
                        "otp_attempts": 0
                    }
                }
            )
            
            customer_email = booking.get("user_email")
            if _mail is not None and customer_email:
                try:
                    msg = Message(
                        subject="AquaFlow Delivery OTP",
                        sender=app.config.get("MAIL_USERNAME"),
                        recipients=[customer_email]
                    )
                    msg.body = f"""Hello,
 
Your Delivery OTP is:
 
{otp}
 
Please share this OTP with the driver only after your delivery has arrived.
 
Thank You,
AquaFlow Team
"""
                    _mail.send(msg)
                except Exception as mail_err:
                    logger.error(f"Failed to email delivery OTP to customer: {mail_err}")
                    return jsonify({"success": False, "message": "Could not send OTP to customer email"}), 500
            
            return jsonify({
                "success": True,
                "message": "OTP generated and sent to the customer's email. Please ask the customer for it."
            })
            
        except Exception as e:
            logger.error(f"OTP generation error: {str(e)}")
            return jsonify({"success": False, "message": str(e)}), 500
    
    # =====================================================
    # VERIFY DELIVERY OTP
    # =====================================================
    
    @app.route("/verify-delivery-otp/<booking_id>", methods=["POST"])
    @driver_login_required
    def verify_delivery_otp(booking_id):
        try:
            entered_otp = request.form.get("otp")
            
            booking = _bookings_collection.find_one({
                "_id": ObjectId(booking_id),
                "driver_email": session["driver_email"]
            })
            
            if not booking:
                return jsonify({"success": False, "message": "Booking not found"}), 404
            
            # Check OTP attempts (prevent brute force)
            attempts = booking.get("otp_attempts", 0)
            if attempts >= 5:
                return jsonify({"success": False, "message": "Too many failed attempts. Please contact support."}), 400
            
            real_otp = booking.get("delivery_otp")
            
            if not real_otp:
                return jsonify({"success": False, "message": "OTP not generated yet. Please generate OTP first."}), 400
            
            if entered_otp != real_otp:
                # Increment failed attempts
                _bookings_collection.update_one(
                    {"_id": ObjectId(booking_id)},
                    {"$inc": {"otp_attempts": 1}}
                )
                remaining_attempts = 4 - attempts
                return jsonify({"success": False, "message": f"Invalid OTP. {remaining_attempts} attempts remaining."}), 400
            
            # OTP verified - complete delivery
            delivery_fee = booking.get("delivery_fee", 500)
            
            # Update booking
            _bookings_collection.update_one(
                {"_id": ObjectId(booking_id)},
                {
                    "$set": {
                        "status": "Delivered",
                        "delivered_at": datetime.now(),
                        "otp_verified": True,
                        "otp_verified_at": datetime.now()
                    }
                }
            )
            
            # Update driver earnings
            _drivers_collection.update_one(
                {"email": session["driver_email"]},
                {
                    "$inc": {
                        "completed_deliveries": 1,
                        "total_earnings": delivery_fee
                    },
                    "$set": {
                        "last_delivery_at": datetime.now()
                    }
                }
            )
            
            # Create earnings record
            if _earnings_collection is not None:
                _earnings_collection.insert_one({
                    "driver_email": session["driver_email"],
                    "booking_id": booking_id,
                    "amount": delivery_fee,
                    "type": "delivery",
                    "status": "pending",
                    "created_at": datetime.now()
                })
            
            # Create notification for driver
            if _notifications_collection is not None:
                _notifications_collection.insert_one({
                    "driver_email": session["driver_email"],
                    "type": "delivery_completed",
                    "title": "Delivery Completed",
                    "message": f"Delivery completed successfully. ₹{delivery_fee} added to your earnings.",
                    "read": False,
                    "created_at": datetime.now()
                })
            
            return jsonify({"success": True, "message": "Delivery completed successfully!"})
            
        except Exception as e:
            logger.error(f"OTP verification error: {str(e)}")
            return jsonify({"success": False, "message": "Error verifying OTP"}), 500
    
    # =====================================================
    # DRIVER EARNINGS PAGE
    # =====================================================
    
    @app.route("/driver-earnings")
    @driver_login_required
    def driver_earnings():
        driver = _drivers_collection.find_one({"email": session["driver_email"]})
        earnings_data = calculate_driver_earnings(session["driver_email"])

        settlement = get_driver_settlement_summary(
            _transactions_collection, _bookings_collection, session["driver_email"]
        )

        # Get earnings history
        earnings_history = []
        if _earnings_collection is not None:
            earnings_history = list(
                _earnings_collection.find({"driver_email": session["driver_email"]})
                .sort("created_at", -1)
                .limit(50)
            )
        
        return render_template(
            "driver_earnings.html",
            driver=driver,
            earnings=earnings_data,
            settlement=settlement,
            earnings_history=earnings_history
        )
    
    # =====================================================
    # EMERGENCY MESSAGE
    # =====================================================
    
    @app.route("/send-emergency-message", methods=["POST"])
    @driver_login_required
    def send_emergency_message():
        try:
            message = request.form.get("message")
            
            if not message:
                flash("Please enter a message", "warning")
                return redirect(url_for("driver_dashboard"))
            
            driver = _drivers_collection.find_one({"email": session["driver_email"]})
            
            _notifications_collection.insert_one({
                "type": "emergency",
                "driver_email": session["driver_email"],
                "driver_name": driver.get("name"),
                "driver_phone": driver.get("phone"),
                "message": message,
                "status": "unread",
                "priority": "high",
                "created_at": datetime.now()
            })
            
            flash("Emergency alert sent to admin", "warning")
            
        except Exception as e:
            logger.error(f"Emergency message error: {str(e)}")
            flash("Error sending emergency message", "danger")
        
        return redirect(url_for("driver_dashboard"))
    
    # =====================================================
    # DRIVER ONLINE STATUS (AJAX)
    # =====================================================
    
    @app.route("/api/driver-status", methods=["POST"])
    @driver_login_required
    def api_driver_status():
        try:
            data = request.get_json()
            status = data.get("status", "Offline")
            
            _drivers_collection.update_one(
                {"email": session["driver_email"]},
                {
                    "$set": {
                        "online_status": status,
                        "last_status_change": datetime.now()
                    }
                }
            )
            
            return jsonify({
                "success": True,
                "status": status,
                "message": f"Status updated to {status}"
            })
            
        except Exception as e:
            logger.error(f"Status update error: {str(e)}")
            return jsonify({"success": False, "message": str(e)}), 500
    
    @app.route("/driver-online")
    @driver_login_required
    def driver_online():
        _drivers_collection.update_one(
            {"email": session["driver_email"]},
            {"$set": {"online_status": "Online", "last_activity": datetime.now()}}
        )
        flash("You are now online and ready to accept deliveries", "success")
        return redirect(url_for("driver_dashboard"))
    
    @app.route("/driver-offline")
    @driver_login_required
    def driver_offline():
        _drivers_collection.update_one(
            {"email": session["driver_email"]},
            {"$set": {"online_status": "Offline", "last_activity": datetime.now()}}
        )
        flash("You are now offline", "info")
        return redirect(url_for("driver_dashboard"))
    
    # =====================================================
    # DRIVER NOTIFICATIONS
    # =====================================================
    
    @app.route("/driver-notifications")
    @driver_login_required
    def driver_notifications():
        # Mark all as read when viewing
        _notifications_collection.update_many(
            {"driver_email": session["driver_email"], "read": False},
            {"$set": {"read": True}}
        )
        
        notifications = list(
            _notifications_collection.find({
                "driver_email": session["driver_email"]
            }).sort("created_at", -1).limit(100)
        )
        
        return render_template(
            "driver_notifications.html",
            notifications=notifications
        )
    
    @app.route("/api/notifications/unread-count")
    @driver_login_required
    def unread_notifications_count():
        count = _notifications_collection.count_documents({
            "driver_email": session["driver_email"],
            "read": False
        })
        return jsonify({"count": count})
    
    # =====================================================
    # DRIVER ORDER DETAILS
    # =====================================================
    
    @app.route("/driver-order-details/<booking_id>")
    @driver_login_required
    def driver_order_details(booking_id):
        booking = _bookings_collection.find_one({
            "_id": ObjectId(booking_id),
            "driver_email": session["driver_email"]
        })
        
        if not booking:
            flash("Order not found", "danger")
            return redirect(url_for("driver_dashboard"))
        
        return render_template(
            "driver_order_details.html",
            booking=booking
        )
    
    # =====================================================
    # DRIVER ANALYTICS
    # =====================================================
    
    @app.route("/driver-analytics")
    @driver_login_required
    def driver_analytics():
        driver = _drivers_collection.find_one({"email": session["driver_email"]})
        
        # Get analytics data
        all_orders = list(_bookings_collection.find({"driver_email": session["driver_email"]}))
        
        # Monthly stats for last 6 months
        monthly_stats = []
        today = datetime.now()
        
        for i in range(6):
            # Calculate month start
            if today.month - i <= 0:
                year = today.year - 1
                month = 12 + (today.month - i)
            else:
                year = today.year
                month = today.month - i
            
            month_start = datetime(year, month, 1)
            
            if i == 0:
                month_end = today
            else:
                if month == 12:
                    month_end = datetime(year + 1, 1, 1)
                else:
                    month_end = datetime(year, month + 1, 1)
            
            month_orders = [
                o for o in all_orders
                if o.get("delivered_at") and
                isinstance(o["delivered_at"], datetime) and
                month_start <= o["delivered_at"] < month_end
            ]
            
            monthly_stats.append({
                "month": month_start.strftime("%B %Y"),
                "deliveries": len(month_orders),
                "earnings": sum(o.get("delivery_fee", 500) for o in month_orders)
            })
        
        # Performance metrics
        completed_orders = [o for o in all_orders if o.get("status") == "Delivered"]
        on_time_deliveries = len([
            o for o in completed_orders
            if o.get("delivered_at") and o.get("eta") and
            isinstance(o["delivered_at"], datetime) and
            isinstance(o["eta"], datetime) and
            o["delivered_at"] <= o["eta"]
        ])
        
        performance = {
            "total_deliveries": len(all_orders),
            "completed_deliveries": len(completed_orders),
            "completion_rate": (len(completed_orders) / len(all_orders) * 100) if all_orders else 0,
            "on_time_rate": (on_time_deliveries / len(completed_orders) * 100) if completed_orders else 0,
            "average_response_time": "5 mins",
            "customer_satisfaction": driver.get("average_rating", 4.8)
        }
        
        return render_template(
            "driver_analytics.html",
            driver=driver,
            monthly_stats=monthly_stats,
            performance=performance
        )
    
    # =====================================================
    # DRIVER DOCUMENTS
    # =====================================================
    
    @app.route("/driver-documents")
    @driver_login_required
    def driver_documents():
        documents = []
        if _documents_collection is not None:
            documents = list(
                _documents_collection.find({"driver_email": session["driver_email"]})
                .sort("uploaded_at", -1)
            )
        
        return render_template(
            "driver_documents.html",
            documents=documents
        )
    
    @app.route("/upload-document", methods=["POST"])
    @driver_login_required
    def upload_document():
        try:
            doc_type = request.form.get("document_type")
            doc_file = request.files.get("document")
            
            if not doc_file or not doc_type:
                flash("Please select a document to upload", "warning")
                return redirect(url_for("driver_documents"))
            
            # Implement file upload logic here
            # For now, just record the upload
            if _documents_collection is not None:
                _documents_collection.insert_one({
                    "driver_email": session["driver_email"],
                    "document_type": doc_type,
                    "filename": doc_file.filename,
                    "status": "pending_review",
                    "uploaded_at": datetime.now()
                })
            
            flash("Document uploaded successfully", "success")
            
        except Exception as e:
            logger.error(f"Document upload error: {str(e)}")
            flash("Error uploading document", "danger")
        
        return redirect(url_for("driver_documents"))
    
    # =====================================================
    # DRIVER LOGOUT
    # =====================================================
    
    @app.route("/driver-logout")
    def driver_logout_route():
        if "driver_email" in session:
            # Set offline status
            _drivers_collection.update_one(
                {"email": session["driver_email"]},
                {"$set": {"online_status": "Offline", "last_logout": datetime.now()}}
            )
            session.clear()
            flash("You have been logged out", "info")
        
        return redirect(url_for("driver_login"))
    
    # =====================================================
    # API ENDPOINTS FOR AJAX UPDATES
    # =====================================================
    
    @app.route("/api/driver/dashboard-data")
    @driver_login_required
    def api_dashboard_data():
        """Get real-time dashboard data for AJAX updates"""
        try:
            driver = _drivers_collection.find_one({"email": session["driver_email"]})
            earnings_data = calculate_driver_earnings(session["driver_email"])
            
            active_orders = list(
                _bookings_collection.find({
                    "driver_email": session["driver_email"],
                    "status": {"$nin": ["Delivered", "Cancelled"]}
                })
            )
            
            # Get total orders count
            total_orders = _bookings_collection.count_documents({"driver_email": session["driver_email"]})
            
            return jsonify({
                "total_earnings": earnings_data["total_earnings"],
                "pending_payments": earnings_data["pending_payments"],
                "active_orders": len(active_orders),
                "total_orders": total_orders,
                "online_status": driver.get("online_status", "Offline") if driver else "Offline",
                "last_activity": driver.get("last_activity", datetime.now()).isoformat() if driver and driver.get("last_activity") else None
            })
        except Exception as e:
            logger.error(f"API dashboard data error: {str(e)}")
            return jsonify({"error": str(e)}), 500
    
    logger.info("Driver dashboard routes initialized successfully")
    return app