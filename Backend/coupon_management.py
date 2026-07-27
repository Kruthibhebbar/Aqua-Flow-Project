"""
coupon_management.py
=====================

Feature 4 - Coupon System.

Admin side:
    - Create coupons (e.g. WELCOME100, SAVE50, SUMMER10) as either a
      flat rupee discount or a percentage discount.
    - Optionally mark a coupon "one use per customer, ever" - for
      welcome-style offers like WELCOME100. Once a customer redeems
      it on any booking, they can never apply it again, even after
      removing it or years later on a different booking - this is
      tracked permanently in coupon_redemptions_collection, not on
      the booking itself (which can be edited/cancelled).
    - Activate / deactivate / delete coupons.

Customer side:
    - Can be applied from the booking page itself (at booking-creation
      time, see app.py's /calculate-price and /booking routes) or from
      the payment page (Feature 3) for bookings that don't have one yet.
    - Remove an applied coupon to go back to the original amount - this
      also frees up a one-time coupon so it can be re-applied (removing
      isn't the same as spending it).

The original pre-coupon amount is always kept in "pre_coupon_amount"
so re-applying a different code (or applying twice) never compounds
discounts on top of a previous discount.
"""

from datetime import datetime
from bson.objectid import ObjectId

from flask import request, redirect, url_for, session, flash, render_template, jsonify


def validate_coupon(coupons_collection, coupon_redemptions_collection, code, user_email, base_amount):
    """
    Shared validation used by both the booking page (apply-at-creation)
    and the payment page (apply-after-approval). Returns
    (coupon_doc, discount_amount, error_message) - exactly one of
    coupon_doc or error_message will be None.
    """

    code = (code or "").strip().upper()
    if not code:
        return None, 0, "Please enter a coupon code"

    coupon = coupons_collection.find_one({"code": code, "active": True})
    if not coupon:
        return None, 0, "Invalid or inactive coupon code"

    if coupon.get("one_time_per_user") and coupon_redemptions_collection is not None:
        already_used = coupon_redemptions_collection.find_one({
            "user_email": user_email,
            "code": code
        })
        if already_used:
            return None, 0, f"You've already used {code} before - it's a one-time offer per customer"

    if coupon["discount_type"] == "percent":
        discount = round(base_amount * coupon["value"] / 100, 2)
    else:
        discount = coupon["value"]

    discount = min(discount, base_amount)

    return coupon, discount, None


def record_redemption(coupon_redemptions_collection, user_email, code, booking_id):
    if coupon_redemptions_collection is None:
        return
    coupon_redemptions_collection.insert_one({
        "user_email": user_email,
        "code": code,
        "booking_id": str(booking_id),
        "redeemed_at": datetime.now()
    })


def release_redemption(coupon_redemptions_collection, user_email, code, booking_id):
    """Undo record_redemption - called when a coupon is removed before payment."""
    if coupon_redemptions_collection is None:
        return
    coupon_redemptions_collection.delete_one({
        "user_email": user_email,
        "code": code,
        "booking_id": str(booking_id)
    })


