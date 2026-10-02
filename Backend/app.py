from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from flask_mail import Mail, Message
import pymongo
import bcrypt
import random
from datetime import datetime, timedelta
from bson.objectid import ObjectId
import re
import os
from urllib.parse import quote
from dotenv import load_dotenv
from werkzeug.utils import secure_filename
from image_utils import compress_image_file
from driver_assignment import init_driver_assignment
from notification import init_notification
from driver_auth import init_driver_auth
from driver_dashboard import init_driver_dashboard
from driver_management import init_driver_management
from finance_management import init_finance_management
from new_driver_requests import init_new_driver_requests
from booking_management import init_booking_management
from driver_notifications import init_driver_notifications
from live_status_tracking import init_live_tracking
from payment_admin import init_payment_admin, get_bank_details, get_finance_overview, get_ledger_overview, get_top_drivers, get_top_customers
from razorpay_payments import init_razorpay_payments, razorpay_configured
from wallet_recharge import init_wallet_recharge
from payment_management import init_payment_management
from coupon_management import init_coupon_management, validate_coupon, record_redemption
from business_insights import init_business_insights
from pricing_engine import calculate_booking_price
from quickrebook import init_quickrebook
from gps_engine import init_gps_engine
from mail_utils import send_mail_capped
from admin_user_management import init_admin_user_management

# =====================================================
# LOAD ENV
# =====================================================

load_dotenv()


# =====================================================
# FLASK APP
# =====================================================

app = Flask(

    __name__,

    template_folder='../templates',

    static_folder='../static'
)



# =====================================================
# SECRET KEY
# =====================================================

app.secret_key = os.getenv("SECRET_KEY")


# =====================================================
# MAIL CONFIG (Gmail SMTP only - no Resend, no fallback)
#
# Port/TLS/SSL are read from the environment with Gmail's normal
# defaults (587 + STARTTLS) so they can be changed without touching
# code - useful if a network blocks 587 specifically (common on some
# ISPs/college networks) and 465 (implicit SSL) needs to be used
# instead: set MAIL_PORT=465 and MAIL_USE_SSL=true in .env.
# =====================================================

app.config['MAIL_SERVER'] = os.getenv("MAIL_SERVER", "smtp.gmail.com")
app.config['MAIL_PORT'] = int(os.getenv("MAIL_PORT", 587))
app.config['MAIL_USE_TLS'] = os.getenv("MAIL_USE_TLS", "true").strip().lower() == "true"
app.config['MAIL_USE_SSL'] = os.getenv("MAIL_USE_SSL", "false").strip().lower() == "true"

app.config['MAIL_USERNAME'] = os.getenv("MAIL_USERNAME")
app.config['MAIL_PASSWORD'] = os.getenv("MAIL_PASSWORD")
app.config['MAIL_DEFAULT_SENDER'] = os.getenv("MAIL_USERNAME")

# ================= CACHE FIX =================
# PERF: previously this applied "no-cache, no-store" to EVERY response,
# including static files (CSS/JS/images), so the browser re-downloaded
# them on every page load. Static assets are now allowed to cache in the
# browser; dynamic/session pages keep the original no-cache behaviour
# unchanged so login/dashboard freshness is unaffected.
@app.after_request
def add_header(response):
    if request.path.startswith('/static/'):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    else:
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# ================= RESPONSE COMPRESSION =================
# PERF: gzip/br-compresses HTML/JSON/CSS/JS responses before sending them
# over the wire. Purely additive - no route/behaviour changes, just
# smaller responses. Wrapped in try/except so a missing dependency
# (e.g. before `pip install -r requirements.txt` is re-run) can never
# crash the app.
try:
    from flask_compress import Compress
    Compress(app)
except ImportError:
    pass

mail = Mail(app)

client = pymongo.MongoClient(
    os.getenv("MONGO_URI"),
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
    socketTimeoutMS=10000
)

db = client["aquaflow"]

users_collection = db["users"]
bookings_collection = db["bookings"]
admins_collection = db["admins"]
notifications_collection = db["notifications"]
drivers_collection = db["drivers"]
earnings_collection = db["earnings"]

settings_collection = db["settings"]
transactions_collection = db["transactions"]


def blocked_user_response():
    """
    Shared guard for any customer-facing action that should be denied
    once an admin has blocked the account (see admin_user_management.py).
    Blocking already prevents a fresh login, but a user who was already
    logged in keeps their session - so anything that creates or changes
    an order also has to check this, not just the login route.

    Returns a Flask response (kicking the user back to login with an
    explanation) if the currently-signed-in user is blocked, or None
    if they're clear to proceed.
    """

    email = session.get("user_email")
    if not email:
        return None

    user = users_collection.find_one({"email": email})
    if user and user.get("is_blocked"):
        session.clear()
        return render_template(
            "login.html",
            error="Your account has been suspended. Contact AquaFlow support for help."
        )

    return None
coupons_collection = db["coupons"]
wallet_transactions_collection = db["wallet_transactions"]
coupon_redemptions_collection = db["coupon_redemptions"]
wallet_topups_collection = db["wallet_topups"]
counters_collection = db["counters"]
documents_collection = db["driver_documents"]
driver_activity_collection = db["driver_activity"]
from payment_ledger import init_payment_ledger, log_transaction
payment_transactions_collection = init_payment_ledger(db)


def expire_stale_awaiting_payment_bookings():
    """
    'Pay Now' bookings start life as status "Awaiting Payment" - not a
    real booking until payment actually succeeds. If someone abandons
    checkout (closes the tab, never comes back), we don't want that
    sitting around forever looking like a live order. Rather than a
    background scheduler, this runs as a cheap lazy sweep any time a
    customer or admin views their booking list - anything still
    "Awaiting Payment" after 30 minutes gets auto-cancelled.
    """
    try:
        cutoff = datetime.now() - timedelta(minutes=30)
        bookings_collection.update_many(
            {"status": "Awaiting Payment", "created_at": {"$lt": cutoff}},
            {"$set": {
                "status": "Cancelled",
                "auto_cancelled_reason": "Payment was never completed within 30 minutes"
            }}
        )
    except Exception:
        # Never let a cleanup sweep break the page that triggered it.
        pass

# Real-time GPS engine collections:
# driver_locations = latest known fix per driver (fast reads)
# tracking_history  = every GPS point ever received (replay / analytics / ML)
driver_locations_collection = db["driver_locations"]
tracking_history_collection = db["tracking_history"]  
try:
    users_collection.create_index("email")
    bookings_collection.create_index("user_email")
    bookings_collection.create_index("driver_email")
    bookings_collection.create_index("status")
    bookings_collection.create_index("payment_status")
    bookings_collection.create_index("created_at")
    drivers_collection.create_index("email")
    drivers_collection.create_index("phone")
    notifications_collection.create_index("user_email")
    notifications_collection.create_index("driver_email")
    notifications_collection.create_index("read")
    notifications_collection.create_index("created_at")
    driver_locations_collection.create_index("driver_id")
    tracking_history_collection.create_index("driver_id")
    transactions_collection.create_index("driver_email")
    transactions_collection.create_index("status")
    coupons_collection.create_index("code")
except Exception as e:
    print("Index creation skipped (non-fatal):", e)

init_driver_assignment(
    app,
    bookings_collection,
    users_collection,
    drivers_collection,
    notifications_collection
)
init_notification(
    app,
    notifications_collection,
    bookings_collection
)
init_driver_auth(
    app,
    drivers_collection,
    mail,
    activity_collection=driver_activity_collection
)
init_driver_dashboard(
    app,
    drivers_collection,
    bookings_collection,
    notifications_collection,
    earnings_collection=earnings_collection,
    documents_collection=documents_collection,
    activity_collection=driver_activity_collection,
    mail=mail,
    settings_collection=settings_collection,
    transactions_collection=transactions_collection,
    payment_transactions_collection=payment_transactions_collection
)
init_driver_management(
    app,
    drivers_collection,
    bookings_collection,
    settings_collection=settings_collection,
    transactions_collection=transactions_collection,
    documents_collection=documents_collection
)
init_finance_management(
    app,
    drivers_collection,
    bookings_collection,
    settings_collection=settings_collection,
    transactions_collection=transactions_collection,
    payment_transactions_collection=payment_transactions_collection,
    notifications_collection=notifications_collection
)
init_new_driver_requests(
    app,
    drivers_collection
)
init_booking_management(
    app,
    bookings_collection,
    drivers_collection
)
init_driver_notifications(
    app,
    bookings_collection,
    drivers_collection,
    notifications_collection
)
# NOTE: the old start_delivery.py / reject_booking.py blueprints used to be
# registered here as well, but they defined the exact same URL rules
# ("/start-delivery/<id>" POST and "/reject-booking/<id>" POST) that are
# now owned by driver_dashboard.py / driver_notifications.py. Having two
# rules for the same path+method is what made the Start/Reject buttons
# behave unpredictably - only one module's code was actually running.
# They have been removed; driver_dashboard.py and driver_notifications.py
# are now the single source of truth for those actions.
init_live_tracking(
    app,
    bookings_collection
)
init_payment_admin(
    app,
    bookings_collection,
    drivers_collection,
    notifications_collection,
    settings_collection,
    transactions_collection,
    payment_transactions_collection
)
init_razorpay_payments(
    app,
    bookings_collection,
    settings_collection,
    notifications_collection,
    payment_transactions_collection
)
init_wallet_recharge(
    app,
    users_collection,
    wallet_transactions_collection,
    wallet_topups_collection,
    payment_transactions_collection
)
init_payment_management(app, payment_transactions_collection)
init_coupon_management(
    app,
    coupons_collection,
    bookings_collection,
    coupon_redemptions_collection
)
init_business_insights(
    app,
    bookings_collection,
    drivers_collection
)
init_gps_engine(
    app,
    drivers_collection,
    bookings_collection,
    driver_locations_collection,
    tracking_history_collection,
    notifications_collection
)
init_quickrebook(
    app,
    bookings_collection,
    users_collection
)

