from flask import (
    session,
    redirect,
    url_for,
    request,
    jsonify,
    flash
)
from bson.objectid import ObjectId
from datetime import datetime
from functools import wraps
import hmac
from pymongo import ReturnDocument
from flask_mail import Message
from mail_utils import send_mail_capped
import random
import re
import logging
from typing import Dict, Any

from payment_ledger import record_cod_submission
from payment_admin import (
    save_payment_screenshot,
    DRIVER_SHARE_RATE
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
_activity_collection = None
_settings_collection = None
_transactions_collection = None
_payment_transactions_collection = None
_mail = None
 
# =====================================================
# DELIVERY WORKFLOW RULES (shared by every driver endpoint)
# =====================================================
# Status flow:  Assigned -> Accepted -> On The Way -> Arrived -> Delivered
#
# Through the status endpoints a driver may only take the two forward steps
# below, each from exactly one status. "Delivered" is deliberately NOT here:
# it is reachable only via /verify-delivery-otp (the customer's OTP).
DRIVER_STATUS_TRANSITIONS = {
    "On The Way": ("Accepted",),
    "Arrived": ("On The Way",),
}
OTP_COMPLETABLE_STATUSES = ("On The Way", "Arrived")
OTP_MAX_ATTEMPTS = 5


def transition_delivery(booking_id, driver_email, new_status, extra_fields=None):
    """Atomically move a delivery owned by `driver_email` to `new_status`.

    Returns (booking_as_it_was_before, error_message, http_status). On
    success the error is None. The status check is part of the update
    filter itself, so a double click or a stale tab can never apply a
    transition twice or skip a step.
    """
    allowed_from = DRIVER_STATUS_TRANSITIONS.get(new_status)
    if allowed_from is None:
        if new_status == "Delivered":
            return None, "A delivery can only be completed with the OTP the customer received.", 400
        return None, f"'{new_status}' is not a status you can set.", 400

    try:
        oid = ObjectId(booking_id)
    except Exception:
        return None, "Invalid booking ID", 400

    fields = {"status": new_status, "last_updated": datetime.now()}
    fields.update(extra_fields or {})

    before = _bookings_collection.find_one_and_update(
        {"_id": oid, "driver_email": driver_email, "status": {"$in": list(allowed_from)}},
        {"$set": fields}
    )
    if before is not None:
        return before, None, 200

    current = _bookings_collection.find_one({"_id": oid, "driver_email": driver_email})
    if not current:
        return None, "Booking not found or not assigned to you", 404
    return None, f"This delivery is '{current.get('status')}' and can't be moved to '{new_status}'.", 409


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
        "/arrived-location/",
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
    activity_collection=None,
    mail=None,
    settings_collection=None,
    transactions_collection=None,
    payment_transactions_collection=None
):
    # Set global variables
    global _drivers_collection, _bookings_collection, _notifications_collection
    global _earnings_collection, _documents_collection, _activity_collection, _mail, _settings_collection
    global _transactions_collection, _payment_transactions_collection
    
    _drivers_collection = drivers_collection
    _bookings_collection = bookings_collection
    _notifications_collection = notifications_collection
    _earnings_collection = earnings_collection
    _documents_collection = documents_collection
    _activity_collection = activity_collection
    _mail = mail
    _settings_collection = settings_collection
    _transactions_collection = transactions_collection
    _payment_transactions_collection = payment_transactions_collection
    
    # =====================================================
    # HELPER FUNCTIONS
    # =====================================================
    
    def calculate_driver_earnings(driver_email: str) -> Dict[str, Any]:
        """Calculate comprehensive earnings for driver.

        IMPORTANT: this must report the driver's actual commission
        share (DRIVER_SHARE_RATE), not the full fare - the wallet page
        (get_driver_wallet_summary/get_driver_settlement_summary) and
        the admin dashboard's top-drivers leaderboard both use the
        commission share. If this function used the full fare instead,
        a driver would see two different "earnings" numbers for the
        same deliveries depending on which page they're looking at.
        """
        # Get all completed deliveries
        completed_deliveries = list(_bookings_collection.find({
            "driver_email": driver_email,
            "status": "Delivered"
        }))

        total_earnings = sum([
            round(order.get("delivery_fee", 500) * DRIVER_SHARE_RATE) + order.get("tip_amount", 0)
            for order in completed_deliveries
        ])

        # Calculate pending payments (delivered but not paid)
        pending_payments = sum([
            round(order.get("delivery_fee", 500) * DRIVER_SHARE_RATE)
            for order in completed_deliveries
            if not order.get("payment_processed", False)
        ])

        # Calculate approved payments
        approved_payments = sum([
            round(order.get("delivery_fee", 500) * DRIVER_SHARE_RATE)
            for order in completed_deliveries
            if order.get("payment_processed", False)
        ])

        # Calculate weekly earnings
        weekly_earnings = {
            "Mon": 0, "Tue": 0, "Wed": 0, "Thu": 0, "Fri": 0, "Sat": 0, "Sun": 0
        }

        for order in completed_deliveries:
            delivered_at = order.get("delivered_at")
            if delivered_at and isinstance(delivered_at, datetime):
                day_name = delivered_at.strftime("%a")
                if day_name in weekly_earnings:
                    weekly_earnings[day_name] += round(order.get("delivery_fee", 500) * DRIVER_SHARE_RATE)

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
        # Only real customer ratings count. With none, this stays None -
        # it must never be filled with a made-up default score.
        ratings = [o.get("customer_rating") for o in completed_orders if o.get("customer_rating")]
        avg_rating = round(sum(ratings) / len(ratings), 1) if ratings else None
        
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
                    "average_rating": avg_rating,
                    "last_stats_update": datetime.now()
                }
            }
        )
        
        return earnings_data
    # =====================================================
    # DELIVERY STATUS UPDATE (AJAX)
    # -----------------------------------------------------
    # Only the forward steps a driver controls are allowed here
    # (Accepted -> On The Way -> Arrived), enforced server-side by
    # transition_delivery(). "Delivered" is refused outright: a delivery
    # can only be completed through /verify-delivery-otp, which needs the
    # code the customer received by email.
    # =====================================================

    @app.route("/api/update-delivery-status/<booking_id>", methods=["POST"])
    @driver_login_required
    def api_update_delivery_status(booking_id):
        try:
            data = request.get_json(silent=True) or {}
            new_status = str(data.get("status") or "").strip()

            extra = {}
            live_location = data.get("live_location")
            eta = data.get("eta")
            if isinstance(live_location, str) and live_location.strip():
                extra["live_location"] = live_location.strip()[:120]
            if isinstance(eta, str) and eta.strip():
                extra["eta"] = eta.strip()[:60]
            if new_status == "On The Way":
                extra["started_at"] = datetime.now()
            elif new_status == "Arrived":
                extra["arrived_at"] = datetime.now()

            _before, error, status_code = transition_delivery(
                booking_id, session["driver_email"], new_status, extra
            )
            if error:
                return jsonify({"success": False, "message": error}), status_code

            update_driver_stats(session["driver_email"])
            return jsonify({"success": True, "message": "Status updated"})

        except Exception as e:
            logger.error(f"Status update error: {str(e)}")
            return jsonify({"success": False, "message": "Could not update the status"}), 500
    # =====================================================
    # START DELIVERY
    # =====================================================

    @app.route("/start-delivery/<booking_id>", methods=["POST"])
    @driver_login_required
    def start_delivery(booking_id):
        try:
            booking, error, status_code = transition_delivery(
                booking_id, session["driver_email"], "On The Way",
                {"started_at": datetime.now()}
            )

            if error:
                if status_code == 409:
                    current = (
                        _bookings_collection.find_one({"_id": ObjectId(booking_id)}) or {}
                    ).get("status") or "Unknown"
                    if current in ("On The Way", "Arrived"):
                        error = "This delivery has already been started."
                    else:
                        error = (
                            "Please accept this order before starting the delivery "
                            f"(current status: {current})"
                        )
                    status_code = 400
                return jsonify({"success": False, "message": error}), status_code

            if _notifications_collection is not None and booking.get("user_email"):
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

        except Exception as e:
            logger.error(f"Start delivery error: {str(e)}")
            return jsonify({
                "success": False,
                "message": "Error starting delivery"
            }), 500
    # =====================================================
    # ARRIVED LOCATION
    # -----------------------------------------------------
    # POST only (it changes state), and only valid from "On The Way".
    # The GPS engine also flips a delivery to Arrived automatically when
    # the driver is within ~100 m; this is the manual fallback.
    # =====================================================

    @app.route("/arrived-location/<booking_id>", methods=["POST"])
    @driver_login_required
    def arrived_location(booking_id):
        try:
            now = datetime.now()
            booking, error, status_code = transition_delivery(
                booking_id, session["driver_email"], "Arrived", {"arrived_at": now}
            )
            if error:
                return jsonify({"success": False, "message": error}), status_code

            if _notifications_collection is not None and booking.get("user_email"):
                try:
                    _notifications_collection.insert_one({
                        "user_email": booking["user_email"],
                        "title": "🚛 Driver Arrived",
                        "message": "Your AquaFlow driver has arrived at your location.",
                        "type": "arrived",
                        "status": "success",
                        "icon": "📍",
                        "read": False,
                        "created_at": now
                    })
                except Exception as notify_err:
                    logger.error(f"Arrived notification error: {str(notify_err)}")

            return jsonify({"success": True, "message": "Marked as arrived"})

        except Exception as e:
            logger.error(f"Arrived location error: {str(e)}")
            return jsonify({"success": False, "message": "Error updating status"}), 500
    
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
    # PUSH DRIVER'S REAL GPS LOCATION WHILE IDLE (no active
    # booking yet) - stored on the driver's own record so the
    # admin's assign-driver screen can compute a genuine
    # distance-based ETA the moment a driver is selected,
    # instead of only after they're already assigned.
    # =====================================================

    @app.route("/api/update-idle-location", methods=["POST"])
    @driver_login_required
    def update_idle_location():
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

            _drivers_collection.update_one(
                {"email": session["driver_email"]},
                {
                    "$set": {
                        "current_lat": lat,
                        "current_lng": lng,
                        "location_updated_at": datetime.now()
                    }
                }
            )

            return jsonify({"success": True})

        except Exception as e:
            logger.error(f"Update idle location error: {str(e)}")
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

            # Atomic claim: only ONE request can flip payment_submitted to True.
            # A double tap / retry / second tab matches nothing here and stops,
            # so it can't create a second notification or ledger row.
            claimed = _bookings_collection.find_one_and_update(
                {"_id": ObjectId(booking_id), "driver_email": session["driver_email"],
                 "payment_submitted": {"$ne": True}},
                {"$set": update_fields, "$inc": {"payment_submission_attempt": 1}},
                return_document=ReturnDocument.AFTER
            )
            if claimed is None:
                return jsonify({"success": False, "message": "Payment already submitted for this order"}), 400
            attempt = claimed.get("payment_submission_attempt", 1)

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

            # ONE ledger row for this submission; the admin's decision later
            # updates this same row (see payment_admin.verify_driver_payment).
            record_cod_submission(
                _payment_transactions_collection,
                booking_id,
                attempt,
                amount=amount,
                user_email=booking.get("user_email"),
                driver_email=session["driver_email"],
                payment_screenshot=screenshot_path,
                notes=f"{method} ref: {transaction_ref}" + (f" — {note}" if note else "")
            )

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
    
    @app.route("/generate-delivery-otp/<booking_id>", methods=["GET", "POST"])
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

            if booking.get("status") not in ("Accepted",) + OTP_COMPLETABLE_STATUSES:
                return jsonify({
                    "success": False,
                    "message": "An OTP is only needed for a delivery that is in progress."
                }), 400

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
                    send_mail_capped(app, _mail, msg, timeout_seconds=6)
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
    # -----------------------------------------------------
    # The ONLY way a delivery becomes "Delivered". Guarantees:
    #   * the booking must belong to this driver and be On The Way/Arrived
    #   * a delivery that is already Delivered can never be completed twice
    #     (no double earnings)
    #   * wrong codes are counted atomically - parallel requests cannot
    #     squeeze extra guesses past the 5-attempt limit
    #   * the final Delivered write is a compare-and-set on the status
    # =====================================================

    @app.route("/verify-delivery-otp/<booking_id>", methods=["POST"])
    @driver_login_required
    def verify_delivery_otp(booking_id):
        try:
            entered_otp = (request.form.get("otp") or "").strip()
            driver_email = session["driver_email"]

            try:
                oid = ObjectId(booking_id)
            except Exception:
                return jsonify({"success": False, "message": "Invalid booking ID"}), 400

            booking = _bookings_collection.find_one({
                "_id": oid,
                "driver_email": driver_email
            })

            if not booking:
                return jsonify({"success": False, "message": "Booking not found"}), 404

            current_status = booking.get("status")
            if current_status == "Delivered":
                return jsonify({
                    "success": False,
                    "message": "This delivery has already been completed."
                }), 409
            if current_status not in OTP_COMPLETABLE_STATUSES:
                return jsonify({
                    "success": False,
                    "message": f"Start the delivery before verifying the OTP (current status: {current_status or 'Unknown'})."
                }), 400

            # Check OTP attempts (prevent brute force)
            if (booking.get("otp_attempts") or 0) >= OTP_MAX_ATTEMPTS:
                return jsonify({"success": False, "message": "Too many failed attempts. Please contact support."}), 400

            real_otp = booking.get("delivery_otp")

            if not real_otp:
                return jsonify({"success": False, "message": "OTP not generated yet. Please generate OTP first."}), 400

            # Claim one attempt atomically BEFORE comparing, so concurrent
            # requests can't all slip under the limit.
            attempt_guard = {
                "_id": oid,
                "driver_email": driver_email,
                "status": {"$in": list(OTP_COMPLETABLE_STATUSES)},
                "$or": [
                    {"otp_attempts": {"$exists": False}},
                    {"otp_attempts": {"$lt": OTP_MAX_ATTEMPTS}}
                ]
            }
            after = _bookings_collection.find_one_and_update(
                attempt_guard,
                {"$inc": {"otp_attempts": 1}},
                return_document=ReturnDocument.AFTER
            )
            if after is None:
                return jsonify({"success": False, "message": "Too many failed attempts. Please contact support."}), 400

            if not hmac.compare_digest(entered_otp.encode("utf-8"), str(real_otp).encode("utf-8")):
                remaining = max(0, OTP_MAX_ATTEMPTS - (after.get("otp_attempts") or 0))
                return jsonify({"success": False, "message": f"Invalid OTP. {remaining} attempts remaining."}), 400

            # OTP verified - complete delivery (compare-and-set on status)
            delivery_fee = booking.get("delivery_fee", 500)
            now = datetime.now()

            completed = _bookings_collection.find_one_and_update(
                {
                    "_id": oid,
                    "driver_email": driver_email,
                    "status": {"$in": list(OTP_COMPLETABLE_STATUSES)}
                },
                {
                    "$set": {
                        "status": "Delivered",
                        "delivered_at": now,
                        "otp_verified": True,
                        "otp_verified_at": now,
                        "otp_attempts": 0
                    }
                }
            )
            if completed is None:
                return jsonify({
                    "success": False,
                    "message": "This delivery was just updated elsewhere. Refresh and check its status."
                }), 409

            # Update driver earnings
            _drivers_collection.update_one(
                {"email": driver_email},
                {
                    "$inc": {
                        "completed_deliveries": 1,
                        "total_earnings": delivery_fee
                    },
                    "$set": {
                        "last_delivery_at": now
                    }
                }
            )

            # Create earnings record
            if _earnings_collection is not None:
                _earnings_collection.insert_one({
                    "driver_email": driver_email,
                    "booking_id": booking_id,
                    "amount": delivery_fee,
                    "type": "delivery",
                    "status": "pending",
                    "created_at": now
                })

            # Create notification for driver
            if _notifications_collection is not None:
                _notifications_collection.insert_one({
                    "driver_email": driver_email,
                    "type": "delivery_completed",
                    "title": "Delivery Completed",
                    "message": f"Delivery completed successfully. ₹{delivery_fee} added to your earnings.",
                    "read": False,
                    "created_at": now
                })

            return jsonify({"success": True, "message": "Delivery completed successfully!"})

        except Exception as e:
            logger.error(f"OTP verification error: {str(e)}")
            return jsonify({"success": False, "message": "Error verifying OTP"}), 500

    # =====================================================
    # DRIVER SUBMITS / UPDATES BANK DETAILS FOR PAYOUTS
    # =====================================================
    # A driver must have admin-approved bank details on file before
    # they're allowed to request a withdrawal (enforced in
    # payment_admin.request_driver_withdrawal). Any edit here resets
    # approval back to Pending - a driver can't quietly change the
    # account a payout would go to without admin re-checking it.

    @app.route("/driver/bank-details", methods=["POST"])
    @driver_login_required
    def submit_driver_bank_details():
        driver_email = session["driver_email"]

        account_holder_name = request.form.get("account_holder_name", "").strip()
        account_number = request.form.get("account_number", "").strip()
        ifsc = request.form.get("ifsc", "").strip().upper()

        if not account_holder_name:
            flash("Enter the account holder's name.", "danger")
            return redirect(url_for("driver_earnings"))

        if not re.fullmatch(r"\d{9,18}", account_number):
            flash("Enter a valid bank account number (9-18 digits).", "danger")
            return redirect(url_for("driver_earnings"))

        if not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", ifsc):
            flash("Enter a valid IFSC code (e.g. HDFC0001234).", "danger")
            return redirect(url_for("driver_earnings"))

        _drivers_collection.update_one(
            {"email": driver_email},
            {"$set": {
                "bank_details": {
                    "account_holder_name": account_holder_name,
                    "account_number": account_number,
                    "ifsc": ifsc,
                    "approval_status": "Pending",
                    "submitted_at": datetime.now(),
                    "approved_at": None,
                    "approved_by": None,
                }
            }}
        )

        if _notifications_collection is not None:
            _notifications_collection.insert_one({
                "user_email": None,
                "title": "Driver Bank Details Submitted",
                "message": f"{driver_email} submitted bank details for payout approval.",
                "type": "driver_bank_details",
                "status": "info",
                "icon": "🏦",
                "read": False,
                "created_at": datetime.now(),
                "audience": "admin"
            })

        flash("Bank details submitted. Withdrawals are unlocked once admin approves them.", "success")
        return redirect(url_for("driver_earnings"))
    
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
            data = request.get_json(silent=True) or {}
            status = str(data.get("status") or "").strip().capitalize()

            if status not in ("Online", "Offline"):
                return jsonify({
                    "success": False,
                    "message": "Status must be Online or Offline"
                }), 400

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
            return jsonify({"success": False, "message": "Could not update status"}), 500
    
    @app.route("/api/notifications/unread-count")
    @driver_login_required
    def unread_notifications_count():
        count = _notifications_collection.count_documents({
            "driver_email": session["driver_email"],
            "read": False
        })
        return jsonify({"count": count})
    
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