def init_coupon_management(app, coupons_collection, bookings_collection, coupon_redemptions_collection=None):

    # =========================================
    # ADMIN - COUPON MANAGEMENT PAGE
    # =========================================

    @app.route("/coupon-management")
    def coupon_management():

        if "admin" not in session:
            return redirect(url_for("admin_login"))

        coupons = list(coupons_collection.find().sort("created_at", -1))

        return render_template(
            "coupon_management.html",
            coupons=coupons,
            admin_name="Admin",
            admin_email=session.get("admin_email", "admin@aquaflow.com")
        )

    # =========================================
    # ADMIN - CREATE COUPON
    # =========================================

    @app.route("/create-coupon", methods=["POST"])
    def create_coupon():

        if "admin" not in session:
            return jsonify({"success": False, "message": "Unauthorized"})

        try:
            code = request.form.get("code", "").strip().upper()
            discount_type = request.form.get("discount_type", "flat").strip().lower()
            value_raw = request.form.get("value", "0").strip()
            one_time_per_user = request.form.get("one_time_per_user") == "on"

            if not code or len(code) < 3:
                flash("Please enter a valid coupon code (min 3 characters).", "danger")
                return redirect(url_for("coupon_management"))

            if discount_type not in ("flat", "percent"):
                discount_type = "flat"

            try:
                value = float(value_raw)
            except ValueError:
                value = 0

            if value <= 0:
                flash("Please enter a discount value greater than 0.", "danger")
                return redirect(url_for("coupon_management"))

            if discount_type == "percent" and value > 100:
                value = 100

            if coupons_collection.find_one({"code": code}):
                flash(f"A coupon with code {code} already exists.", "danger")
                return redirect(url_for("coupon_management"))

            coupons_collection.insert_one({
                "code": code,
                "discount_type": discount_type,
                "value": value,
                "one_time_per_user": one_time_per_user,
                "active": True,
                "created_at": datetime.now()
            })

            flash(f"Coupon {code} created successfully.", "success")
            return redirect(url_for("coupon_management"))

        except Exception as e:
            flash(f"Could not create coupon: {str(e)}", "danger")
            return redirect(url_for("coupon_management"))

    # =========================================
    # ADMIN - TOGGLE COUPON ACTIVE/INACTIVE
    # =========================================

    @app.route("/toggle-coupon/<coupon_id>", methods=["POST"])
    def toggle_coupon(coupon_id):

        if "admin" not in session:
            return jsonify({"success": False, "message": "Unauthorized"})

        try:
            coupon = coupons_collection.find_one({"_id": ObjectId(coupon_id)})
            if not coupon:
                flash("Coupon not found.", "danger")
                return redirect(url_for("coupon_management"))

            coupons_collection.update_one(
                {"_id": ObjectId(coupon_id)},
                {"$set": {"active": not coupon.get("active", True)}}
            )
            return redirect(url_for("coupon_management"))

        except Exception as e:
            flash(f"Could not update coupon: {str(e)}", "danger")
            return redirect(url_for("coupon_management"))

    # =========================================
    # ADMIN - DELETE COUPON
    # =========================================

    @app.route("/delete-coupon/<coupon_id>", methods=["POST"])
    def delete_coupon(coupon_id):

        if "admin" not in session:
            return jsonify({"success": False, "message": "Unauthorized"})

        try:
            coupons_collection.delete_one({"_id": ObjectId(coupon_id)})
            flash("Coupon deleted.", "success")
            return redirect(url_for("coupon_management"))
        except Exception as e:
            flash(f"Could not delete coupon: {str(e)}", "danger")
            return redirect(url_for("coupon_management"))

    # =========================================
    # CUSTOMER - APPLY COUPON (payment page, Feature 3)
    # =========================================

    @app.route("/apply-coupon/<booking_id>", methods=["POST"])
    def apply_coupon(booking_id):

        if "user_email" not in session:
            return jsonify({"success": False, "message": "Not logged in"}), 401

        data = request.get_json(silent=True) or {}
        code = data.get("code", "").strip().upper()

        try:
            booking = bookings_collection.find_one({"_id": ObjectId(booking_id)})
        except Exception:
            booking = None

        if not booking or booking.get("user_email") != session["user_email"]:
            return jsonify({"success": False, "message": "Booking not found"}), 404

        if booking.get("payment_status") == "Paid (Online)":
            return jsonify({"success": False, "message": "This booking has already been paid for"})

        if booking.get("coupon"):
            return jsonify({"success": False, "message": "A coupon is already applied - remove it first"})

        # Anchor to the ORIGINAL amount so re-applying a different code
        # (or applying twice) never stacks discounts.
        base_amount = booking.get("pre_coupon_amount")
        if base_amount is None:
            base_amount = booking.get("delivery_fee", 0)

        coupon, discount, error = validate_coupon(
            coupons_collection, coupon_redemptions_collection,
            code, session["user_email"], base_amount
        )
        if error:
            return jsonify({"success": False, "message": error})

        new_amount = max(0, round(base_amount - discount, 2))

        bookings_collection.update_one(
            {"_id": ObjectId(booking_id)},
            {"$set": {
                "delivery_fee": new_amount,
                "pre_coupon_amount": base_amount,
                "coupon": {
                    "code": code,
                    "discount_type": coupon["discount_type"],
                    "value": coupon["value"],
                    "discount": discount
                }
            }}
        )
        record_redemption(coupon_redemptions_collection, session["user_email"], code, booking_id)

        return jsonify({
            "success": True,
            "message": f"Coupon {code} applied successfully",
            "discount": discount,
            "new_amount": new_amount,
            "fully_paid": new_amount <= 0
        })

    # =========================================
    # CUSTOMER - REMOVE COUPON
    # =========================================

    @app.route("/remove-coupon/<booking_id>", methods=["POST"])
    def remove_coupon(booking_id):

        if "user_email" not in session:
            return jsonify({"success": False, "message": "Not logged in"}), 401

        try:
            booking = bookings_collection.find_one({"_id": ObjectId(booking_id)})
        except Exception:
            booking = None

        if not booking or booking.get("user_email") != session["user_email"]:
            return jsonify({"success": False, "message": "Booking not found"}), 404

        if not booking.get("coupon"):
            return jsonify({"success": False, "message": "No coupon is applied on this booking"})

        original_amount = booking.get("pre_coupon_amount", booking.get("delivery_fee", 0))
        code = booking["coupon"]["code"]

        bookings_collection.update_one(
            {"_id": ObjectId(booking_id)},
            {
                "$set": {"delivery_fee": original_amount},
                "$unset": {"coupon": "", "pre_coupon_amount": ""}
            }
        )
        release_redemption(coupon_redemptions_collection, session["user_email"], code, booking_id)

        return jsonify({
            "success": True,
            "message": "Coupon removed",
            "new_amount": original_amount
        })

    return app