init_admin_user_management(
    app,
    users_collection,
    bookings_collection,
    notifications_collection
)

# ================= HOME =================
@app.route('/')
def home():
    return render_template('index.html')

# ================= HEALTH CHECK =================
# Pings both the web process AND the actual database connection - the
# keepalive workflow hits this instead of "/" so a broken Mongo Atlas
# connection (e.g. Render's IP falling outside the Atlas allow-list) shows
# up as a failing ping in GitHub Actions instead of silently only showing
# up when a real driver/customer tries to log in.
@app.route('/healthz')
def healthz():
    try:
        client.admin.command('ping')
        return jsonify({"status": "ok", "db": "connected"}), 200
    except Exception as e:
        return jsonify({"status": "error", "db": "unreachable", "detail": str(e)}), 503

# ================= LOGIN =================
@app.route('/login', methods=['GET', 'POST'])
def login():

    # ALREADY LOGGED IN -> SKIP LOGIN PAGE ENTIRELY
    # (this is what makes the Back button land on the dashboard
    # instead of re-showing the login form)
    if request.method == 'GET' and 'user_email' in session:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':

        email = request.form['email'].strip().lower()
        password = request.form['password'].strip()

        if not email or not password:
            return render_template(
                'login.html',
                error="Please fill all fields"
            )

        user = users_collection.find_one({
            "email": {
                "$regex": f"^{re.escape(email)}$",
                "$options": "i"
            }
        })

        if not user:
            return render_template(
                'login.html',
                error="Account not found"
            )

        try:
            password_correct = bcrypt.checkpw(
                password.encode('utf-8'),
                user['password'].encode('utf-8')
            )
        except:
            return render_template(
                'login.html',
                error="Password format invalid"
            )

        if password_correct:

            # ACCOUNT BLOCKED BY ADMIN (see admin_user_management.py) -
            # deny login entirely rather than letting them into a
            # dashboard, even though the password was right.
            if user.get("is_blocked"):
                return render_template(
                    'login.html',
                    error="Your account has been suspended. Contact AquaFlow support for help."
                )

            # UPDATE LAST LOGIN
            users_collection.update_one(
                {"email": user['email']},
                {
                    "$set": {
                        "last_login": datetime.now()
                    }
                }
            )

            session['user_id'] = str(user['_id'])
            session['user_name'] = user['name']
            session['user_email'] = user['email']

            return redirect(url_for('dashboard'))

        else:
            return render_template(
                'login.html',
                error="Wrong password"
            )

    return render_template('login.html')

# ================= SIGNUP =================
@app.route('/signup', methods=['GET', 'POST'])
def signup():

    if request.method == 'POST':

        name = request.form['name'].strip()
        email = request.form['email'].strip().lower()
        password = request.form['password'].strip()

        if not name or not email or not password:
            return render_template(
                'signup.html',
                error="Please fill all fields"
            )

        if len(password) < 6:
            return render_template(
                'signup.html',
                error="Password must be at least 6 characters"
            )

        existing_user = users_collection.find_one({
            "email": {
                "$regex": f"^{re.escape(email)}$",
                "$options": "i"
            }
        })

        if existing_user:
            return render_template(
                'signup.html',
                error="Email already registered"
            )

        otp = str(random.randint(100000, 999999))

        session['signup_name'] = name
        session['signup_email'] = email
        session['signup_password'] = password
        session['signup_otp'] = otp

        try:

            msg = Message(
                'AquaFlow OTP Verification',
                sender=app.config['MAIL_USERNAME'],
                recipients=[email]
            )

            msg.body = f"""
Hello {name},

Your AquaFlow verification OTP is:

{otp}

This OTP is valid for 5 minutes.

Thank You,
AquaFlow Team
"""

            send_mail_capped(app, mail, msg, timeout_seconds=6)

            return redirect(url_for('verify_otp'))

        except Exception as e:
            print("MAIL ERROR:", e)

            return render_template(
                'signup.html',
                error="Failed to send OTP"
            )

    return render_template('signup.html')

# ================= VERIFY OTP =================
@app.route('/verify-otp', methods=['GET', 'POST'])
def verify_otp():

    if 'signup_otp' not in session:
        return redirect(url_for('signup'))

    if request.method == 'POST':

        entered_otp = request.form['otp'].strip()
        stored_otp = str(session.get('signup_otp')).strip()

        if entered_otp == stored_otp:

            hashed_password = bcrypt.hashpw(
                session['signup_password'].encode('utf-8'),
                bcrypt.gensalt()
            ).decode('utf-8')

            # ================= PROFESSIONAL USER DATA =================
            user_data = {

                "name": session['signup_name'],
                "email": session['signup_email'],
                "password": hashed_password,

                # PROFILE DETAILS
                "phone": "",
                "gender": "",
                "dob": "",
                "address": "",
                "profile_image": "",

                # ACCOUNT DETAILS
                "created_at": datetime.now(),
                "last_login": datetime.now(),
                "is_verified": True,

                # MEMBERSHIP
                "membership": "Premium",

                # BOOKING STATS
                "total_orders": 0,
                "completed_orders": 0,
                "cancelled_orders": 0,

                # USER PREFERENCES
                "favorite_tanker": "5000L",
                "preferred_water": "Fresh Water"
            }

            users_collection.insert_one(user_data)

            session.pop('signup_name', None)
            session.pop('signup_email', None)
            session.pop('signup_password', None)
            session.pop('signup_otp', None)

            return redirect(url_for('login'))

        else:
            return render_template(
                'verify_otp.html',
                error="Invalid OTP"
            )

    return render_template('verify_otp.html')
# ================= DASHBOARD ================
@app.route("/dashboard")
def dashboard():

    # LOGIN PROTECTION
    if "user_email" not in session:
        return redirect(url_for("login"))

    # CURRENT LOGGED IN USER
    user_email = session["user_email"]

    expire_stale_awaiting_payment_bookings()

    user = users_collection.find_one({
        "email": user_email
    })

    # TOTAL BOOKINGS (an unpaid "Pay Now" attempt that never went
    # through isn't a real booking - excluded here the same way it's
    # excluded from the customer's own history and the admin queue)
    total_bookings = bookings_collection.count_documents({
        "user_email": user_email,
        "status": {"$ne": "Awaiting Payment"}
    })

    # PENDING ORDERS
    pending_orders = bookings_collection.count_documents({
        "user_email": user_email,
        "status": "Pending"
    })

    # DELIVERED ORDERS
    delivered_orders = bookings_collection.count_documents({
        "user_email": user_email,
        "status": "Delivered"
    })

    # ACTIVE DELIVERIES
    active_deliveries = bookings_collection.count_documents({
        "user_email": user_email,
        "status": "On The Way"
    })

    # TODAY DELIVERIES
    today_deliveries = bookings_collection.count_documents({
        "user_email": user_email,
        "status": "Delivered"
    })

    # RECENT BOOKINGS
    recent_bookings = list(
        bookings_collection.find({
            "user_email": user_email,
            "status": {"$ne": "Awaiting Payment"}
        }).sort("created_at", -1).limit(5)
    )

    # LIVE ACTIVITIES
    live_activities = list(
        bookings_collection.find({
            "user_email": user_email,
            "status": {"$ne": "Awaiting Payment"}
        }).sort("created_at", -1).limit(10)
    )

    # TOTAL WATER
    total_water = 0

    all_bookings = bookings_collection.find({
        "user_email": user_email,
        "status": {"$ne": "Awaiting Payment"}
    })

    for booking in all_bookings:

        try:
            quantity = int(
                booking.get("quantity", 0)
            )

            total_water += quantity

        except:
            pass

    # ASSIGNED DRIVERS
    assigned_drivers = bookings_collection.count_documents({
        "user_email": user_email,
        "driver_name": {
            "$ne": ""
        }
    })

    # PAYMENTS
    successful_payments = bookings_collection.count_documents({
        "user_email": user_email,
        "payment_status": "Paid"
    })

    pending_payments = bookings_collection.count_documents({
        "user_email": user_email,
        "payment_status": "Pending"
    })

    # NOTIFICATIONS
    notification_count = bookings_collection.count_documents({
        "user_email": user_email,
        "status": "Pending"
    })

    # LIVE CHART DATA
    chart_labels = []

    chart_values = []

    bookings = bookings_collection.find({
        "user_email": user_email
    }).sort("created_at", 1)

    for booking in bookings:

        chart_labels.append(
            booking.get("delivery_date", "")
        )

        try:

            chart_values.append(
                int(booking.get("quantity", 0))
            )

        except:

            chart_values.append(0)

    return render_template(

        "dashboard.html",

        user=user,

        total_bookings=total_bookings,

        pending_orders=pending_orders,

        delivered_orders=delivered_orders,

        active_deliveries=active_deliveries,

        today_deliveries=today_deliveries,

        recent_bookings=recent_bookings,

        live_activities=live_activities,

        total_water=total_water,

        assigned_drivers=assigned_drivers,

        successful_payments=successful_payments,

        pending_payments=pending_payments,

        notification_count=notification_count,

        chart_labels=chart_labels,

        chart_values=chart_values
    )

#==================payment_system=====================================
@app.route("/payment_system")
def payment_system():
    if 'admin' not in session and 'admin_email' not in session:
        return redirect(url_for('admin_login'))
    return render_template("payment_system.html")

#=================analytics_charts======================================
@app.route("/analytics_charts")
def analytics_charts():
    return render_template("analytics_charts.html")


#==========profile_system===============================================
@app.route("/profile_system")
def profile_system():
    return render_template("profile_system.html")

#=========live_ETA_tracking============================================
@app.route("/admin/live_tracking")
def admin_live_tracking():

    return render_template("live_ETA_tracking.html")
