from flask import (
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash
)
from flask_mail import Message
import random
import traceback
from werkzeug.utils import secure_filename
from datetime import datetime
import bcrypt
from mail_utils import send_mail_capped
from image_utils import compress_image_file


def init_driver_auth(
    app,
    drivers_collection,
    mail,
    activity_collection=None
):
    # =====================================================
    # DRIVER REGISTER
    # =====================================================

    @app.route("/driver-register", methods=["GET", "POST"])
    def driver_register():

        if request.method == "POST":

            name = request.form.get("name")
            address = request.form.get("address")
            truck_capacity = int(request.form.get("truck_capacity"))
            truck_number = request.form.get("truck_number")
            email = request.form.get("email").strip().lower()
            phone = request.form.get("phone")
            password = request.form.get("password")

            photo = request.files.get("photo")

            if not all([
                name,
                address,
                truck_capacity,
                truck_number,
                email,
                phone,
                password
            ]):

                return "All fields are required"

            if not photo or photo.filename == "":

                return "Driver photo is required"

            existing_driver = drivers_collection.find_one({

                "email": email
            })

            if existing_driver:

                return "Driver already exists"

            filename = f"{datetime.now().timestamp()}_{secure_filename(photo.filename)}"

            photo_path = f"static/uploads/drivers/{filename}"

            photo.save(photo_path)
            compress_image_file(photo_path)  # PERF: shrink for faster page loads (safe no-op on failure)

            hashed_password = bcrypt.hashpw(
                password.encode("utf-8"),
                bcrypt.gensalt()
            )

            drivers_collection.insert_one({

                "name": name,
                "address": address,
                "truck_capacity": truck_capacity,
                "truck_number": truck_number,
                "email": email,
                "phone": phone,
                "password": hashed_password,
                "photo": photo_path,
                "online_status": "Offline",
                "activity_status": "Available",
                "total_orders": 0,
                "total_earnings": 0,
                "pending_payments": 0,
                "completed_deliveries": 0,
                "joined_at": datetime.now(),
                "approval_status": "Pending"
            })

            return redirect(
                url_for("driver_login")
            )

        return render_template(
            "driver_register.html"
        )

    # =====================================================
    # DRIVER LOGIN
    # =====================================================

    @app.route("/driver-login", methods=["GET", "POST"])
    def driver_login():

        # ALREADY LOGGED IN -> SKIP LOGIN PAGE ENTIRELY
        if request.method == "GET" and session.get("driver_logged_in"):
            return redirect(url_for("driver_dashboard"))

        if request.method == "POST":

            email = request.form.get("email").strip().lower()
            password = request.form.get("password")

            try:
                driver = drivers_collection.find_one({
                    "email": email
                })
            except Exception as e:
                # With the 5s Mongo timeout set in app.py, a DB outage now
                # surfaces here quickly and clearly instead of the request
                # hanging for ~30s and then failing with no explanation.
                print(f"driver_login DB error: {e}")
                return "We couldn't reach the database right now. Please try again in a moment.", 503

            if not driver:
                return "Invalid email"

            password_match = bcrypt.checkpw(
                password.encode("utf-8"),
                driver["password"]
            )

            if not password_match:
                return "Invalid password"

            if driver.get("approval_status") != "Approved":
                return "Waiting For Admin Approval"

            # Generate OTP
            otp = random.randint(100000, 999999)
            otp_str = str(otp).zfill(6)
            
            # Store in session
            session.clear()
            session["driver_otp"] = otp_str
            session["driver_email"] = email
            session["driver_name"] = driver["name"]
            session["driver_id"] = str(driver["_id"])
            session["otp_generated_at"] = datetime.now().timestamp()
            
            print("========== OTP GENERATED ==========")
            print(f"Email: {email}")
            print(f"OTP: {otp_str}")
            print(f"Session data: {dict(session)}")
            print("===================================")

            # Send email
            try:
                msg = Message(
                    "AquaFlow Driver Verification OTP",
                    recipients=[email]
                )
                msg.body = f"""
Welcome To AquaFlow Driver Portal.

Your Login OTP Is: {otp_str}

Do not share this OTP with anyone.

This OTP is valid for 10 minutes.
"""
                sent = send_mail_capped(app, mail, msg, timeout_seconds=6)
                print(f"OTP email {'sent' if sent else 'still sending in background / failed'} for {email}")
            except Exception as e:
                print(f"Email sending failed: {e}")
                print(traceback.format_exc())

            drivers_collection.update_one(
                {"email": email},
                {"$set": {
                    "online_status": "Online",
                    "last_login": datetime.now()
                }}
            )

            return redirect(url_for("verify_driver_otp"))

        return render_template("driver_login.html")

    # =====================================================
    # VERIFY DRIVER OTP - UPDATED to work with HTML template
    # =====================================================

    @app.route("/verify-driver-otp", methods=["GET", "POST"])
    def verify_driver_otp():
        # Check if already logged in
        if session.get("driver_logged_in"):
           return redirect(url_for("home"))

        # Check if driver email exists in session
        if "driver_email" not in session:
            flash("Please login first.", "error")
            return redirect(url_for("driver_login"))

        # Get error parameter from URL (for toast messages)
        error_param = request.args.get("error")
        message_param = request.args.get("message")

        if request.method == "POST":
            entered_otp = request.form.get("otp", "").strip()
            
            print("\n========== VERIFICATION ATTEMPT ==========")
            print(f"Full session data: {dict(session)}")
            print(f"Entered OTP: '{entered_otp}'")
            
            stored_otp = session.get("driver_otp")
            print(f"Stored OTP from session: '{stored_otp}'")
            
            if not stored_otp:
                print("ERROR: No OTP found in session!")
                # Use URL parameter instead of flash
                return redirect(url_for("verify_driver_otp", error="invalid_otp"))
            
            # Check OTP expiry
            otp_time = session.get("otp_generated_at")
            if otp_time:
                current_time = datetime.now().timestamp()
                time_diff = current_time - otp_time
                print(f"OTP age: {time_diff:.2f} seconds")
                if time_diff > 600:  # 10 minutes
                    print("OTP EXPIRED!")
                    session.pop("driver_otp", None)
                    session.pop("otp_generated_at", None)
                    return redirect(url_for("verify_driver_otp", error="expired"))
            
            otp_match = (entered_otp == stored_otp)
            print(f"OTP Match: {otp_match}")
            print("==========================================\n")
            
            if otp_match:
                session["driver_logged_in"] = True
                session.permanent = True
                session.pop("driver_otp", None)
                session.pop("otp_generated_at", None)
                print("OTP VERIFIED SUCCESSFULLY!")

                # Save the real GPS fix captured on the OTP page (if the
                # driver allowed location access) as their starting position -
                # this is what lets the live map / admin ETA calculation
                # start from the driver's actual location instead of a
                # random/stale one.
                driver_lat = request.form.get("driver_lat")
                driver_lng = request.form.get("driver_lng")
                if driver_lat and driver_lng:
                    try:
                        drivers_collection.update_one(
                            {"email": session["driver_email"]},
                            {"$set": {
                                "current_lat": float(driver_lat),
                                "current_lng": float(driver_lng),
                                "location_updated_at": datetime.now()
                            }}
                        )
                    except (TypeError, ValueError):
                        pass

                if activity_collection is not None:
                    activity_collection.insert_one({
                        "driver_email": session["driver_email"],
                        "type": "Login",
                        "detail": "Logged in",
                        "at": datetime.now()
                    })

                return redirect(url_for("driver_dashboard"))
            else:
                print("OTP VERIFICATION FAILED!")
                # Use URL parameter instead of flash for toast display
                return redirect(url_for("verify_driver_otp", error="invalid_otp"))

        # GET request - render template with error/message parameters
        return render_template(
            "verify_driver_otp.html",
            error=error_param,
            message=message_param
        )
    
    # =====================================================
    # RESEND DRIVER OTP - UPDATED to match HTML action
    # =====================================================

    @app.route("/resend-otp", methods=["GET", "POST"])
    def resend_otp():
        
        email = session.get("driver_email")
        
        if not email:
            return redirect(url_for("verify_driver_otp", error="session_expired"))
        
        # Generate new OTP
        otp = random.randint(100000, 999999)
        otp_str = str(otp).zfill(6)
        
        # Update session with new OTP
        session["driver_otp"] = otp_str
        session["otp_generated_at"] = datetime.now().timestamp()
        
        print("========== OTP RESENT ==========")
        print(f"Email: {email}")
        print(f"New OTP: {otp_str}")
        print(f"Session data: {dict(session)}")
        print("================================")
        
        # Send email with new OTP
        try:
            msg = Message(
                "AquaFlow Driver Verification OTP - Resend",
                recipients=[email]
            )
            msg.body = f"""
Welcome To AquaFlow Driver Portal.

Your NEW Login OTP Is: {otp_str}

Do not share this OTP with anyone.

This OTP is valid for 10 minutes.
"""
            sent = send_mail_capped(app, mail, msg, timeout_seconds=6)
            print(f"Resent OTP email {'sent' if sent else 'still sending in background / failed'} for {email}")
            # Redirect with success message parameter regardless - the OTP
            # is already valid in the session even if the email is slow;
            # the driver can also use "resend" again if it never arrives.
            return redirect(url_for("verify_driver_otp", message="otp_resent"))
        except Exception as e:
            print(f"Email sending failed: {e}")
            print(traceback.format_exc())
            return redirect(url_for("verify_driver_otp", error="resend_failed"))

    # =====================================================
    # DRIVER LOGOUT
    # =====================================================

    @app.route("/driver-logout")
    def driver_logout():

        if "driver_email" in session:
            drivers_collection.update_one(
                {"email": session["driver_email"]},
                {"$set": {"online_status": "Offline"}}
            )
            session.clear()

        return redirect(url_for("driver_login"))