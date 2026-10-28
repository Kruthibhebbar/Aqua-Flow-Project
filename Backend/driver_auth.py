from flask import (
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash
)
from flask_mail import Message
import hashlib
import hmac
import logging
import random
import traceback
from datetime import timedelta
from pymongo import ReturnDocument
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
    logger = logging.getLogger(__name__)

    # =====================================================
    # LOGIN OTP HELPERS
    # -----------------------------------------------------
    # The session cookie is only *signed*, not encrypted - the browser
    # can read everything in it. So the OTP itself is never stored there;
    # only an HMAC of it, keyed with the server's SECRET_KEY, which the
    # client cannot reproduce. The failed-attempt counter and the lock
    # live on the driver's database record so the client can't reset
    # them by discarding its cookie.
    # =====================================================

    OTP_MAX_ATTEMPTS = 5
    OTP_LOCK_MINUTES = 15
    OTP_VALID_SECONDS = 600

    def _otp_signature(email, otp, issued_at):
        message = f"{email}|{otp}|{issued_at}".encode("utf-8")
        key = str(app.secret_key or "").encode("utf-8")
        return hmac.new(key, message, hashlib.sha256).hexdigest()

    def _start_otp_challenge(email):
        otp_str = str(random.randint(100000, 999999)).zfill(6)
        issued_at = datetime.now().timestamp()
        session["driver_otp_sig"] = _otp_signature(email, otp_str, issued_at)
        session["otp_generated_at"] = issued_at
        return otp_str

    def _send_otp_email(email, otp_str, resend=False):
        try:
            msg = Message(
                "AquaFlow Driver Verification OTP" + (" - Resend" if resend else ""),
                recipients=[email]
            )
            msg.body = f"""
Welcome To AquaFlow Driver Portal.

Your {"NEW " if resend else ""}Login OTP Is: {otp_str}

Do not share this OTP with anyone.

This OTP is valid for 10 minutes.
"""
            sent = send_mail_capped(app, mail, msg, timeout_seconds=6)
            logger.info("Driver login OTP email %s for %s",
                        "sent" if sent else "still sending in background / failed", email)
            return True
        except Exception:
            logger.error("Driver login OTP email failed for %s\n%s", email, traceback.format_exc())
            return False

    def _register_failed_otp(email):
        """Count a wrong code against the driver record. Returns True when
        this failure triggered the lockout."""
        updated = drivers_collection.find_one_and_update(
            {"email": email},
            {"$inc": {"otp_failed_attempts": 1}},
            return_document=ReturnDocument.AFTER
        )
        failures = (updated or {}).get("otp_failed_attempts", 0)
        if failures >= OTP_MAX_ATTEMPTS:
            drivers_collection.update_one(
                {"email": email},
                {"$set": {
                    "otp_failed_attempts": 0,
                    "otp_locked_until": datetime.now() + timedelta(minutes=OTP_LOCK_MINUTES)
                }}
            )
            return True
        return False
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

            # Brute-force lock that the client cannot reset.
            locked_until = driver.get("otp_locked_until")
            if isinstance(locked_until, datetime) and locked_until > datetime.now():
                minutes = int((locked_until - datetime.now()).total_seconds() // 60) + 1
                return (
                    f"Too many incorrect verification codes. "
                    f"Please try again in {minutes} minute(s).",
                    429
                )

            # The password was correct, but the driver is NOT logged in
            # yet. Until the emailed OTP is verified the session only
            # carries a *pending* marker, which no driver-only route
            # accepts (they all require session["driver_email"]).
            session.clear()
            session["pending_driver_email"] = email
            otp_str = _start_otp_challenge(email)
            _send_otp_email(email, otp_str, resend=False)

            return redirect(url_for("verify_driver_otp"))

        return render_template("driver_login.html")

    # =====================================================
    # VERIFY DRIVER OTP - UPDATED to work with HTML template
    # =====================================================

    @app.route("/verify-driver-otp", methods=["GET", "POST"])
    def verify_driver_otp():
        # Already fully logged in
        if session.get("driver_logged_in") and session.get("driver_email"):
            return redirect(url_for("driver_dashboard"))

        pending_email = session.get("pending_driver_email")
        if not pending_email:
            flash("Please login first.", "error")
            return redirect(url_for("driver_login"))

        # Error parameter from URL (for toast messages)
        error_param = request.args.get("error")
        message_param = request.args.get("message")

        if request.method == "POST":
            entered_otp = request.form.get("otp", "").strip()
            signature = session.get("driver_otp_sig")
            issued_at = session.get("otp_generated_at")

            if not signature or not issued_at:
                return redirect(url_for("verify_driver_otp", error="invalid_otp"))

            if datetime.now().timestamp() - issued_at > OTP_VALID_SECONDS:
                session.pop("driver_otp_sig", None)
                session.pop("otp_generated_at", None)
                return redirect(url_for("verify_driver_otp", error="expired"))

            expected = _otp_signature(pending_email, entered_otp, issued_at)
            if not hmac.compare_digest(str(signature), expected):
                if _register_failed_otp(pending_email):
                    session.clear()
                    return (
                        f"Too many incorrect verification codes. "
                        f"Please try again in {OTP_LOCK_MINUTES} minute(s).",
                        429
                    )
                return redirect(url_for("verify_driver_otp", error="invalid_otp"))

            # ---- OTP verified: only now does the session become a login ----
            driver = drivers_collection.find_one({"email": pending_email})
            if not driver or driver.get("approval_status") != "Approved":
                session.clear()
                return redirect(url_for("driver_login"))

            session.pop("driver_otp_sig", None)
            session.pop("otp_generated_at", None)
            session.pop("pending_driver_email", None)
            session["driver_email"] = pending_email
            session["driver_name"] = driver.get("name")
            session["driver_id"] = str(driver["_id"])
            session["driver_logged_in"] = True
            session.permanent = True

            drivers_collection.update_one(
                {"email": pending_email},
                {"$set": {
                    "online_status": "Online",
                    "last_login": datetime.now(),
                    "otp_failed_attempts": 0,
                    "otp_locked_until": None
                }}
            )

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
                        {"email": pending_email},
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
                    "driver_email": pending_email,
                    "type": "Login",
                    "detail": "Logged in",
                    "at": datetime.now()
                })

            return redirect(url_for("driver_dashboard"))

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

        email = session.get("pending_driver_email")

        if not email:
            return redirect(url_for("verify_driver_otp", error="session_expired"))

        # A resend issues a fresh code but does NOT reset the failed-attempt
        # counter (that lives on the driver record), so resending can't be
        # used to get unlimited guesses.
        otp_str = _start_otp_challenge(email)

        if _send_otp_email(email, otp_str, resend=True):
            # Redirect with success message regardless - the OTP is already
            # valid in the session even if the email is slow; the driver can
            # also use "resend" again if it never arrives.
            return redirect(url_for("verify_driver_otp", message="otp_resent"))
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