# ================= PROFILE =================
@app.route('/profile')
def profile():

    if 'user_id' not in session:
        return redirect(url_for('home'))

    user = users_collection.find_one({
        "email": session['user_email']
    })

    total_orders = bookings_collection.count_documents({
        "user_email": session['user_email']
    })

    completed_orders = bookings_collection.count_documents({
        "user_email": session['user_email'],
        "status": "Delivered"
    })

    cancelled_orders = bookings_collection.count_documents({
        "user_email": session['user_email'],
        "status": "Cancelled"
    })

    active_orders = bookings_collection.count_documents({
        "user_email": session['user_email'],
        "status": {
            "$in": [
                "Pending",
                "On The Way"
            ]
        }
    })

    # Real lifetime consumption, computed from actual delivered bookings
    # rather than a placeholder figure.
    total_liters = 0
    for b in bookings_collection.find(
        {"user_email": session['user_email'], "status": "Delivered"},
        {"quantity": 1}
    ):
        try:
            total_liters += int(b.get("quantity", 0))
        except (TypeError, ValueError):
            pass

    # The soonest upcoming delivery, if any, so the profile page has
    # something genuinely actionable at a glance instead of only history.
    next_delivery = bookings_collection.find_one(
        {
            "user_email": session['user_email'],
            "status": {"$in": ["Pending", "Assigned", "On The Way"]}
        },
        sort=[("delivery_date", 1)]
    )

    return render_template(
        'profile.html',
        user=user,
        total_orders=total_orders,
        completed_orders=completed_orders,
        cancelled_orders=cancelled_orders,
        active_orders=active_orders,
        total_liters=total_liters,
        next_delivery=next_delivery
    )

# ================= PROFILE PHOTO UPLOAD =================
@app.route('/profile/upload_photo', methods=['POST'])
def upload_profile_photo():

    if 'user_email' not in session:
        return redirect(url_for('login'))

    photo = request.files.get('photo')

    if not photo or photo.filename == '':
        flash("Please choose an image to upload.", "danger")
        return redirect(url_for('profile'))

    allowed_exts = {'.png', '.jpg', '.jpeg', '.webp'}
    ext = os.path.splitext(photo.filename)[1].lower()
    if ext not in allowed_exts:
        flash("Profile photo must be a PNG, JPG, or WEBP image.", "danger")
        return redirect(url_for('profile'))

    os.makedirs("static/uploads/profile_photos", exist_ok=True)

    filename = f"{secure_filename(session['user_email'])}_{int(datetime.now().timestamp())}{ext}"
    photo_path = f"static/uploads/profile_photos/{filename}"

    photo.save(photo_path)
    compress_image_file(photo_path)  # safe no-op on failure

    users_collection.update_one(
        {"email": session['user_email']},
        {"$set": {"profile_image": "/" + photo_path}}
    )

    flash("Profile photo updated.", "success")
    return redirect(url_for('profile'))


# ================= HISTORY =================
@app.route('/history')
def history():

    if 'user_email' not in session:
        return redirect(url_for('login'))

    expire_stale_awaiting_payment_bookings()

    bookings = list(
        bookings_collection.find(
            {"user_email": session['user_email']}
        ).sort("created_at", -1)
    )

    total_bookings = len(bookings)

    return render_template(
        'history.html',
        bookings=bookings,
        total_bookings=total_bookings
    )


# ================= MY PAYMENTS (customer-facing, own data only) =================
# Separate from /payment_system (admin-only, platform-wide monitoring).
# This page shows exactly one customer's own payments - never another
# customer's name, transaction ID, or amount. Bookings are the base
# list (so an unpaid/COD booking that hasn't reached the ledger yet
# still shows up correctly), enriched with the real transaction_id
# from payment_transactions_collection wherever a ledger entry exists.
@app.route('/my-payments')
def my_payments():

    if 'user_email' not in session:
        return redirect(url_for('login'))

    user_email = session['user_email']
    expire_stale_awaiting_payment_bookings()

    bookings = list(
        bookings_collection.find({"user_email": user_email}).sort("created_at", -1)
    )

    # Pull every ledger row for this user in one query, then match it
    # to bookings in Python - cheaper than one query per booking, and
    # keeps payment_transactions_collection as the single source of
    # truth for the real transaction reference shown to the customer.
    ledger_rows = list(
        payment_transactions_collection.find({"user_email": user_email})
    ) if payment_transactions_collection is not None else []
    ledger_by_booking = {}
    for row in ledger_rows:
        bid = row.get("booking_id")
        if not bid:
            continue
        existing = ledger_by_booking.get(bid)
        # Prefer the most recent row per booking (e.g. a Verification
        # Failed attempt followed by a real Paid one).
        if not existing or row.get("created_at", datetime.min) > existing.get("created_at", datetime.min):
            ledger_by_booking[bid] = row

    payments = []
    total_paid_this_month = 0
    month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    for b in bookings:
        bid = str(b["_id"])
        ledger_row = ledger_by_booking.get(bid)
        amount = b.get("pre_coupon_amount") or b.get("delivery_fee", 0)
        method = b.get("payment_method", "Cash on Delivery")

        # Plain-language status - a customer should never see internal
        # ledger vocabulary like "Settled" or "Captured". status_kind
        # drives the badge color in the template - decided HERE, right
        # alongside the label, so a new status can never be added here
        # without also getting a correct color (that's exactly how the
        # earlier bug happened: colors were mapped separately in the
        # template and quietly fell out of sync as labels changed).
        # kinds: green = paid/settled, amber = still pending/waiting,
        # red = failed/rejected/cancelled, gray = needs a human to look
        if b.get("status") == "Awaiting Payment":
            plain_status, can_retry, status_kind = "Payment Pending", True, "amber"
        elif b.get("payment_status") == "Paid (Online)":
            plain_status, can_retry, status_kind = "Paid", False, "green"
        elif b.get("status") == "Cancelled":
            # Covers both a customer-cancelled booking and the 30-minute
            # auto-cancel for an abandoned UPI checkout that was never
            # actually paid (see expire_stale_awaiting_payment_bookings).
            plain_status, can_retry, status_kind = (
                ("Cancelled - Never Paid" if b.get("auto_cancelled_reason") else "Cancelled"),
                False, "red"
            )
        elif b.get("status") == "Rejected":
            plain_status, can_retry, status_kind = "Booking Rejected", False, "red"
        elif b.get("payment_submission_status") == "Rejected":
            plain_status, can_retry, status_kind = "Payment Not Verified", False, "red"
        elif b.get("payment_submission_status") == "Approved":
            # Cash was collected on delivery AND the driver's remittance
            # was verified by admin - this is fully settled, not still
            # owed. Without this check it fell through to the same
            # "Pay on Delivery" label as money genuinely still due,
            # which is exactly backwards.
            plain_status, can_retry, status_kind = "Paid (Cash on Delivery)", False, "green"
        elif b.get("payment_submission_status") in ("Pending Verification", "Waiting Admin Approval"):
            plain_status, can_retry, status_kind = "Cash Submitted - Verifying", False, "amber"
        elif method == "Cash on Delivery":
            # Genuinely still owed: delivery hasn't happened (or cash
            # hasn't been submitted/verified) yet.
            plain_status, can_retry, status_kind = "Not Yet Paid - Due on Delivery", False, "amber"
        elif method == "UPI":
            # A UPI booking that reached here is neither Awaiting
            # Payment, Paid, nor Cancelled - genuinely still waiting on
            # something (e.g. driver not yet assigned to a booking that
            # was actually paid through another path). Be explicit that
            # this isn't a normal COD-style "nothing due yet" state.
            plain_status, can_retry, status_kind = "Payment Status Unclear - Contact Support", False, "gray"
        else:
            plain_status, can_retry, status_kind = "Pending", False, "amber"

        if ledger_row and ledger_row.get("status") in ("Paid", "Settled") and \
                ledger_row.get("created_at", datetime.min) >= month_start:
            total_paid_this_month += ledger_row.get("amount") or 0

        payments.append({
            "booking_id": bid,
            "date": b.get("created_at"),
            "amount": amount,
            "method": method,
            "status": plain_status,
            "status_kind": status_kind,
            "can_retry": can_retry,
            "can_view_invoice": plain_status in ("Paid", "Paid (Cash on Delivery)"),
            "transaction_ref": (ledger_row or {}).get("razorpay_payment_id") or (ledger_row or {}).get("transaction_id"),
        })

    return render_template(
        'my_payments.html',
        payments=payments,
        total_paid_this_month=total_paid_this_month
    )


# ================= CANCEL AN UNPAID "PAY NOW" REQUEST =================
# Lets the customer explicitly walk away from a booking they started
# but decided not to pay for, instead of waiting for the 30-minute
# auto-expiry. Only works on bookings that were never actually paid.
@app.route('/cancel-awaiting-payment/<booking_id>', methods=['POST'])
def cancel_awaiting_payment(booking_id):

    if 'user_email' not in session:
        return redirect(url_for('login'))

    try:
        booking = bookings_collection.find_one({"_id": ObjectId(booking_id)})
    except Exception:
        booking = None

    if not booking or booking.get('user_email') != session['user_email']:
        flash("Booking not found.", "danger")
        return redirect(url_for('history'))

    if booking.get('status') != 'Awaiting Payment':
        flash("This request has already been resolved.", "info")
        return redirect(url_for('history'))

    bookings_collection.update_one(
        {"_id": ObjectId(booking_id)},
        {"$set": {
            "status": "Cancelled",
            "auto_cancelled_reason": "Cancelled by customer before payment"
        }}
    )

    flash("Booking request cancelled - you were never charged.", "success")
    return redirect(url_for('history'))

# ================= BOOKING =================

