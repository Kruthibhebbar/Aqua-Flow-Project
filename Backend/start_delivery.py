from flask import Blueprint, jsonify
from bson import ObjectId
from datetime import datetime  # Add this import

start_delivery_bp = Blueprint(
    'start_delivery_bp',
    __name__
)


def init_start_delivery(bookings, notifications):

    @start_delivery_bp.route(
        '/start-delivery/<booking_id>',
        methods=['POST']
    )
    def start_delivery(booking_id):

        try:
            booking = bookings.find_one({
                "_id": ObjectId(booking_id)
            })
            if not booking:
                return jsonify({
                    "success": False,
                    "message": "Booking not found"
                })

            result = bookings.update_one(
                {"_id": ObjectId(booking_id)},
                {
                    "$set": {
    "status": "On The Way"
}
                }
            )
            
            # ✅ FIXED: Use correct field names matching the notification system
            notifications.insert_one({
                "user_email": booking["user_email"],
                "title": "Delivery Started",
                "message": "Driver started delivery for your booking.",
                "type": "on_way",  # Changed from "delivery_started" to match filter
                "status": "warning",
                "icon": "🚛",
                "read": False,  # ✅ Changed from "is_read" to "read"
                "created_at": datetime.now(),  # ✅ Added created_at field

            })

            if result.modified_count > 0:
                return jsonify({
                    "success": True,
                    "message": "Delivery started successfully"
                })

            return jsonify({
                "success": False,
                "message": "Booking not found"
            })

        except Exception as e:
            return jsonify({
                "success": False,
                "message": str(e)
            }), 500