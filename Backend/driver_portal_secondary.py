"""
driver_portal_secondary.py
=====================================================
Second half of the Driver Operations Suite routes (kept separate so each
file stays readable): analytics, profile, notifications, help & support,
and the small admin queue that lets staff actually see support requests.
=====================================================
"""

import logging
import os
import uuid
from datetime import date, datetime, timedelta

import bcrypt
from bson.objectid import ObjectId
from flask import (
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_mail import Message

from driver_dashboard import driver_login_required
from driver_documents import documents_overview, project_root, validate_image_upload
from driver_portal_data import (
    NOTIFICATION_FILTERS,
    analytics_payload,
    booking_ref,
    notification_category,
    performance_summary,
    profile_completeness,
    validate_new_password,
    validate_profile,
)
from image_utils import compress_image_file
from mail_utils import send_mail_capped
from payment_admin import DRIVER_SHARE_RATE

logger = logging.getLogger(__name__)

SUPPORT_CATEGORIES = {
    "delivery": "Delivery problem",
    "payment": "Payment or payout",
    "document": "Document issue",
    "account": "Account or vehicle details change",
    "app": "App problem",
    "other": "Something else",
}
ANALYTICS_RANGES = {"7d": 7, "30d": 30, "90d": 90, "12m": 365}
MAX_ANALYTICS_DAYS = 366
NOTIFICATIONS_PER_PAGE = 15
SUPPORT_HOURLY_LIMIT = 5
SUPPORT_STATUSES = ("Open", "Answered", "Resolved")
SUPPORT_MAX_REPLIES = 60
SUPPORT_TEAM_NAME = "AquaFlow Support"
EMERGENCY_COOLDOWN_SECONDS = 120


def _iso_date(value):
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _password_matches(plain, stored):
    try:
        if isinstance(stored, str):
            stored = stored.encode("utf-8")
        return bcrypt.checkpw(plain.encode("utf-8"), stored)
    except (TypeError, ValueError):
        return False


def faq_items():
    """Answers describe how AquaFlow actually behaves - rates come from the real constant."""
    share = round(DRIVER_SHARE_RATE * 100)
    return [
        ("How do I complete a delivery?",
         "Start the trip, then press Arrived when you reach the customer. Ask the customer for the 6-digit "
         "OTP AquaFlow emailed them and enter it under Complete with OTP. A delivery can only be marked "
         "delivered with that code - there is no way to skip it."),
        ("The customer's OTP isn't working. What now?",
         "Check the digits with the customer. After 5 wrong attempts the delivery is locked to protect the "
         "customer, so send a Delivery problem request below with the booking reference and we will help."),
        ("How much do I earn per delivery?",
         f"You earn {share}% of each delivery fee; the remaining {100 - share}% is the platform commission. "
         "The Earnings page shows the exact split for every delivery."),
        ("What is 'cash to submit'?",
         "When a customer pays cash on delivery, that money belongs to the company. Send it by UPI or bank "
         "transfer and report the transaction reference on the Earnings page. An admin checks it against the "
         "bank statement before it counts as verified."),
        ("How do I withdraw my earnings?",
         "Add your bank details on the Earnings page. Once an admin approves them you can request a "
         "withdrawal of your available balance. A payout is only marked Completed after an admin records the "
         "real bank transfer reference."),
        ("Why was my document rejected?",
         "Open Documents - the reason the reviewer gave is shown on the card. Upload a clear, current copy "
         "(PDF, PNG, JPG or WEBP up to 5 MB) and it goes back into review."),
        ("How do I change my vehicle number or capacity?",
         "These are controlled by the admin team because they affect which deliveries you can be given. "
         "Send an Account or vehicle details change request below."),
        ("Why does the map ask for my location?",
         "AquaFlow uses your phone's GPS to show customers where you are and to detect arrival. Location "
         "is only shared while you are Online. Going Offline stops it."),
    ]


def register_secondary_routes(
    app,
    drivers_collection,
    bookings_collection,
    notifications_collection,
    documents_collection,
    activity_collection,
    support_collection,
    mail,
    render_page,
    driver_or_logout,
    owned_orders,
    inbox_filter,
    notification_view,
):
    def _admin_only():
        return "admin" in session or "admin_email" in session

    def _log(email, kind, detail):
        if activity_collection is not None:
            activity_collection.insert_one({
                "driver_email": email, "type": kind, "detail": detail, "at": datetime.now()})

    # =====================================================
    # ANALYTICS
    # =====================================================

    @app.route("/driver-analytics")
    @driver_login_required
    def driver_analytics():
        driver = driver_or_logout()
        if not driver:
            return redirect(url_for("driver_login"))
        return render_page(
            "driver_analytics.html", driver, "analytics", "Analytics",
            "Your delivery activity and earnings over time.",
            crumbs=[("Dashboard", "driver_dashboard"), ("Analytics", None)],
            today=date.today().isoformat(),
            share_pct=round(DRIVER_SHARE_RATE * 100),
        )

    @app.route("/api/driver/analytics")
    @driver_login_required
    def api_driver_analytics():
        today = date.today()
        selected = request.args.get("range", "30d")

        if selected == "custom":
            start, end = _iso_date(request.args.get("from")), _iso_date(request.args.get("to"))
            if not start or not end:
                return jsonify({"success": False, "message": "Choose both a start and an end date."}), 400
        elif selected in ANALYTICS_RANGES:
            end = today
            start = today - timedelta(days=ANALYTICS_RANGES[selected] - 1)
        else:
            return jsonify({"success": False, "message": "Unknown date range."}), 400

        if start > end:
            return jsonify({"success": False, "message": "The start date must be before the end date."}), 400
        if end > today:
            return jsonify({"success": False, "message": "The end date can't be in the future."}), 400
        if (end - start).days + 1 > MAX_ANALYTICS_DAYS:
            return jsonify({"success": False, "message": "Choose a range of up to 366 days."}), 400

        bucket = request.args.get("bucket")
        payload = analytics_payload(owned_orders(session["driver_email"]), start, end, bucket)
        return jsonify({"success": True, **payload})

    # =====================================================
    # PROFILE
    # =====================================================

    @app.route("/driver-profile")
    @driver_login_required
    def driver_profile():
        email = session["driver_email"]
        driver = driver_or_logout()
        if not driver:
            return redirect(url_for("driver_login"))

        orders = owned_orders(email)
        docs = documents_overview(app, documents_collection, email)
        return render_page(
            "driver_profile.html", driver, "profile", "My Profile",
            "Your details, verification and account security.",
            crumbs=[("Dashboard", "driver_dashboard"), ("My Profile", None)],
            perf=performance_summary(orders),
            docs=docs,
            completeness=profile_completeness(driver, docs, driver.get("bank_details")),
        )

    @app.route("/api/driver/profile", methods=["POST"])
    @driver_login_required
    def api_driver_profile():
        email = session["driver_email"]
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            data = request.form.to_dict()

        clean, errors = validate_profile(data)
        if errors:
            return jsonify({"success": False, "message": "Please fix the highlighted fields.", "errors": errors}), 400

        try:
            drivers_collection.update_one(
                {"email": email}, {"$set": {**clean, "profile_updated_at": datetime.now()}})
            _log(email, "Profile Updated", "Contact and profile details updated")
        except Exception:
            logger.exception("Profile update failed for %s", email)
            return jsonify({"success": False, "message": "We couldn't save your changes. Please try again."}), 500

        # Report back what is really stored, not what was sent.
        saved = drivers_collection.find_one({"email": email}) or {}
        return jsonify({
            "success": True,
            "message": "Profile saved.",
            "profile": {k: saved.get(k) for k in
                        ("phone", "address", "emergency_contact", "experience_years", "license_number")},
        })

    @app.route("/api/driver/profile/photo", methods=["POST"])
    @driver_login_required
    def api_driver_profile_photo():
        email = session["driver_email"]
        upload = request.files.get("photo")
        ext, error = validate_image_upload(upload)
        if error:
            return jsonify({"success": False, "message": error, "errors": {"photo": error}}), 400

        try:
            # Profile photos are public avatars, served from /static like the
            # ones the registration form already saves. DRIVER_PHOTOS_DIR lets
            # tests (or a deployment with a mounted volume) redirect them.
            directory = app.config.get("DRIVER_PHOTOS_DIR") or os.path.join(
                project_root(app), "static", "uploads", "drivers")
            os.makedirs(directory, exist_ok=True)
            filename = f"drv_{uuid.uuid4().hex}{ext}"
            absolute = os.path.join(directory, filename)
            upload.stream.seek(0)
            upload.save(absolute)
            compress_image_file(absolute)

            stored = "static/uploads/drivers/" + filename
            drivers_collection.update_one({"email": email}, {"$set": {"photo": stored}})
            _log(email, "Photo Updated", "Profile photo changed")
            return jsonify({"success": True, "message": "Photo updated.", "photo_url": "/" + stored})
        except Exception:
            logger.exception("Photo upload failed for %s", email)
            return jsonify({"success": False, "message": "We couldn't save that photo. Please try again."}), 500

    @app.route("/api/driver/password", methods=["POST"])
    @driver_login_required
    def api_driver_password():
        email = session["driver_email"]
        data = request.get_json(silent=True) or {}
        current = str(data.get("current_password") or "")
        new = str(data.get("new_password") or "")
        confirm = str(data.get("confirm_password") or "")

        errors = validate_new_password(current, new, confirm)
        driver = drivers_collection.find_one({"email": email}) or {}
        if not errors and not _password_matches(current, driver.get("password")):
            errors["current_password"] = "That isn't your current password."
        if errors:
            return jsonify({"success": False, "message": "Please fix the highlighted fields.", "errors": errors}), 400

        hashed = bcrypt.hashpw(new.encode("utf-8"), bcrypt.gensalt())
        drivers_collection.update_one(
            {"email": email}, {"$set": {"password": hashed, "password_changed_at": datetime.now()}})
        _log(email, "Password Changed", "Account password changed")
        notifications_collection.insert_one({
            "driver_email": email, "type": "account_password", "title": "Password changed",
            "message": "Your AquaFlow driver password was changed. If this wasn't you, contact support now.",
            "status": "info", "read": False, "created_at": datetime.now()})
        return jsonify({"success": True, "message": "Password changed."})

    # =====================================================
    # NOTIFICATIONS
    # =====================================================

    @app.route("/driver-notifications")
    @driver_login_required
    def driver_notifications():
        email = session["driver_email"]
        driver = driver_or_logout()
        if not driver:
            return redirect(url_for("driver_login"))

        selected = request.args.get("filter", "all")
        if selected not in NOTIFICATION_FILTERS:
            selected = "all"
        try:
            page = max(1, int(request.args.get("page", 1)))
        except ValueError:
            page = 1

        latest = list(notifications_collection.find(inbox_filter(email)).sort("created_at", -1).limit(300))
        counts = {name: 0 for name in NOTIFICATION_FILTERS}
        for note in latest:
            counts["all"] += 1
            counts[notification_category(note)] += 1
            if note.get("read") is False:
                counts["unread"] += 1

        if selected == "unread":
            chosen = [n for n in latest if n.get("read") is False]
        elif selected == "all":
            chosen = latest
        else:
            chosen = [n for n in latest if notification_category(n) == selected]

        pages = max(1, -(-len(chosen) // NOTIFICATIONS_PER_PAGE))
        page = min(page, pages)
        window = chosen[(page - 1) * NOTIFICATIONS_PER_PAGE: page * NOTIFICATIONS_PER_PAGE]

        return render_page(
            "driver_notifications.html", driver, "notifications", "Notifications",
            "Updates about your deliveries, payouts and documents.",
            crumbs=[("Dashboard", "driver_dashboard"), ("Notifications", None)],
            notes=[notification_view(n) for n in window],
            counts=counts, selected=selected, cur_page=page, pages=pages, total=len(chosen),
            filters=NOTIFICATION_FILTERS,
        )

    @app.route("/api/driver/notifications")
    @driver_login_required
    def api_driver_notifications():
        email = session["driver_email"]
        try:
            limit = min(30, max(1, int(request.args.get("limit", 6))))
        except ValueError:
            limit = 6
        items = notifications_collection.find(inbox_filter(email)).sort("created_at", -1).limit(limit)
        return jsonify({
            "success": True,
            "unread": notifications_collection.count_documents(inbox_filter(email, {"read": False})),
            "items": [notification_view(n) for n in items],
        })

    @app.route("/api/driver/notifications/<note_id>/read", methods=["POST"])
    @driver_login_required
    def api_driver_notification_read(note_id):
        email = session["driver_email"]
        try:
            oid = ObjectId(note_id)
        except Exception:
            return jsonify({"success": False, "message": "Invalid notification."}), 400
        # Ownership is part of the filter - another driver's id matches nothing.
        result = notifications_collection.update_one(
            {"_id": oid, "driver_email": email, "type": {"$ne": "emergency"}},
            {"$set": {"read": True, "read_at": datetime.now()}})
        if result.matched_count == 0:
            return jsonify({"success": False, "message": "Notification not found."}), 404
        return jsonify({
            "success": True,
            "unread": notifications_collection.count_documents(inbox_filter(email, {"read": False})),
        })

    @app.route("/api/driver/notifications/read-all", methods=["POST"])
    @driver_login_required
    def api_driver_notifications_read_all():
        email = session["driver_email"]
        notifications_collection.update_many(
            inbox_filter(email, {"read": False}),
            {"$set": {"read": True, "read_at": datetime.now()}})
        return jsonify({"success": True, "unread": 0})

    # =====================================================
    # HELP & SUPPORT
    # =====================================================

    def _ticket_ref():
        return "SR-" + uuid.uuid4().hex[:6].upper()

    def _email_support(subject, body):
        """Best-effort email to SUPPORT_EMAIL. True only if it was confirmed sent."""
        recipient = os.getenv("SUPPORT_EMAIL", "").strip()
        if not recipient or mail is None:
            return False
        try:
            msg = Message(subject, recipients=[recipient])
            msg.body = body
            return bool(send_mail_capped(app, mail, msg, timeout_seconds=6))
        except Exception:
            logger.exception("Support email failed")
            return False

    def _recent_count(collection, flt, seconds):
        window = dict(flt)
        window["created_at"] = {"$gte": datetime.now() - timedelta(seconds=seconds)}
        return collection.count_documents(window)

    def _when(moment):
        return moment.strftime("%d %b %Y, %I:%M %p") if isinstance(moment, datetime) else ""

    def support_view(r):
        """One support request as plain JSON-safe data (used by both the driver and admin screens)."""
        category = r.get("category")
        emergency = category == "emergency"
        status = r.get("status") if r.get("status") in SUPPORT_STATUSES else "Open"
        replies = [{
            "by": "admin" if x.get("by") == "admin" else "driver",
            "name": x.get("name") or (SUPPORT_TEAM_NAME if x.get("by") == "admin" else "Driver"),
            "text": x.get("text") or "",
            "when": _when(x.get("at")),
        } for x in (r.get("replies") or [])]
        return {
            "id": str(r["_id"]),
            "ticket": r.get("ticket") or "",
            "subject": r.get("subject") or "(no subject)",
            "message": r.get("message") or "",
            "category": category or "other",
            "category_label": "Emergency" if emergency else SUPPORT_CATEGORIES.get(category, "Something else"),
            "emergency": emergency,
            "priority": r.get("priority") or "normal",
            "status": status,
            "when": _when(r.get("created_at")),
            "booking": booking_ref({"_id": r["booking_id"]}) if r.get("booking_id") else "",
            "replies": replies,
            "reply_count": len(replies),
            "last_activity": _when(r.get("last_activity_at") or r.get("created_at")),
            "driver_name": r.get("driver_name") or "Driver",
            "driver_email": r.get("driver_email") or "",
            "driver_phone": r.get("driver_phone") or "",
            "location": r.get("last_known_location"),
        }

    @app.route("/driver-support")
    @driver_login_required
    def driver_support():
        email = session["driver_email"]
        driver = driver_or_logout()
        if not driver:
            return redirect(url_for("driver_login"))
        recent = list(support_collection.find({"driver_email": email}).sort("created_at", -1).limit(8))
        return render_page(
            "driver_support.html", driver, "support", "Help & Support",
            "Answers, problem reports and urgent help.",
            crumbs=[("Dashboard", "driver_dashboard"), ("Help & Support", None)],
            faqs=faq_items(), categories=SUPPORT_CATEGORIES, requests_list=recent,
            requests_view=[support_view(r) for r in recent],
            open_ticket=(request.args.get("ticket") or "").strip().upper()[:12],
            prefill_category=request.args.get("category") if request.args.get("category") in SUPPORT_CATEGORIES else "",
        )

    @app.route("/api/driver/support/<request_id>")
    @driver_login_required
    def api_driver_support_detail(request_id):
        try:
            oid = ObjectId(request_id)
        except Exception:
            return jsonify({"success": False, "message": "Request not found."}), 404
        doc = support_collection.find_one({"_id": oid, "driver_email": session["driver_email"]})
        if not doc:
            return jsonify({"success": False, "message": "Request not found."}), 404
        return jsonify({"success": True, "request": support_view(doc)})

    @app.route("/api/driver/support/<request_id>/reply", methods=["POST"])
    @driver_login_required
    def api_driver_support_reply(request_id):
        """Driver follow-up on their own request. Puts it back in the admin's open queue."""
        email = session["driver_email"]
        text = str((request.get_json(silent=True) or {}).get("message") or "").strip()
        if not 2 <= len(text) <= 1000:
            return jsonify({"success": False, "message": "Write a reply of 2-1000 characters."}), 400
        try:
            oid = ObjectId(request_id)
        except Exception:
            return jsonify({"success": False, "message": "Request not found."}), 404
        doc = support_collection.find_one({"_id": oid, "driver_email": email})
        if not doc or doc.get("category") == "emergency":
            return jsonify({"success": False, "message": "Request not found."}), 404
        if len(doc.get("replies") or []) >= SUPPORT_MAX_REPLIES:
            return jsonify({"success": False, "message": "This conversation is full. Please open a new request."}), 400
        now = datetime.now()
        driver = drivers_collection.find_one({"email": email}) or {}
        support_collection.update_one({"_id": oid, "driver_email": email}, {
            "$push": {"replies": {"by": "driver", "name": driver.get("name") or "Driver", "text": text, "at": now}},
            "$set": {"status": "Open", "last_activity_at": now, "driver_replied_at": now},
            "$unset": {"resolved_at": "", "resolved_by": ""},
        })
        notifications_collection.insert_one({
            "admin": True, "audience": "admin", "user_email": None, "type": "driver_support",
            "title": "Driver replied on a support request",
            "message": f"{doc.get('ticket')}: {doc.get('subject')}", "status": "info", "read": False,
            "created_at": now})
        _log(email, "Support Reply", doc.get("ticket") or "")
        fresh = support_collection.find_one({"_id": oid})
        return jsonify({"success": True, "message": "Reply sent to the support team.", "request": support_view(fresh)})

    @app.route("/api/driver/support", methods=["POST"])
    @driver_login_required
    def api_driver_support():
        email = session["driver_email"]
        data = request.get_json(silent=True) or {}
        category = str(data.get("category") or "").strip()
        subject = " ".join(str(data.get("subject") or "").split())
        message = str(data.get("message") or "").strip()
        ref_raw = str(data.get("booking_ref") or "").strip().lower().lstrip("#")

        errors = {}
        if category not in SUPPORT_CATEGORIES:
            errors["category"] = "Choose what this is about."
        if not 4 <= len(subject) <= 100:
            errors["subject"] = "Give it a short subject (4-100 characters)."
        if not 10 <= len(message) <= 1000:
            errors["message"] = "Describe the problem in 10-1000 characters."

        booking_id = None
        if ref_raw:
            matches = [o for o in owned_orders(email) if str(o["_id"]).lower().startswith(ref_raw)]
            if len(ref_raw) < 6 or len(matches) != 1:
                errors["booking_ref"] = "We couldn't find that booking among your deliveries."
            else:
                booking_id = str(matches[0]["_id"])
        if errors:
            return jsonify({"success": False, "message": "Please fix the highlighted fields.", "errors": errors}), 400

        if _recent_count(support_collection, {"driver_email": email}, 3600) >= SUPPORT_HOURLY_LIMIT:
            return jsonify({"success": False,
                            "message": "You've sent several requests in the last hour. Please wait a little, "
                                       "or use the emergency alert if it's urgent."}), 429

        driver = drivers_collection.find_one({"email": email}) or {}
        ticket = _ticket_ref()
        now = datetime.now()
        try:
            support_collection.insert_one({
                "ticket": ticket, "driver_email": email, "driver_name": driver.get("name"),
                "driver_phone": driver.get("phone"), "category": category, "subject": subject,
                "message": message, "booking_id": booking_id, "priority": "normal",
                "status": "Open", "emailed": False, "created_at": now,
                "last_activity_at": now, "replies": []})
        except Exception:
            logger.exception("Support request insert failed for %s", email)
            return jsonify({"success": False, "message": "We couldn't save your request. Please try again."}), 500

        notifications_collection.insert_one({
            "admin": True, "audience": "admin", "user_email": None, "type": "driver_support",
            "title": f"Driver support request ({SUPPORT_CATEGORIES[category]})",
            "message": f"{ticket}: {subject}", "status": "info", "read": False, "created_at": now})

        emailed = _email_support(
            f"[AquaFlow] {ticket} - {SUPPORT_CATEGORIES[category]}",
            f"Driver: {driver.get('name')} <{email}> {driver.get('phone') or ''}\n"
            f"Category: {SUPPORT_CATEGORIES[category]}\nSubject: {subject}\n"
            f"Booking: {booking_ref({'_id': booking_id}) if booking_id else '-'}\n\n{message}\n")
        if emailed:
            support_collection.update_one({"ticket": ticket}, {"$set": {"emailed": True}})

        _log(email, "Support Request", f"{ticket} - {SUPPORT_CATEGORIES[category]}")
        return jsonify({
            "success": True, "ticket": ticket, "emailed": emailed,
            "message": f"Request {ticket} saved." + (" The support team has also been emailed." if emailed else ""),
        })

    @app.route("/api/driver/support/emergency", methods=["POST"])
    @driver_login_required
    def api_driver_emergency():
        email = session["driver_email"]
        data = request.get_json(silent=True) or {}
        note = " ".join(str(data.get("message") or "").split())[:300]

        if _recent_count(support_collection, {"driver_email": email, "category": "emergency"},
                         EMERGENCY_COOLDOWN_SECONDS):
            return jsonify({"success": False,
                            "message": "You just sent an emergency alert. The team has it - please wait a "
                                       "couple of minutes before sending another."}), 429

        driver = drivers_collection.find_one({"email": email}) or {}
        now = datetime.now()
        ticket = _ticket_ref()
        message = note or "Driver pressed the emergency alert (no message added)."
        location = None
        if driver.get("current_lat") is not None and driver.get("current_lng") is not None:
            location = {"lat": driver.get("current_lat"), "lng": driver.get("current_lng"),
                        "as_of": driver.get("last_gps_at") or driver.get("location_updated_at")}
        try:
            support_collection.insert_one({
                "ticket": ticket, "driver_email": email, "driver_name": driver.get("name"),
                "driver_phone": driver.get("phone"), "category": "emergency",
                "subject": "Emergency alert", "message": message, "priority": "high",
                "status": "Open", "last_known_location": location, "emailed": False, "created_at": now,
                "last_activity_at": now, "replies": []})
            # Same document shape the app already used for emergency messages.
            notifications_collection.insert_one({
                "type": "emergency", "driver_email": email, "driver_name": driver.get("name"),
                "driver_phone": driver.get("phone"), "message": message,
                "status": "unread", "priority": "high", "created_at": now})
        except Exception:
            logger.exception("Emergency alert insert failed for %s", email)
            return jsonify({"success": False, "message": "We couldn't record your alert. If you are in danger, "
                                                       "call your local emergency number now."}), 500

        emailed = _email_support(
            f"[AquaFlow] URGENT {ticket} - driver emergency alert",
            f"Driver: {driver.get('name')} <{email}> {driver.get('phone') or ''}\n{message}\n"
            + (f"Last known location: {location['lat']}, {location['lng']}\n" if location else ""))
        if emailed:
            support_collection.update_one({"ticket": ticket}, {"$set": {"emailed": True}})

        _log(email, "Emergency Alert", ticket)
        return jsonify({
            "success": True, "ticket": ticket, "emailed": emailed,
            "message": f"Alert {ticket} was recorded for the AquaFlow team. This does not contact emergency "
                       "services - call your local emergency number if anyone is in danger.",
        })

    # =====================================================
    # ADMIN: support inbox (reached from Driver Management)
    # =====================================================

    def _driver_notice(doc, title, message, tone, reply=None):
        """Drops the admin's answer into the driver's Notifications > Support tab."""
        notifications_collection.insert_one({
            "type": "support_reply", "driver_email": doc.get("driver_email"),
            "title": title, "message": message, "status": tone, "read": False,
            "created_at": datetime.now(),
            "support": {
                "id": str(doc["_id"]), "ticket": doc.get("ticket"), "subject": doc.get("subject"),
                "category": "Emergency" if doc.get("category") == "emergency"
                else SUPPORT_CATEGORIES.get(doc.get("category"), "Something else"),
                "description": doc.get("message"), "reply": reply,
                "replied_by": SUPPORT_TEAM_NAME,
                "state": "Resolved" if tone == "success" else "Answered",
            },
        })

    @app.route("/admin/driver-support")
    def admin_driver_support():
        if not _admin_only():
            return redirect(url_for("admin_login"))
        selected = request.args.get("status", "Open")
        if selected not in SUPPORT_STATUSES and selected != "all":
            selected = "Open"
        driver_filter = (request.args.get("driver") or "").strip().lower()
        flt = {} if selected == "all" else {"status": selected}
        if driver_filter:
            flt["driver_email"] = driver_filter
        items = list(support_collection.find(flt).sort("created_at", -1).limit(200))
        base = {"driver_email": driver_filter} if driver_filter else {}
        counts = {name: support_collection.count_documents({**base, "status": name}) for name in SUPPORT_STATUSES}
        counts["all"] = sum(counts.values())
        driver_name = ""
        if driver_filter:
            d = drivers_collection.find_one({"email": driver_filter}) or {}
            driver_name = d.get("name") or driver_filter
        return render_template(
            "admin_driver_support.html", items=[support_view(r) for r in items], selected=selected,
            counts=counts, driver_filter=driver_filter, driver_name=driver_name,
            open_count=support_collection.count_documents({"status": "Open"}),
        )

    @app.route("/admin/driver-support/summary")
    def admin_driver_support_summary():
        """Powers the red badges on the admin dashboard and Driver Management page."""
        if not _admin_only():
            return jsonify({"ok": False}), 401
        by_driver = {}
        for row in support_collection.aggregate([
            {"$match": {"status": "Open"}},
            {"$group": {"_id": "$driver_email", "n": {"$sum": 1}}},
        ]):
            if row["_id"]:
                by_driver[row["_id"]] = row["n"]
        return jsonify({
            "ok": True, "open": sum(by_driver.values()), "by_driver": by_driver,
            "emergency": support_collection.count_documents({"status": "Open", "category": "emergency"}),
        })

    @app.route("/admin/driver-support/<request_id>/reply", methods=["POST"])
    def admin_driver_support_reply(request_id):
        if not _admin_only():
            return jsonify({"ok": False, "message": "Your admin session has expired."}), 401
        data = request.get_json(silent=True) or {}
        text = str(data.get("message") or "").strip()
        resolve = bool(data.get("resolve"))
        if not (2 <= len(text) <= 2000):
            return jsonify({"ok": False, "message": "Write a reply of 2-2000 characters."}), 400
        try:
            oid = ObjectId(request_id)
        except Exception:
            return jsonify({"ok": False, "message": "Request not found."}), 404
        doc = support_collection.find_one({"_id": oid})
        if not doc:
            return jsonify({"ok": False, "message": "Request not found."}), 404
        if len(doc.get("replies") or []) >= SUPPORT_MAX_REPLIES:
            return jsonify({"ok": False, "message": "This conversation is full."}), 400

        now = datetime.now()
        admin_id = session.get("admin_email", "admin")
        fields = {"status": "Resolved" if resolve else "Answered", "last_activity_at": now,
                  "replied_at": now, "replied_by": admin_id}
        if resolve:
            fields.update({"resolved_at": now, "resolved_by": admin_id})
        support_collection.update_one({"_id": oid}, {
            "$push": {"replies": {"by": "admin", "name": SUPPORT_TEAM_NAME, "admin": admin_id, "text": text, "at": now}},
            "$set": fields,
        })
        _driver_notice(
            doc,
            "Support replied to your request" if not resolve else "Your request was resolved",
            text[:160] + ("..." if len(text) > 160 else ""),
            "success" if resolve else "info", reply=text)
        return jsonify({"ok": True, "message": "Reply sent to the driver." if not resolve
                        else "Reply sent and request resolved.",
                        "request": support_view(support_collection.find_one({"_id": oid}))})

    @app.route("/admin/driver-support/<request_id>/<action>", methods=["POST"])
    def admin_driver_support_action(request_id, action):
        if not _admin_only():
            return jsonify({"ok": False, "message": "Your admin session has expired."}), 401
        if action not in ("resolve", "reopen"):
            return jsonify({"ok": False, "message": "Unknown action."}), 400
        try:
            oid = ObjectId(request_id)
        except Exception:
            return jsonify({"ok": False, "message": "Request not found."}), 404

        now = datetime.now()
        if action == "resolve":
            fields = {"status": "Resolved", "resolved_at": now, "last_activity_at": now,
                      "resolved_by": session.get("admin_email", "admin")}
            flt = {"_id": oid, "status": {"$in": ["Open", "Answered"]}}
        else:
            fields = {"status": "Open", "last_activity_at": now}
            flt = {"_id": oid, "status": "Resolved"}
        doc = support_collection.find_one_and_update(flt, {"$set": fields})
        if doc is None:
            return jsonify({"ok": False, "message": "That request has already been updated."}), 409
        if action == "resolve":
            _driver_notice(doc, "Your request was resolved",
                           "The AquaFlow team marked this request as resolved.", "success")
        return jsonify({"ok": True, "message": "Marked resolved." if action == "resolve" else "Reopened."})

    return app