# ================= LIVE PRICE PREVIEW =================
# Called from booking.html as the customer picks a tanker size, drops
# their pin, or types a coupon code - runs the exact same calculation
# that /booking uses to actually store the price, so nothing shown
# here can drift from what's really charged.
@app.route('/calculate-price', methods=['POST'])
def calculate_price():

    if 'user_email' not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401

    data = request.get_json(silent=True) or {}
    quantity = str(data.get('quantity', '')).strip()
    latitude = data.get('latitude')
    longitude = data.get('longitude')
    coupon_code = (data.get('coupon_code') or '').strip().upper()

    if quantity not in ("1000", "2000", "5000", "10000"):
        return jsonify({"success": False, "message": "Select a valid tanker size"})

    try:
        latitude = float(latitude) if latitude not in (None, '') else None
        longitude = float(longitude) if longitude not in (None, '') else None
    except (TypeError, ValueError):
        latitude = longitude = None

    price = calculate_booking_price(
        quantity, latitude, longitude, settings_collection,
        drivers_collection, bookings_collection
    )

    if not price["deliverable"]:
        return jsonify({
            "success": True,
            "deliverable": False,
            "reason": price["reason"],
            "tanker_cost": price["tanker_cost"],
            "distance_km": price.get("distance_km")
        })

    result = {
        "success": True,
        "deliverable": True,
        "awaiting_location": price["awaiting_location"],
        "tanker_cost": price["tanker_cost"],
        "distance_km": price["distance_km"],
        "nearest_driver_name": price.get("nearest_driver_name"),
        "delivery_fee": price["delivery_fee"],
        "platform_fee": price["platform_fee"],
        "gst_percent": price["gst_percent"],
        "gst": price["gst"],
        "subtotal": price["subtotal"],
        "total": price["total"],
        "coupon_discount": 0,
        "coupon_error": None,
        "final_total": price["total"]
    }

    if coupon_code and not price["awaiting_location"]:
        coupon, discount, coupon_error = validate_coupon(
            coupons_collection, coupon_redemptions_collection,
            coupon_code, session['user_email'], price["total"]
        )
        if coupon_error:
            result["coupon_error"] = coupon_error
        else:
            result["coupon_discount"] = discount
            result["final_total"] = max(0, round(price["total"] - discount, 2))

    return jsonify(result)


@app.route('/booking', methods=['GET', 'POST'])
def booking():

    # =========================
    # LOGIN CHECK
    # =========================

    if 'user_email' not in session:
        return redirect(url_for('login'))

    # Blocked accounts keep their session but lose the ability to book -
    # catches admins blocking someone who's already logged in.
    blocked_response = blocked_user_response()
    if blocked_response:
        return blocked_response

    # =========================
    # BOOKING FORM SUBMIT
    # =========================

    if request.method == 'POST':

        fullname = request.form.get('fullname', '').strip()

        address = request.form.get('address', '').strip()

        quantity = request.form.get('quantity', '').strip()

        delivery_date = request.form.get('delivery_date', '').strip()

        delivery_time = request.form.get('delivery_time', '').strip()

        water_type = request.form.get('water_type', '').strip()

        phone = request.form.get('phone', '').strip()

        latitude_raw = request.form.get('latitude', '').strip()

        longitude_raw = request.form.get('longitude', '').strip()

        payment_method = request.form.get('payment_method', 'cod').strip().lower()
        if payment_method not in ('upi', 'cod'):
            payment_method = 'cod'

        # =========================
        # SERVER-SIDE VALIDATION
        # (backstop behind the frontend JS validation — never trust
        # the client alone)
        # =========================

        errors = []

        if not fullname or len(fullname) < 2:
            errors.append("Please enter a valid full name")

        if not phone or not re.fullmatch(r"[6-9]\d{9}", phone):
            errors.append("Please enter a valid 10-digit phone number")

        if not address or len(address) < 5:
            errors.append("Please enter a complete delivery address")

        if not delivery_date:
            errors.append("Please select a delivery date")
        else:
            try:
                parsed_delivery_date = datetime.strptime(delivery_date, "%Y-%m-%d").date()
                today_date = datetime.now().date()
                if parsed_delivery_date < today_date:
                    errors.append("Delivery date cannot be in the past")
                elif parsed_delivery_date > today_date + timedelta(days=15):
                    errors.append("Delivery date can be at most 15 days from today")
            except ValueError:
                errors.append("Invalid delivery date")

        if not delivery_time:
            errors.append("Please select a preferred delivery time")

        # =========================
        # DELIVERY LOCATION (from the map picker)
        # =========================

        latitude = None
        longitude = None

        try:
            if latitude_raw and longitude_raw:
                latitude = float(latitude_raw)
                longitude = float(longitude_raw)
        except ValueError:
            latitude = None
            longitude = None

        if latitude is None or longitude is None:
            errors.append("Please pin your delivery location on the map")

        # =========================
        # PRICING (Feature: real pricing engine)
        # Tanker Cost + real distance-based Delivery Fee (from the
        # customer's actual pinned lat/lng) + Platform Fee + GST -
        # computed server-side so this is the authoritative number,
        # not something trusted from the browser. The exact same
        # function backs the live preview on booking.html, so what the
        # customer saw while filling the form matches what's charged.
        # =========================

        if quantity not in ("1000", "2000", "5000", "10000"):
            errors.append("Please select a valid water quantity")

        if errors:
            flash(" | ".join(errors), "danger")
            return render_template('booking.html')

        price = calculate_booking_price(
            quantity, latitude, longitude, settings_collection,
            drivers_collection, bookings_collection
        )

        if not price["deliverable"]:
            flash(price["reason"], "danger")
            return render_template('booking.html')

        delivery_fee = price["total"]

        price_breakdown = {
            "water_charge": price["tanker_cost"],
            "distance_charge": price["delivery_fee"],
            "emergency_charge": 0,
            "platform_fee": price["platform_fee"],
            "gst": price["gst"],
            "gst_percent": price["gst_percent"],
            "discount": 0,
            "nearest_driver_name": price.get("nearest_driver_name")
        }

        # =========================
        # OPTIONAL COUPON (applied directly on the booking page)
        # =========================

        coupon_code = request.form.get('coupon_code', '').strip().upper()
        applied_coupon = None
        pre_coupon_amount = None

        if coupon_code:
            coupon, discount, coupon_error = validate_coupon(
                coupons_collection, coupon_redemptions_collection,
                coupon_code, session['user_email'], delivery_fee
            )
            if coupon_error:
                # Don't fail the whole booking over a bad coupon code -
                # just skip it silently; the customer already saw the
                # same validation live on the booking page before
                # submitting, so this should be rare (e.g. someone else
                # used a one-time code in another tab in the meantime).
                flash(f"Coupon note: {coupon_error} - booking created without it.", "warning")
            else:
                pre_coupon_amount = delivery_fee
                delivery_fee = max(0, round(delivery_fee - discount, 2))
                applied_coupon = {
                    "code": coupon_code,
                    "discount_type": coupon["discount_type"],
                    "value": coupon["value"],
                    "discount": discount
                }

        # =========================
        # BOOKING DATA
        # =========================

        booking_data = {

            "fullname": fullname,

            "phone": phone,

            "address": address,

            "latitude": latitude,

            "longitude": longitude,

            "water_type": water_type if water_type else "Fresh Water",

            "quantity": quantity,

            "delivery_fee": delivery_fee,

            "price_breakdown": price_breakdown,

            "delivery_date": delivery_date,

            "delivery_time": delivery_time,

            # Bookings for today go straight to the main admin screen.
            # Future-dated bookings are held back and flagged unseen
            # until the admin checks the future-orders dropdown for
            # that date (see booking_management.py).
            "admin_seen_future": delivery_date == datetime.now().strftime("%Y-%m-%d"),

            "user_email": session['user_email'],

            # =========================
            # LIVE STATUS
            # =========================
            # "Pay Now" bookings start as "Awaiting Payment" - NOT a
            # real, confirmed booking yet. They're excluded from the
            # admin approval queue and clearly marked (with a
            # Complete Payment / Cancel option) on the customer's own
            # booking history, and auto-cancelled if abandoned for too
            # long (see _expire_stale_awaiting_payment_bookings below).
            # Only once payment actually succeeds does this become a
            # real "Pending" booking. Cash On Delivery is a genuine
            # commitment immediately, so it still starts as "Pending".

            "status": "Awaiting Payment" if payment_method == "upi" else "Pending",

            # =========================
            # DRIVER DETAILS
            # =========================

            "driver_name": "Not Assigned",
"driver_phone": "Not Available",
"truck_number": "Not Assigned",
"live_location": "Waiting For Approval",
"ETA": "Not Available",

            # =========================
            # PAYMENT
            # =========================

            "payment_status": "Pending",

            "payment_method": "UPI" if payment_method == "upi" else "Cash on Delivery",

            # =========================
            # TIME
            # =========================

            "created_at": datetime.now()

        }

        if applied_coupon:
            booking_data["coupon"] = applied_coupon
            booking_data["pre_coupon_amount"] = pre_coupon_amount

        # =========================
        # SAVE TO MONGODB
        # =========================

        result = bookings_collection.insert_one(
            booking_data
        )

        if applied_coupon:
            record_redemption(
                coupon_redemptions_collection,
                session['user_email'],
                applied_coupon["code"],
                str(result.inserted_id)
            )

        if payment_method == "upi":
            return redirect(
                url_for('payment_page', booking_id=str(result.inserted_id))
            )

        return redirect(
            url_for('history')
        )

    return render_template(
        'booking.html'
    )


# ================= PAYMENT PAGE (Feature 3) =================
# Shown right after booking creation for "Pay Now" bookings — full
# summary + coupon field + payment method buttons, instead of jumping
# the customer straight into a gateway with no way back. This is also
# where a cancelled/dismissed Razorpay checkout sends the customer
# back to, so they can retry or switch to Cash on Delivery instead of
# being left stranded on the gateway page.
@app.route('/payment/<booking_id>')
def payment_page(booking_id):

    if 'user_email' not in session:
        return redirect(url_for('login'))

    try:
        booking_doc = bookings_collection.find_one({"_id": ObjectId(booking_id)})
    except Exception:
        booking_doc = None

    if not booking_doc or booking_doc.get('user_email') != session['user_email']:
        flash("Booking not found.", "danger")
        return redirect(url_for('history'))

    # Already paid online? Nothing to do here — send them to history.
    if booking_doc.get('payment_status') == 'Paid (Online)':
        flash("This booking is already paid.", "info")
        return redirect(url_for('history'))

    if booking_doc.get('status') == 'Cancelled':
        flash("This booking request was cancelled and can no longer be paid for. Please create a new booking.", "danger")
        return redirect(url_for('history'))

    user_doc = users_collection.find_one({"email": session['user_email']})
    wallet_balance = user_doc.get('wallet_balance', 0) if user_doc else 0

    return render_template(
        'payment_page.html',
        booking=booking_doc,
        booking_id=booking_id,
        amount=booking_doc.get('delivery_fee', 0),
        breakdown=booking_doc.get('price_breakdown'),
        coupon=booking_doc.get('coupon'),
        wallet_balance=wallet_balance,
        wallet_used=booking_doc.get('wallet_used', 0),
        gateway_available=razorpay_configured()
    )


