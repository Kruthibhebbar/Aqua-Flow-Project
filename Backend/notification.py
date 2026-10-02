from flask import render_template, redirect, url_for, session, jsonify, Response
from datetime import datetime
from bson.objectid import ObjectId
import json
import time

# =========================================================
# NOTIFICATION SYSTEM INITIALIZER
# =========================================================

def init_notification(app, notifications_collection, bookings_collection):

    @app.context_processor
    def utility_processor():
        def get_icon(notification_type):
            icons = {
                'confirmed': '✅',
                'on_way': '🚚',
                'payment': '💳',
                'failed': '❌',
                'success': '✅',
                'warning': '⚠️',
                'danger': '🔴',
                'info': 'ℹ️',
                'waiting_driver': '⏳',
                'driver_accepted': '🚛',
                'driver_request': '📦'
            }
            return icons.get(notification_type, '🔔')
        return dict(get_icon=get_icon)

    # =====================================================
    # HELPER FUNCTION FOR REPLACE REDIRECT
    # =====================================================
    def replace_redirect(endpoint):
        """Redirect using JavaScript replace to fix back button issue"""
        return Response(
            f'<!DOCTYPE html><html><head><script>window.location.replace("{url_for(endpoint)}");</script></head><body></body></html>',
            status=200,
            mimetype="text/html"
        )

    # =====================================================
    # HELPER FUNCTION TO GET PENDING REQUESTS
    # =====================================================
    def get_pending_requests():
        """Get all pending driver approval requests"""
        pending_requests = list(
            bookings_collection.find({
                "status": "Waiting Driver Approval",
                "driver_response": "Pending"
            })
        )
        return pending_requests

    # =====================================================
    # MAIN NOTIFICATION PAGE (FIXED)
    # =====================================================
    @app.route("/notification")
    def notification():
        if "user_email" not in session:
            return redirect(url_for("login"))

        user_email = session["user_email"]

        # FIX: Use correct sort syntax
        notifications = list(
            notifications_collection.find({
                "user_email": user_email
            }).sort([("created_at", -1)])  # ✅ Fixed sort syntax
        )

        # Calculate relative time for each notification
        current_time = datetime.now()
        for notif in notifications:
            if notif.get("created_at"):
                diff = current_time - notif["created_at"]
                if diff.days > 0:
                    notif["time"] = f"{diff.days} day(s) ago"
                elif diff.seconds >= 3600:
                    notif["time"] = f"{diff.seconds // 3600} hour(s) ago"
                elif diff.seconds >= 60:
                    notif["time"] = f"{diff.seconds // 60} min(s) ago"
                else:
                    notif["time"] = "Just now"

        total_notifications = len(notifications)
        unread_notifications = notifications_collection.count_documents({
            "user_email": user_email,
            "read": False
        })

        pending_requests = get_pending_requests()

        return render_template(
            "notification.html",
            notifications=notifications,
            total_notifications=total_notifications,
            unread_notifications=unread_notifications,
            pending_requests=pending_requests
        )

    # =====================================================
    # ADD TEST NOTIFICATION (FIXED - removed fake time)
    # =====================================================
    @app.route("/add_test_notification")
    def add_test_notification():
        if "user_email" not in session:
            return redirect(url_for("login"))

        user_email = session["user_email"]

        notifications_collection.insert_one({
            "user_email": user_email,
            "title": "Driver Assigned",
            "message": "Driver Ravi Kumar assigned for your delivery.",
            "type": "on_way",
            "status": "active",
            "icon": "🚚",
            "read": False,
            "created_at": datetime.now()  # ✅ Only created_at
        })

        return replace_redirect("notification")

    # =====================================================
    # BOOKING CONFIRMED NOTIFICATION (FIXED)
    # =====================================================
    @app.route("/add_booking_notification")
    def add_booking_notification():
        if "user_email" not in session:
            return redirect(url_for("login"))

        user_email = session["user_email"]

        notifications_collection.insert_one({
            "user_email": user_email,
            "title": "Booking Confirmed",
            "message": "Your water tanker booking confirmed successfully.",
            "type": "confirmed",
            "status": "success",
            "icon": "✅",
            "read": False,
            "created_at": datetime.now()  # ✅ Only created_at
        })

        return replace_redirect("notification")

    # =====================================================
    # DELIVERY STARTED NOTIFICATION (FIXED)
    # =====================================================
    @app.route("/add_delivery_started")
    def add_delivery_started():
        if "user_email" not in session:
            return redirect(url_for("login"))

        user_email = session["user_email"]

        notifications_collection.insert_one({
            "user_email": user_email,
            "title": "Delivery Started",
            "message": "Your tanker is now on the way.",
            "type": "on_way",
            "status": "warning",
            "icon": "🚛",
            "read": False,
            "created_at": datetime.now()  # ✅ Only created_at
        })

        return replace_redirect("notification")

    # =====================================================
    # PAYMENT SUCCESS NOTIFICATION (FIXED)
    # =====================================================
    @app.route("/add_payment_success")
    def add_payment_success():
        if "user_email" not in session:
            return redirect(url_for("login"))

        user_email = session["user_email"]

        notifications_collection.insert_one({
            "user_email": user_email,
            "title": "Payment Successful",
            "message": "Payment completed successfully.",
            "type": "payment",
            "status": "success",
            "icon": "💳",
            "read": False,
            "created_at": datetime.now()  # ✅ Only created_at
        })

        return replace_redirect("notification")

    # =====================================================
    # PAYMENT FAILED NOTIFICATION (FIXED)
    # =====================================================
    @app.route("/add_payment_failed")
    def add_payment_failed():
        if "user_email" not in session:
            return redirect(url_for("login"))

        user_email = session["user_email"]

        notifications_collection.insert_one({
            "user_email": user_email,
            "title": "Payment Failed",
            "message": "Payment failed. Please retry again.",
            "type": "failed",
            "status": "danger",
            "icon": "❌",
            "read": False,
            "created_at": datetime.now()  # ✅ Only created_at
        })

        return replace_redirect("notification")

    # =====================================================
    # MARK SINGLE NOTIFICATION AS READ
    # =====================================================
    @app.route("/mark_notification_read/<notification_id>")
    def mark_notification_read(notification_id):
        if not session.get("user_email"):
            return redirect(url_for("login"))

        user_email = session["user_email"]

        notifications_collection.update_one(
            {
                "_id": ObjectId(notification_id),
                "user_email": user_email
            },
            {
                "$set": {
                    "read": True
                }
            }
        )

        return replace_redirect("notification")

    # =====================================================
    # MARK ALL NOTIFICATIONS AS READ
    # =====================================================
    @app.route("/mark_all_notifications_read")
    def mark_all_notifications_read():
        if "user_email" not in session:
            return redirect(url_for("login"))

        user_email = session["user_email"]

        notifications_collection.update_many(
            {
                "user_email": user_email
            },
            {
                "$set": {
                    "read": True
                }
            }
        )

        return replace_redirect("notification")

    # =====================================================
    # DELETE SINGLE NOTIFICATION
    # =====================================================
    @app.route("/delete_notification/<notification_id>")
    def delete_notification(notification_id):
        if not session.get("user_email"):
            return redirect(url_for("login"))

        user_email = session["user_email"]

        notifications_collection.delete_one({
            "_id": ObjectId(notification_id),
            "user_email": user_email
        })

        return replace_redirect("notification")

    # =====================================================
    # CLEAR ALL NOTIFICATIONS
    # =====================================================
    @app.route("/clear_all_notifications")
    def clear_all_notifications():
        if "user_email" not in session:
            return redirect(url_for("login"))

        user_email = session["user_email"]

        notifications_collection.delete_many({
            "user_email": user_email
        })

        return replace_redirect("notification")

    # =====================================================
    # FILTER NOTIFICATIONS (FIXED sort syntax)
    # =====================================================
    @app.route("/confirmed_notifications")
    def confirmed_notifications():
        if "user_email" not in session:
            return redirect(url_for("login"))

        user_email = session["user_email"]
        notifications = list(notifications_collection.find({
            "user_email": user_email,
            "type": "confirmed"
        }).sort([("created_at", -1)]))  # ✅ Fixed

        pending_requests = get_pending_requests()
        return render_template(
            "notification.html",
            notifications=notifications,
            pending_requests=pending_requests
        )

    @app.route("/on_way_notifications")
    def on_way_notifications():
        if "user_email" not in session:
            return redirect(url_for("login"))

        user_email = session["user_email"]
        notifications = list(notifications_collection.find({
            "user_email": user_email,
            "type": "on_way"
        }).sort([("created_at", -1)]))  # ✅ Fixed

        pending_requests = get_pending_requests()
        return render_template(
            "notification.html",
            notifications=notifications,
            pending_requests=pending_requests
        )

    @app.route("/payment_notifications")
    def payment_notifications():
        if "user_email" not in session:
            return redirect(url_for("login"))

        user_email = session["user_email"]
        notifications = list(notifications_collection.find({
            "user_email": user_email,
            "type": "payment"
        }).sort([("created_at", -1)]))  # ✅ Fixed

        pending_requests = get_pending_requests()
        return render_template(
            "notification.html",
            notifications=notifications,
            pending_requests=pending_requests
        )

    # =====================================================
    # API NOTIFICATION COUNT
    # =====================================================
    @app.route("/notification_count")
    def notification_count():
        if "user_email" not in session:
            return jsonify({"count": 0})

        user_email = session["user_email"]
        unread_count = notifications_collection.count_documents({
            "user_email": user_email,
            "read": False
        })

        return jsonify({"count": unread_count})

    # =====================================================
    # LIVE NOTIFICATION API
    # =====================================================
    @app.route("/api/live_notifications")
    def live_notifications():
        if "user_email" not in session:
            return jsonify([])

        user_email = session["user_email"]
        notifications = list(
            notifications_collection.find({
                "user_email": user_email
            }).sort([("created_at", -1)])  # ✅ Fixed
        )

        for n in notifications:
            n["_id"] = str(n["_id"])
            if "created_at" in n:
                n["created_at"] = str(n["created_at"])

        return jsonify(notifications)
    
    # =====================================================
    # STREAM NOTIFICATIONS (Server-Sent Events)
    # =====================================================
    @app.route("/stream/notifications")
    def stream_notifications():
        def event_stream():
            user_email = session.get("user_email")
            if not user_email:
                return
            last_check = datetime.now()
            while True:
                try:
                    new_notifications = list(notifications_collection.find({
                        "user_email": user_email,
                        "created_at": {"$gt": last_check}
                    }))
                    if new_notifications:
                        for notif in new_notifications:
                            notif["_id"] = str(notif["_id"])
                            if "created_at" in notif:
                                notif["created_at"] = str(notif["created_at"])
                            yield f"data: {json.dumps(notif)}\n\n"
                        last_check = datetime.now()
                    time.sleep(5)
                except Exception as e:
                    print(f"Stream error: {e}")
                    time.sleep(5)
        return Response(event_stream(), mimetype="text/event-stream")