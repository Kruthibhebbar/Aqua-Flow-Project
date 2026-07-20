from flask import Blueprint, jsonify
from bson import ObjectId

reject_booking_bp = Blueprint(
    'reject_booking_bp',
    __name__
)


def init_reject_booking(bookings):

    @reject_booking_bp.route(
        '/reject-booking/<booking_id>',
        methods=['POST']
    )
    def reject_booking(booking_id):

        try:

            result = bookings.update_one(
                {"_id": ObjectId(booking_id)},
                {
                    "$set": {
    "status": "Rejected"
}
                }
            )

            if result.modified_count > 0:

                return jsonify({
                    "success": True,
                    "message": "Booking rejected successfully"
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