# ================= SWITCH TO CASH ON DELIVERY =================
# Lets the customer back out of online payment (e.g. after cancelling
# at the gateway) and confirm the booking as Cash on Delivery instead,
# without having to re-fill the whole booking form.
@app.route('/switch-to-cod/<booking_id>', methods=['POST'])
def switch_to_cod(booking_id):

    if 'user_email' not in session:
        return redirect(url_for('login'))

    try:
        booking_doc = bookings_collection.find_one({"_id": ObjectId(booking_id)})
    except Exception:
        booking_doc = None

    if not booking_doc or booking_doc.get('user_email') != session['user_email']:
        flash("Booking not found.", "danger")
        return redirect(url_for('history'))

    if booking_doc.get('payment_status') == 'Paid (Online)':
        flash("This booking is already paid online.", "info")
        return redirect(url_for('history'))

    bookings_collection.update_one(
        {"_id": ObjectId(booking_id)},
        {"$set": {
            "payment_method": "Cash on Delivery",
            # Choosing COD is a genuine commitment - confirm the
            # booking now, same as if they'd picked COD from the start.
            "status": "Pending"
        }}
    )

    flash("Payment method switched to Cash on Delivery - your booking is confirmed.", "success")
    return redirect(url_for('history'))


# ================= FAKE PAYMENT GATEWAY (Feature 5) =================
# This project doesn't have a live Razorpay merchant account, so when
# real Razorpay isn't configured, this simulated gateway is used
# instead: "Payment Processing..." then "Payment Successful" with a
# generated Transaction ID. Structured exactly like a real gateway
# result (same fields real Razorpay verification writes) so every
# downstream feature - driver assignment, invoices, driver wallet,
# admin finance dashboard - can't tell the difference.
@app.route('/fake-payment/<booking_id>')
def fake_payment(booking_id):

    if 'user_email' not in session:
        return redirect(url_for('login'))

    try:
        booking_doc = bookings_collection.find_one({"_id": ObjectId(booking_id)})
    except Exception:
        booking_doc = None

    if not booking_doc or booking_doc.get('user_email') != session['user_email']:
        flash("Booking not found.", "danger")
        return redirect(url_for('history'))

    if booking_doc.get('payment_status') == 'Paid (Online)':
        flash("This booking is already paid.", "info")
        return redirect(url_for('history'))

    if booking_doc.get('status') == 'Cancelled':
        flash("This booking request was cancelled and can no longer be paid for. Please create a new booking.", "danger")
        return redirect(url_for('history'))

    if booking_doc.get('delivery_fee', 0) <= 0:
        flash("This booking is fully covered by wallet/coupon - use the Confirm Booking button instead.", "info")
        return redirect(url_for('payment_page', booking_id=booking_id))

    method = request.args.get('method', 'upi').strip().lower()
    if method not in ('upi', 'card', 'netbanking'):
        method = 'upi'

    return render_template(
        'fake_payment_gateway.html',
        booking_id=booking_id,
        amount=booking_doc.get('delivery_fee', 0),
        method=method
    )


@app.route('/fake-payment-complete/<booking_id>', methods=['POST'])
def fake_payment_complete(booking_id):

    if 'user_email' not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401

    try:
        booking_doc = bookings_collection.find_one({"_id": ObjectId(booking_id)})
    except Exception:
        booking_doc = None

    if not booking_doc or booking_doc.get('user_email') != session['user_email']:
        return jsonify({"success": False, "message": "Booking not found"}), 404

    # Already paid (e.g. a duplicate click) - just hand back the
    # existing transaction id instead of generating a second one.
    if booking_doc.get('payment_status') == 'Paid (Online)':
        return jsonify({
            "success": True,
            "transaction_id": booking_doc.get('payment_id', ''),
            "already_paid": True
        })

    data = request.get_json(silent=True) or {}
    method = data.get('method', 'upi').strip().lower()
    method_labels = {"upi": "UPI", "card": "Card", "netbanking": "Net Banking"}
    method_label = method_labels.get(method, "UPI")

    transaction_id = "TXN" + str(random.randint(10000000, 99999999))
    payment_time = datetime.now()
    amount = booking_doc.get('delivery_fee', 0)

    bookings_collection.update_one(
        {"_id": ObjectId(booking_id)},
        {"$set": {
            "payment_status": "Paid (Online)",
            "payment_processed": True,
            "payment_method": method_label,
            "payment_id": transaction_id,
            "payment_time": payment_time,
            "paid_at": payment_time,
            # Payment succeeded - this is now a real, confirmed booking
            # that enters the normal admin-approval pipeline.
            "status": "Pending"
        }}
    )

    if transactions_collection is not None:
        transactions_collection.insert_one({
            "type": "customer_payment",
            "transaction_id": transaction_id,
            "booking_id": str(booking_id),
            "user_email": session['user_email'],
            "amount": amount,
            "payment_method": method_label,
            "status": "Completed",
            "created_at": payment_time
        })

    notifications_collection.insert_one({
        "user_email": session['user_email'],
        "title": "Payment Successful",
        "message": f"Your payment of ₹{amount:,.0f} was successful. Your invoice is ready to view.",
        "type": "payment_successful",
        "status": "success",
        "icon": "✅",
        "read": False,
        "created_at": payment_time
    })

    return jsonify({
        "success": True,
        "transaction_id": transaction_id,
        "already_paid": False
    })


# ================= PAYMENT RECEIPT / INVOICE (Feature 6) =================

