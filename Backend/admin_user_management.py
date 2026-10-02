from flask import (
    render_template,
    redirect,
    url_for,
    session,
    request,
    jsonify
)

from datetime import datetime
from bson.objectid import ObjectId
from bson.errors import InvalidId


def init_admin_user_management(
    app,
    users_collection,
    bookings_collection,
    notifications_collection
):
    """
    Registers everything the admin "Users" page needs:

      GET  /admin/users                    -> the customer directory page
      GET  /admin/users/<id>/bookings       -> JSON booking history for the (i) modal
      POST /admin/users/<id>/message         -> sends a message into the user's notifications
      POST /admin/users/<id>/toggle-block    -> suspend / restore a customer account
    """

    def _admin_ok():
        return "admin" in session or "admin_email" in session

    # =====================================================
    # USERS DIRECTORY PAGE
    # =====================================================

    @app.route("/admin/users")
    def admin_users():

        if not _admin_ok():
            return redirect(url_for("admin_login"))

        search = request.args.get("search", "").strip()

        query = {}
        if search:
            query = {
                "$or": [
                    {"name": {"$regex": search, "$options": "i"}},
                    {"email": {"$regex": search, "$options": "i"}},
                    {"phone": {"$regex": search, "$options": "i"}}
                ]
            }

        users = list(
            users_collection.find(query).sort("created_at", -1)
        )

        # =====================================================
        # PER-USER LIVE STATS - orders placed / delivered / cancelled,
        # the most recent order (with its assigned driver), so the
        # admin doesn't have to open every profile to see this.
        # =====================================================

        for u in users:

            u["_id"] = str(u["_id"])
            email = u.get("email", "")

            u["total_orders"] = bookings_collection.count_documents({
                "user_email": email
            })

            u["delivered_orders"] = bookings_collection.count_documents({
                "user_email": email,
                "status": "Delivered"
            })

            u["cancelled_orders"] = bookings_collection.count_documents({
                "user_email": email,
                "status": "Cancelled"
            })

            u["active_orders"] = bookings_collection.count_documents({
                "user_email": email,
                "status": {"$in": ["Pending", "Approved", "On The Way", "Accepted"]}
            })

            last_order = bookings_collection.find_one(
                {"user_email": email},
                sort=[("created_at", -1)]
            )
            u["last_order"] = last_order

        # =====================================================
        # HEADLINE STATS FOR THE TOP CARDS
        # =====================================================

        total_users = users_collection.count_documents({})

        month_start = datetime.now().replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        new_this_month = users_collection.count_documents({
            "created_at": {"$gte": month_start}
        })

        blocked_users = users_collection.count_documents({
            "is_blocked": True
        })

        total_orders_all = bookings_collection.count_documents({})

        return render_template(
            "admin_users.html",
            users=users,
            search=search,
            total_users=total_users,
            new_this_month=new_this_month,
            blocked_users=blocked_users,
            total_orders_all=total_orders_all
        )

    # =====================================================
    # BOOKING HISTORY (used by the "(i)" icon's modal)
    # =====================================================

    @app.route("/admin/users/<user_id>/bookings")
    def admin_user_bookings(user_id):

        if not _admin_ok():
            return jsonify({"success": False, "message": "Unauthorized"}), 401

        try:
            user = users_collection.find_one({"_id": ObjectId(user_id)})
        except InvalidId:
            return jsonify({"success": False, "message": "Invalid user id"}), 400

        if not user:
            return jsonify({"success": False, "message": "User not found"}), 404

        bookings = list(
            bookings_collection.find({"user_email": user.get("email")})
            .sort("created_at", -1)
        )

        out = []
        for b in bookings:
            created_at = b.get("created_at")
            out.append({
                "id": str(b["_id"]),
                "quantity": b.get("quantity", ""),
                "water_type": b.get("water_type", ""),
                "address": b.get("address", ""),
                "status": b.get("status", "Pending"),
                "delivery_date": b.get("delivery_date", ""),
                "delivery_time": b.get("delivery_time", ""),
                "created_at": created_at.strftime("%d %b %Y, %I:%M %p") if created_at else "",
                "driver_name": b.get("driver_name", "Not Assigned"),
                "truck_number": b.get("truck_number", "Not Assigned"),
                "delivery_fee": b.get("delivery_fee", ""),
                "payment_method": b.get("payment_method", "")
            })

        return jsonify({
            "success": True,
            "user_name": user.get("name", ""),
            "bookings": out
        })

    # =====================================================
    # SEND A MESSAGE TO A USER (lands in their Notifications page)
    # =====================================================

    @app.route("/admin/users/<user_id>/message", methods=["POST"])
    def admin_message_user(user_id):

        if not _admin_ok():
            return jsonify({"success": False, "message": "Unauthorized"}), 401

        try:
            user = users_collection.find_one({"_id": ObjectId(user_id)})
        except InvalidId:
            return jsonify({"success": False, "message": "Invalid user id"}), 400

        if not user:
            return jsonify({"success": False, "message": "User not found"}), 404

        message = request.form.get("message", "").strip()

        if not message:
            return jsonify({"success": False, "message": "Message can't be empty"}), 400

        notifications_collection.insert_one({
            "user_email": user.get("email"),
            "title": "Message from AquaFlow Support",
            "message": message,
            "type": "info",
            "status": "info",
            "icon": "📩",
            "read": False,
            "created_at": datetime.now(),
            "sent_by_admin": session.get("admin_email", "admin")
        })

        return jsonify({"success": True, "message": "Message sent"})

    # =====================================================
    # BLOCK / UNBLOCK A USER ACCOUNT
    # =====================================================

    @app.route("/admin/users/<user_id>/toggle-block", methods=["POST"])
    def admin_toggle_block_user(user_id):

        if not _admin_ok():
            return jsonify({"success": False, "message": "Unauthorized"}), 401

        try:
            user = users_collection.find_one({"_id": ObjectId(user_id)})
        except InvalidId:
            return jsonify({"success": False, "message": "Invalid user id"}), 400

        if not user:
            return jsonify({"success": False, "message": "User not found"}), 404

        new_state = not user.get("is_blocked", False)

        users_collection.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"is_blocked": new_state}}
        )

        # Let the user know what happened, if they still have access
        # to the app to see it - and quietly skip it if they're
        # blocked (no point notifying someone who can't log in).
        if not new_state:
            notifications_collection.insert_one({
                "user_email": user.get("email"),
                "title": "Account Restored",
                "message": "Your AquaFlow account has been restored. You can log in normally again.",
                "type": "success",
                "status": "success",
                "icon": "✅",
                "read": False,
                "created_at": datetime.now()
            })

        return jsonify({
            "success": True,
            "is_blocked": new_state
        })