def _get_or_create_invoice_number(booking_doc, booking_id):
    """
    Every booking gets exactly one invoice number, generated the first
    time its invoice is opened and cached on the booking from then on -
    so it never changes on repeat views. Sequential per calendar year:
    INV-2026-000001, INV-2026-000002, ...
    """

    existing = booking_doc.get("invoice_number")
    if existing:
        return existing

    year = datetime.now().year

    counter = counters_collection.find_one_and_update(
        {"_id": f"invoice_{year}"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=pymongo.ReturnDocument.AFTER
    )

    invoice_number = f"INV-{year}-{counter['seq']:06d}"

    bookings_collection.update_one(
        {"_id": ObjectId(booking_id)},
        {"$set": {"invoice_number": invoice_number}}
    )

    return invoice_number


def _build_invoice_timeline(booking_doc):
    """
    Builds the Order Timeline in the order things actually happened,
    not a hardcoded guess - because when payment lands in that sequence
    genuinely varies:
      - online payments are usually paid before admin even approves
      - Cash on Delivery is only collected + verified AFTER delivery
    Steps that already have a timestamp are sorted earliest-first.
    Steps that haven't happened yet keep the natural fallback order
    so the "next up" step is easy to spot.
    """

    if booking_doc.get("payment_method") == "Cash on Delivery":
        payment_stage = ("Cash Collected & Verified", booking_doc.get("payment_verified_at"))
    else:
        payment_stage = ("Payment Successful", booking_doc.get("payment_time") or booking_doc.get("paid_at"))

    all_stages = [
        ("Booking Created", booking_doc.get("created_at")),
        ("Admin Approved", booking_doc.get("approved_at")),
        payment_stage,
        ("Driver Assigned", booking_doc.get("driver_assigned_at")),
        ("Driver Started", booking_doc.get("started_at")),
        ("OTP Verified", booking_doc.get("otp_verified_at")),
        ("Delivered", booking_doc.get("delivered_at")),
    ]

    completed = sorted(
        (s for s in all_stages if s[1]),
        key=lambda s: s[1]
    )
    upcoming = [s for s in all_stages if not s[1]]

    return completed + upcoming


@app.route('/invoice/<booking_id>')
def view_invoice(booking_id):

    if 'user_email' not in session and 'admin' not in session:
        return redirect(url_for('login'))

    try:
        booking_doc = bookings_collection.find_one({"_id": ObjectId(booking_id)})
    except Exception:
        booking_doc = None

    if not booking_doc:
        flash("Booking not found.", "danger")
        return redirect(url_for('history'))

    is_owner = booking_doc.get('user_email') == session.get('user_email')
    is_admin = 'admin' in session
    if not (is_owner or is_admin):
        flash("You don't have access to this invoice.", "danger")
        return redirect(url_for('history'))

    amount = booking_doc.get('delivery_fee', 0)
    # Amount charged is treated as GST-inclusive (18%) - this is purely
    # an invoice breakdown, it does NOT change anything already charged.
    gst_amount = round(amount * 18 / 118, 2)
    base_amount = round(amount - gst_amount, 2)

    invoice_number = _get_or_create_invoice_number(booking_doc, booking_id)
    timeline_stages = _build_invoice_timeline(booking_doc)

    return render_template(
        'invoice.html',
        booking=booking_doc,
        booking_id=booking_id,
        invoice_number=invoice_number,
        timeline_stages=timeline_stages,
        amount=amount,
        base_amount=base_amount,
        gst_amount=gst_amount
    )


# ================= TRANSACTION TABLE (Feature 22) =================
# Unified ledger view across every type of money movement this app
# tracks: driver settlements and wallet activity, all already stored in
# transactions_collection, just not visible in one place until now.
@app.route('/transaction-table')
def transaction_table():

    if 'admin' not in session:
        return redirect(url_for('admin_login'))

    txn_type = request.args.get('type', 'all')
    query = {}
    if txn_type != 'all':
        query['type'] = txn_type

    transactions = list(
        transactions_collection.find(query).sort("created_at", -1).limit(200)
    ) if transactions_collection is not None else []

    return render_template(
        'transaction_table.html',
        transactions=transactions,
        active_type=txn_type,
        admin_name="Admin",
        admin_email=session.get('admin_email', 'admin@aquaflow.com')
    )


# ================= USER WALLET (Feature 17) =================
@app.route('/wallet')
def user_wallet():

    if 'user_email' not in session:
        return redirect(url_for('login'))

    user = users_collection.find_one({"email": session['user_email']})
    wallet_balance = user.get('wallet_balance', 0) if user else 0

    history = list(
        wallet_transactions_collection.find({"user_email": session['user_email']})
        .sort("created_at", -1)
        .limit(50)
    )

    # Lifetime totals for the stats strip - summed over every
    # transaction on record, not just the 50 shown in the table.
    all_txns = list(wallet_transactions_collection.find({"user_email": session['user_email']}))
    total_recharged = sum(t.get('amount', 0) for t in all_txns if t.get('type') == 'recharge')
    total_spent = sum(-t.get('amount', 0) for t in all_txns if t.get('type') == 'booking_payment')
    total_refunded = sum(t.get('amount', 0) for t in all_txns if t.get('type') == 'refund_to_wallet')

    return render_template(
        'wallet.html',
        wallet_balance=wallet_balance,
        history=history,
        total_recharged=total_recharged,
        total_spent=total_spent,
        total_refunded=total_refunded,
        razorpay_ready=razorpay_configured()
    )


@app.route('/wallet-receipt/<razorpay_payment_id>')
def wallet_receipt(razorpay_payment_id):

    if 'user_email' not in session:
        return redirect(url_for('login'))

    topup = wallet_topups_collection.find_one({
        "razorpay_payment_id": razorpay_payment_id,
        "user_email": session['user_email']
    })

    if not topup:
        flash("Receipt not found.", "danger")
        return redirect(url_for('user_wallet'))

    return render_template('wallet_receipt.html', topup=topup)


@app.route('/apply-wallet/<booking_id>', methods=['POST'])
def apply_wallet(booking_id):

    if 'user_email' not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401

    try:
        booking = bookings_collection.find_one({"_id": ObjectId(booking_id)})
    except Exception:
        booking = None

    if not booking or booking.get('user_email') != session['user_email']:
        return jsonify({"success": False, "message": "Booking not found"}), 404

    if booking.get('payment_status') == 'Paid (Online)':
        return jsonify({"success": False, "message": "This booking has already been paid for"})

    if booking.get('wallet_used'):
        return jsonify({"success": False, "message": "Wallet is already applied to this booking"})

    user = users_collection.find_one({"email": session['user_email']})
    wallet_balance = user.get('wallet_balance', 0) if user else 0

    if wallet_balance <= 0:
        return jsonify({"success": False, "message": "Your wallet balance is ₹0"})

    current_amount = booking.get('delivery_fee', 0)
    use_amount = min(wallet_balance, current_amount)
    new_amount = round(current_amount - use_amount, 2)

    # Debit immediately - this money is now committed to this booking.
    # It's only ever returned to the wallet if the booking is later
    # cancelled (see cancel_booking) or the customer removes it below.
    users_collection.update_one(
        {"email": session['user_email']},
        {"$inc": {"wallet_balance": -use_amount}}
    )

    bookings_collection.update_one(
        {"_id": ObjectId(booking_id)},
        {"$set": {
            "delivery_fee": new_amount,
            "wallet_used": use_amount
        }}
    )

    wallet_transactions_collection.insert_one({
        "user_email": session['user_email'],
        "type": "booking_payment",
        "booking_id": str(booking_id),
        "amount": -use_amount,
        "created_at": datetime.now()
    })

    return jsonify({
        "success": True,
        "message": f"₹{use_amount:,.0f} applied from your wallet",
        "used": use_amount,
        "new_amount": new_amount,
        "fully_paid": new_amount <= 0
    })


@app.route('/remove-wallet/<booking_id>', methods=['POST'])
def remove_wallet(booking_id):

    if 'user_email' not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401

    try:
        booking = bookings_collection.find_one({"_id": ObjectId(booking_id)})
    except Exception:
        booking = None

    if not booking or booking.get('user_email') != session['user_email']:
        return jsonify({"success": False, "message": "Booking not found"}), 404

    used = booking.get('wallet_used', 0)
    if not used:
        return jsonify({"success": False, "message": "No wallet amount is applied on this booking"})

    users_collection.update_one(
        {"email": session['user_email']},
        {"$inc": {"wallet_balance": used}}
    )

    new_amount = round(booking.get('delivery_fee', 0) + used, 2)

    bookings_collection.update_one(
        {"_id": ObjectId(booking_id)},
        {"$set": {"delivery_fee": new_amount}, "$unset": {"wallet_used": ""}}
    )

    wallet_transactions_collection.insert_one({
        "user_email": session['user_email'],
        "type": "refund_to_wallet",
        "booking_id": str(booking_id),
        "amount": used,
        "created_at": datetime.now()
    })

    return jsonify({"success": True, "message": "Wallet amount removed", "new_amount": new_amount})


@app.route('/wallet-full-payment/<booking_id>', methods=['POST'])
def wallet_full_payment(booking_id):
    """Closes out a booking whose amount due has reached Rs0 - whether
    that's from wallet balance, a coupon, or both combined - without
    needing to send a Rs0 charge through a payment gateway."""

    if 'user_email' not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401

    try:
        booking = bookings_collection.find_one({"_id": ObjectId(booking_id)})
    except Exception:
        booking = None

    if not booking or booking.get('user_email') != session['user_email']:
        return jsonify({"success": False, "message": "Booking not found"}), 404

    if booking.get('payment_status') == 'Paid (Online)':
        return jsonify({"success": True, "already_paid": True})

    if booking.get('delivery_fee', 0) > 0:
        return jsonify({"success": False, "message": "This booking isn't fully covered yet"})

    # Attribute the Rs0 close-out to whatever actually covered it.
    used_wallet = bool(booking.get('wallet_used'))
    used_coupon = bool(booking.get('coupon'))
    if used_wallet and used_coupon:
        method_label = "Wallet + Coupon"
    elif used_wallet:
        method_label = "Wallet"
    elif used_coupon:
        method_label = "Coupon"
    else:
        method_label = "Free"

    payment_time = datetime.now()
    bookings_collection.update_one(
        {"_id": ObjectId(booking_id)},
        {"$set": {
            "payment_status": "Paid (Online)",
            "payment_processed": True,
            "payment_method": method_label,
            "payment_id": "NOCHARGE" + str(random.randint(10000000, 99999999)),
            "payment_time": payment_time,
            "paid_at": payment_time,
            "status": "Pending"
        }}
    )

    original_amount = booking.get('pre_coupon_amount') or booking.get('wallet_used') or 0

    log_transaction(
        payment_transactions_collection,
        type="booking_payment",
        status="Paid",
        amount=original_amount,
        booking_id=booking_id,
        user_email=session['user_email'],
        driver_email=booking.get("driver_email"),
        gateway=method_label.lower().replace(" + ", "_and_").replace(" ", "_"),
        gst=booking.get("gst"),
        notes=f"Closed out with no charge via {method_label}"
    )

    notifications_collection.insert_one({
        "user_email": session['user_email'],
        "title": "Payment Successful",
        "message": f"Your booking (₹{original_amount:,.0f}) was fully covered by {method_label} - nothing more to pay. Your invoice is ready to view.",
        "type": "payment_successful",
        "status": "success",
        "icon": "✅",
        "read": False,
        "created_at": payment_time
    })

    return jsonify({"success": True, "already_paid": False})


# ================= UPI PAY REDIRECT =================
# Sends the customer straight into their UPI app with the payee
# (company) UPI ID and the exact amount pre-filled — the customer
# never sees the admin's bank details and never types an amount.
@app.route('/pay-upi/<booking_id>')
def pay_upi(booking_id):

    if 'user_email' not in session:
        return redirect(url_for('login'))

    try:
        booking_doc = bookings_collection.find_one({"_id": ObjectId(booking_id)})
    except Exception:
        booking_doc = None

    if not booking_doc or booking_doc.get('user_email') != session['user_email']:
        flash("Booking not found", "danger")
        return redirect(url_for('history'))

    bank_details = get_bank_details(settings_collection)
    upi_id = bank_details.get('upi_id')

    if not upi_id or upi_id == 'Not set up yet':
        # Admin hasn't configured a UPI ID yet — fall back to COD
        # rather than sending the customer to a dead link.
        flash("Online payment isn't set up yet — please pay the driver by cash on delivery.", "warning")
        return redirect(url_for('history'))

    amount = booking_doc.get('delivery_fee', 0)
    note = f"AquaFlow booking {str(booking_doc['_id'])[-6:]}"

    upi_link = (
        f"upi://pay?pa={quote(upi_id)}"
        f"&pn={quote('AquaFlow')}"
        f"&am={quote(str(amount))}"
        f"&cu=INR"
        f"&tn={quote(note)}"
    )

    return render_template(
        'pay_upi_redirect.html',
        upi_link=upi_link,
        amount=amount,
        booking_id=str(booking_doc['_id'])
    )


# ================= CUSTOMER CONFIRMS UPI PAYMENT =================
# Customer self-reports the transaction ID after paying via the UPI
# deep link. This does NOT mark the booking as verified-paid — a real
# gateway (Razorpay/Cashfree/etc.) would confirm that automatically
# via webhook; without one, admin still verifies it manually, same as
# the existing driver cash-deposit flow.
@app.route('/confirm-customer-payment/<booking_id>', methods=['POST'])
def confirm_customer_payment(booking_id):

    if 'user_email' not in session:
        return redirect(url_for('login'))

    transaction_id = request.form.get('transaction_id', '').strip()

    if not transaction_id:
        flash("Please enter your UPI transaction ID", "danger")
        return redirect(url_for('pay_upi', booking_id=booking_id))

    try:
        booking_doc = bookings_collection.find_one({"_id": ObjectId(booking_id)})
    except Exception:
        booking_doc = None

    if not booking_doc or booking_doc.get('user_email') != session['user_email']:
        flash("Booking not found", "danger")
        return redirect(url_for('history'))

    bookings_collection.update_one(
        {"_id": ObjectId(booking_id)},
        {"$set": {
            "payment_status": "Submitted - Pending Verification",
            "customer_transaction_id": transaction_id,
            "customer_payment_submitted_at": datetime.now()
        }}
    )

    flash("Payment submitted! Our team will verify it shortly.", "success")
    return redirect(url_for('history'))


# ================= FORGOT PASSWORD =================
@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():

    if request.method == 'POST':

        email = request.form['email'].strip().lower()

        if not email:
            return render_template(
                'forgot_password.html',
                error="Please enter email"
            )

        user = users_collection.find_one({
            "email": {
                "$regex": f"^{re.escape(email)}$",
                "$options": "i"
            }
        })

        if not user:
            return render_template(
                'forgot_password.html',
                error="Email not registered"
            )

        otp = str(random.randint(100000, 999999))

        session['reset_email'] = user['email']
        session['reset_otp'] = otp

        try:

            msg = Message(
                'AquaFlow Password Reset OTP',
                sender=app.config['MAIL_USERNAME'],
                recipients=[email]
            )

            msg.body = f"""
Hello {user['name']},

Your AquaFlow password reset OTP is:

{otp}

This OTP is valid for 5 minutes.

Thank You,
AquaFlow Team
"""

            send_mail_capped(app, mail, msg, timeout_seconds=6)

            return redirect(url_for('reset_password_otp'))

        except Exception as e:
            print("MAIL ERROR:", e)

            return render_template(
                'forgot_password.html',
                error="Failed to send OTP"
            )

    return render_template('forgot_password.html')

# ================= RESET PASSWORD =================
@app.route('/reset-password-otp', methods=['GET', 'POST'])
def reset_password_otp():

    if 'reset_otp' not in session:
        return redirect(url_for('forgot_password'))

    if request.method == 'POST':

        entered_otp = request.form['otp'].strip()
        new_password = request.form['new_password'].strip()
        confirm_password = request.form['confirm_password'].strip()

        stored_otp = str(session.get('reset_otp')).strip()

        if entered_otp != stored_otp:
            return render_template(
                'reset_password_otp.html',
                error="Invalid OTP"
            )

        if new_password != confirm_password:
            return render_template(
                'reset_password_otp.html',
                error="Passwords do not match"
            )

        if len(new_password) < 6:
            return render_template(
                'reset_password_otp.html',
                error="Password must be at least 6 characters"
            )

        hashed_password = bcrypt.hashpw(
            new_password.encode('utf-8'),
            bcrypt.gensalt()
        ).decode('utf-8')

        users_collection.update_one(
            {
                "email": session['reset_email']
            },
            {
                "$set": {
                    "password": hashed_password
                }
            }
        )

        session.pop('reset_email', None)
        session.pop('reset_otp', None)

        return redirect(url_for('login'))

    return render_template('reset_password_otp.html')

# ================= LOGOUT =================
@app.route('/logout')
def logout():

    session.clear()

    return redirect(url_for('home'))

# ================= EDIT PROFILE =================
@app.route('/edit_profile', methods=['GET', 'POST'])
def edit_profile():

    if 'user_email' not in session:
        return redirect(url_for('login'))

    user = users_collection.find_one({
        "email": session['user_email']
    })

    if request.method == 'POST':

        phone = request.form.get('phone')
        gender = request.form.get('gender')
        dob = request.form.get('dob')
        address = request.form.get('address')
        favorite_tanker = request.form.get('favorite_tanker')
        preferred_water = request.form.get('preferred_water')

        users_collection.update_one(
            {"email": session['user_email']},
            {
                "$set": {
                    "phone": phone,
                    "gender": gender,
                    "dob": dob,
                    "address": address,
                    "favorite_tanker": favorite_tanker,
                    "preferred_water": preferred_water
                }
            }
        )

        return redirect(url_for('dashboard'))

    return render_template(
        'edit_profile.html',
        user=user
    )

# ================= ADMIN LOGIN =================
@app.route('/admin-login', methods=['GET', 'POST'])
def admin_login():

    # ALREADY LOGGED IN -> SKIP LOGIN PAGE ENTIRELY
    if request.method == 'GET' and session.get('admin'):
        return redirect(url_for('admin_dashboard'))

    if request.method == 'POST':

        email = request.form['email']
        password = request.form['password']

        admin = admins_collection.find_one({
            "email": email,
            "password": password
        })

        if admin:

            session['admin'] = True
            session['admin_email'] = admin['email']  

            return redirect(url_for('admin_dashboard'))

        else:

            return render_template(
                'admin_login.html',
                error="Invalid admin credentials"
            )

    return render_template('admin_login.html')

# ================= API STATS WITH ADMIN CHECK =================
@app.route('/api/stats')
def api_stats():

    # SECURITY CHECK - Admin only
    if 'admin' not in session:
        return jsonify({
            "success": False,
            "message": "Unauthorized"
        })

    total_users = users_collection.count_documents({})
    total_orders = bookings_collection.count_documents({})

    pending_orders = bookings_collection.count_documents({
        "status": "Pending"
    })

    approved_orders = bookings_collection.count_documents({
        "status": "Approved"
    })

    cancelled_orders = bookings_collection.count_documents({
        "status": "Cancelled"
    })

    delivered_orders = bookings_collection.count_documents({
        "status": "Delivered"
    })

    today_orders = bookings_collection.count_documents({
        "created_at": {
            "$gte": datetime.now().replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0
            )
        }
    })

    total_revenue = 0

    bookings = bookings_collection.find()

    for booking in bookings:

        try:
            quantity = int(
                str(booking.get("quantity", "0"))
                .replace("Liters", "")
                .replace("L", "")
                .strip()
            )

            total_revenue += quantity * 12.5

        except:
            pass

    active_deliveries = bookings_collection.count_documents({
        "status": "Approved"
    })

    finance = get_finance_overview(bookings_collection, drivers_collection, transactions_collection)
    ledger_overview = get_ledger_overview(payment_transactions_collection)

    return jsonify({
        "total_users": total_users,
        "total_orders": total_orders,
        "pending_orders": pending_orders,
        "approved_orders": approved_orders,
        "cancelled_orders": cancelled_orders,
        "delivered_orders": delivered_orders,
        "today_orders": today_orders,
        "total_revenue": finance["total_revenue"],
        "today_revenue": finance["today_revenue"],
        "today_commission": finance["today_commission"],
        "pending_driver_deposits": finance["pending_driver_deposits"],
        "pending_driver_payouts": finance["pending_driver_payouts"],
        "gst_collected": ledger_overview["gst_collected"],
        "total_withdrawals": ledger_overview["total_withdrawals"],
        "today_withdrawals": ledger_overview["today_withdrawals"],
        "cod_total": ledger_overview["cod_total"],
        "online_total": ledger_overview["online_total"],
        "active_deliveries": active_deliveries
    })

# ================= ADVANCED ADMIN DASHBOARD =================
@app.route('/admin-dashboard')
def admin_dashboard():

    if 'admin' not in session:
        return redirect(url_for('admin_login'))

    bookings = list(
        bookings_collection.find().sort("created_at", -1)
    )

    total_users = users_collection.count_documents({})

    total_orders = bookings_collection.count_documents({})

    pending_orders = bookings_collection.count_documents({
        "status": "Pending"
    })

    approved_orders = bookings_collection.count_documents({
        "status": "Approved"
    })

    cancelled_orders = bookings_collection.count_documents({
        "status": "Cancelled"
    })

    delivered_orders = bookings_collection.count_documents({
        "status": "Delivered"
    })

    today_orders = bookings_collection.count_documents({
        "created_at": {
            "$gte": datetime.now().replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0
            )
        }
    })

    # =========================
    # FINANCE OVERVIEW
    # =========================

    finance = get_finance_overview(bookings_collection, drivers_collection, transactions_collection)
    ledger_overview = get_ledger_overview(payment_transactions_collection)
    top_drivers = get_top_drivers(bookings_collection, drivers_collection)
    top_customers = get_top_customers(bookings_collection)

    active_deliveries = bookings_collection.count_documents({
        "status": "Approved"
    })

    # Real last-7-days booking counts (was hardcoded placeholder data)
    week_days = [(datetime.now() - timedelta(days=i)) for i in range(6, -1, -1)]
    weekly_chart_data = []
    weekly_revenue_data = []
    for day in week_days:
        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        weekly_chart_data.append(
            bookings_collection.count_documents({
                "created_at": {"$gte": day_start, "$lt": day_end}
            })
        )
        day_delivered = bookings_collection.find({
            "status": "Delivered",
            "delivered_at": {"$gte": day_start, "$lt": day_end}
        })
        weekly_revenue_data.append(
            sum(o.get("delivery_fee", 500) for o in day_delivered)
        )

    return render_template(
        'admin_dashboard.html',
        bookings=bookings,
        stats={
            "total_users": total_users,
            "total_orders": total_orders,
            "pending_orders": pending_orders,
            "approved_orders": approved_orders,
            "cancelled_orders": cancelled_orders,
            "delivered_orders": delivered_orders,
            "today_orders": today_orders,
            "total_revenue": finance["total_revenue"],
            "active_deliveries": active_deliveries
        },
        finance=finance,
        ledger_overview=ledger_overview,
        top_drivers=top_drivers,
        top_customers=top_customers,
        weekly_chart_data=weekly_chart_data,
        weekly_revenue_data=weekly_revenue_data,
        admin_name="Admin",
        admin_email=session.get('admin_email', 'admin@aquaflow.com')
    )

# ================= ADMIN APPROVE BOOKING =================
@app.route('/approve-booking/<booking_id>')
def approve_booking(booking_id):

    # =========================================
    # SECURITY CHECK - ADMIN ONLY
    # =========================================

    if 'admin' not in session:
        return jsonify({
            "success": False,
            "message": "Unauthorized"
        })

    try:

        # =========================================
        # GENERATE DELIVERY OTP
        # =========================================

        otp = random.randint(100000, 999999)

        # =========================================
        # GET BOOKING DETAILS
        # =========================================

        booking = bookings_collection.find_one({
            "_id": ObjectId(booking_id)
        })

        if not booking:
            return jsonify({
                "success": False,
                "message": "Booking not found"
            })

        # =========================================
        # USER EMAIL
        # =========================================

        user_email = booking["user_email"]

        # =========================================
        # UPDATE BOOKING STATUS
        # =========================================

        bookings_collection.update_one(
            {"_id": ObjectId(booking_id)},
            {
                "$set": {

                    "status": "Approved",

                    "delivery_otp": str(otp),

                    "approved_at": datetime.now()

                }
            }
        )

        # =========================================
        # SEND REAL OTP EMAIL
        # =========================================

        msg = Message(

            subject="AquaFlow Delivery OTP",

            sender=app.config["MAIL_USERNAME"],

            recipients=[user_email]

        )

        msg.body = f"""
Hello,

Your AquaFlow water tanker booking has been approved.

Your Delivery OTP is:

{otp}

Please share this OTP with the driver after delivery confirmation.

Thank You,
AquaFlow Team
"""

        send_mail_capped(app, mail, msg, timeout_seconds=6)
        notifications_collection.insert_one({

    "user_email": user_email,

    "title": "OTP Sent",

    "message": "Delivery OTP is sent to your registered Gmail",

    "type": "otp",

    "status": "success",

    "icon": "📧",

    "read": False,

     "created_at": datetime.now()

})

        # =========================================
        # BOOKING CONFIRMED NOTIFICATION
        # =========================================

        notifications_collection.insert_one({

            "user_email": user_email,

            "title": "Booking Confirmed",

            "message": "Your water tanker booking has been approved",

            "type": "confirmed",

            "status": "success",

            "icon": "✅",

            "read": False,

             "created_at": datetime.now()

        })

        
        # =========================================
        # SUCCESS RESPONSE
        # =========================================

        return jsonify({
            "success": True,
            "message": "Booking approved successfully"
        })

    except Exception as e:

        return jsonify({
            "success": False,
            "message": str(e)
        })

# ================= ADMIN CANCEL BOOKING =================

@app.route('/cancel-booking/<booking_id>')
def cancel_booking(booking_id):

    # =========================================
    # SECURITY CHECK - ADMIN ONLY
    # =========================================

    if 'admin' not in session:
        return jsonify({
            "success": False,
            "message": "Unauthorized"
        })

    try:

        # =========================================
        # GET BOOKING
        # =========================================

        booking = bookings_collection.find_one({
            "_id": ObjectId(booking_id)
        })

        if not booking:
            return jsonify({
                "success": False,
                "message": "Booking not found"
            })

        user_email = booking["user_email"]

        # =========================================
        # WALLET REFUND (Feature 17)
        # If any wallet balance was applied to this booking, give it
        # back regardless of how the rest of the payment was handled -
        # this is separate from the online-payment refund/cancellation
        # fee logic below, since it was never actually "charged" to a
        # gateway in the first place.
        # =========================================

        wallet_used = booking.get("wallet_used", 0)
        if wallet_used:
            users_collection.update_one(
                {"email": user_email},
                {"$inc": {"wallet_balance": wallet_used}}
            )
            wallet_transactions_collection.insert_one({
                "user_email": user_email,
                "type": "refund_to_wallet",
                "booking_id": str(booking_id),
                "amount": wallet_used,
                "created_at": datetime.now()
            })

        # =========================================
        # CANCELLATION POLICY
        # By business decision, this app does not run an automated
        # refund system at all - once a tanker has actually been paid
        # for or a driver has been dispatched, it's realistically not
        # reversible, so cancellation from here is blocked outright
        # rather than triggering any refund/cancellation-fee logic.
        # If a customer needs a resolution (e.g. bad water quality),
        # that's handled by support contacting them directly, not by
        # anything in this system.
        # =========================================

        driver_already_assigned = bool(booking.get("driver_name")) or booking.get("status") in (
            "Assigned", "Accepted", "Arrived", "On The Way"
        )

        if booking.get("payment_status") == "Paid (Online)":
            return jsonify({
                "success": False,
                "message": "This booking has already been paid for and can't be cancelled from here. "
                           "If the customer needs a resolution, contact them directly - refunds aren't handled automatically."
            })

        if driver_already_assigned:
            return jsonify({
                "success": False,
                "message": "A driver has already been assigned to this booking, so it can't be cancelled from here. "
                           "Contact the customer directly if it needs to be resolved."
            })

        # =========================================
        # UPDATE STATUS
        # =========================================

        bookings_collection.update_one(
            {"_id": ObjectId(booking_id)},
            {"$set": {"status": "Cancelled"}}
        )

        # =========================================
        # ADD NOTIFICATION
        # =========================================

        notifications_collection.insert_one({

            "user_email": user_email,

            "title": "Booking Cancelled",

            "message": "Your water tanker booking has been cancelled.",

            "type": "cancelled",

            "status": "danger",

            "icon": "❌",

            "read": False,
    "created_at": datetime.now()

        })

        # =========================================
        # SUCCESS RESPONSE
        # =========================================

        return jsonify({
            "success": True,
            "message": "Booking cancelled successfully"
        })

    except Exception as e:

        return jsonify({
            "success": False,
            "message": str(e)
        })
# ================= ADMIN EDIT PRICE / EXTRA CHARGES / DISCOUNT =================
@app.route('/update-booking-price/<booking_id>', methods=['POST'])
def update_booking_price(booking_id):

    # =========================================
    # SECURITY CHECK - ADMIN ONLY
    # =========================================

    if 'admin' not in session:
        return jsonify({
            "success": False,
            "message": "Unauthorized"
        })

    try:
        data = request.get_json(force=True) or {}

        def to_num(v):
            try:
                return round(float(v), 2)
            except (TypeError, ValueError):
                return 0.0

        booking = bookings_collection.find_one({"_id": ObjectId(booking_id)})
        if not booking:
            return jsonify({
                "success": False,
                "message": "Booking not found"
            })

        existing_breakdown = booking.get("price_breakdown") or {}

        water_charge = to_num(data.get('water_charge'))
        distance_charge = to_num(data.get('distance_charge'))
        emergency_charge = to_num(data.get('emergency_charge'))
        discount = to_num(data.get('discount'))

        # Platform fee: editable, but defaults to whatever this booking
        # already had (or the standard fee) rather than silently
        # dropping to Rs0 if the admin's request doesn't include it.
        platform_fee = to_num(data.get('platform_fee')) if 'platform_fee' in data else existing_breakdown.get('platform_fee', 20)

        gst_percent = existing_breakdown.get('gst_percent', 18)

        # GST is a tax, not something an admin manually types - it's
        # always recalculated from the real components so editing the
        # price can never accidentally under- or over-charge GST.
        subtotal = water_charge + distance_charge + emergency_charge + platform_fee
        gst = round(subtotal * gst_percent / 100, 2)

        total = subtotal + gst - discount
        if total < 0:
            total = 0

        # NOTE: "delivery_fee" is the single field every other part of the
        # app already reads (driver earnings, payment page, exports, etc).
        # We keep updating that same field so nothing downstream breaks —
        # we just also store the itemised breakdown alongside it.
        bookings_collection.update_one(
            {"_id": ObjectId(booking_id)},
            {"$set": {
                "price_breakdown": {
                    "water_charge": water_charge,
                    "distance_charge": distance_charge,
                    "emergency_charge": emergency_charge,
                    "platform_fee": platform_fee,
                    "gst_percent": gst_percent,
                    "gst": gst,
                    "discount": discount
                },
                "delivery_fee": total,
                "price_updated_at": datetime.now()
            }}
        )

        return jsonify({
            "success": True,
            "message": "Price updated successfully",
            "total": total
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "message": str(e)
        })

# ================= ADMIN DELIVER BOOKING =================
@app.route('/deliver-booking/<booking_id>')
def deliver_booking(booking_id):

    # SECURITY CHECK - Admin only
    if 'admin' not in session:
        return jsonify({
            "success": False,
            "message": "Unauthorized"
        })

    try:
        bookings_collection.update_one(
            {"_id": ObjectId(booking_id)},
            {
                "$set": {
                    "status": "Delivered"
                }
            }
        )

        return jsonify({
            "success": True,
            "message": "Booking Delivered"
        })
    except:
        return jsonify({
            "success": False,
            "message": "Invalid booking ID"
        })

# ================= API BOOKING DETAILS =================
@app.route('/api/booking/<booking_id>')
def get_booking_details(booking_id):

    # SECURITY CHECK - Admin only
    if 'admin' not in session:
        return jsonify({
            "success": False,
            "message": "Unauthorized"
        })

    try:
        booking = bookings_collection.find_one({
            "_id": ObjectId(booking_id)
        })

        if not booking:
            return jsonify({
                "success": False,
                "message": "Booking not found"
            })

        booking['_id'] = str(booking['_id'])

        return jsonify({
            "success": True,
            "booking": booking
        })
    except:
        return jsonify({
            "success": False,
            "message": "Invalid booking ID"
        })

# ================= EXPORT BOOKINGS =================
@app.route('/export-bookings')
def export_bookings():

    # SECURITY CHECK - Admin only
    if 'admin' not in session:
        return jsonify({
            "success": False,
            "message": "Unauthorized"
        })

    bookings = list(bookings_collection.find())

    for booking in bookings:
        booking['_id'] = str(booking['_id'])

    return jsonify(bookings)
# ================= ADMIN LOGOUT =================
@app.route('/admin-logout')
def admin_logout():

    session.pop('admin', None)
    session.pop('admin_email', None)

    return redirect(url_for('admin_login'))


# ================= RUN APP =================
if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)