from flask import Flask, render_template, request, redirect, url_for, session, make_response, jsonify, flash
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
from driver_assignment import init_driver_assignment
from notification import init_notification
from driver_auth import init_driver_auth
from driver_dashboard import init_driver_dashboard
from driver_management import init_driver_management
from new_driver_requests import init_new_driver_requests
from booking_management import init_booking_management
from driver_notifications import init_driver_notifications
from live_status_tracking import init_live_tracking
from payment_admin import init_payment_admin, get_bank_details, get_finance_overview
from razorpay_payments import init_razorpay_payments, razorpay_configured
from gps_engine import init_gps_engine
from mail_utils import send_mail_capped

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
# MAIL CONFIG
# =====================================================

app.config['MAIL_SERVER'] = 'smtp.gmail.com'

app.config['MAIL_PORT'] = 587

app.config['MAIL_USE_TLS'] = True

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

# ================= MAIL CONFIG =================
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True

app.config['MAIL_USERNAME'] = os.getenv("MAIL_USERNAME")
app.config['MAIL_PASSWORD'] = os.getenv("MAIL_PASSWORD")

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
    mail
)
init_driver_dashboard(
    app,
    drivers_collection,
    bookings_collection,
    notifications_collection,
    earnings_collection=earnings_collection,
    mail=mail,
    settings_collection=settings_collection,
    transactions_collection=transactions_collection
)
init_driver_management(
    app,
    drivers_collection,
    bookings_collection,
    settings_collection=settings_collection,
    transactions_collection=transactions_collection
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
    transactions_collection
)
init_razorpay_payments(
    app,
    bookings_collection,
    settings_collection
)
init_gps_engine(
    app,
    drivers_collection,
    bookings_collection,
    driver_locations_collection,
    tracking_history_collection,
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

            send_mail_capped(mail, msg, timeout_seconds=6)

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

    user = users_collection.find_one({
        "email": user_email
    })

    # TOTAL BOOKINGS
    total_bookings = bookings_collection.count_documents({
        "user_email": user_email
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
            "user_email": user_email
        }).sort("created_at", -1).limit(5)
    )

    # LIVE ACTIVITIES
    live_activities = list(
        bookings_collection.find({
            "user_email": user_email
        }).sort("created_at", -1).limit(10)
    )

    # TOTAL WATER
    total_water = 0

    all_bookings = bookings_collection.find({
        "user_email": user_email
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
    return render_template("payment_system.html")

#=================analytics_charts======================================
@app.route("/analytics_charts")
def analytics_charts():
    return render_template("analytics_charts.html")


#==========profile_system===============================================
@app.route("/profile_system")
def profile_system():
    return render_template("profile_system.html")

#=========quick_rebook=================================================
@app.route("/quick_rebook")
def quick_rebook():
    return render_template("quick_rebook.html")

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

    return render_template(
        'profile.html',
        user=user,
        total_orders=total_orders,
        completed_orders=completed_orders,
        cancelled_orders=cancelled_orders,
        active_orders=active_orders
    )

# ================= HISTORY =================
@app.route('/history')
def history():

    if 'user_email' not in session:
        return redirect(url_for('login'))

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

# ================= BOOKING =================
@app.route('/booking', methods=['GET', 'POST'])
def booking():

    # =========================
    # LOGIN CHECK
    # =========================

    if 'user_email' not in session:
        return redirect(url_for('login'))

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
        # PRICING
        # (kept in sync with the pricingMap in booking.html's JS —
        # server-side is the source of truth actually stored/billed)
        # =========================

        pricing_map = {
            "1000": 750,
            "2000": 1200,
            "5000": 2600,
            "10000": 4800
        }

        delivery_fee = pricing_map.get(quantity)

        if delivery_fee is None:
            errors.append("Please select a valid water quantity")

        if errors:
            flash(" | ".join(errors), "danger")
            return render_template('booking.html')

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

            "status": "Pending",

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

        # =========================
        # SAVE TO MONGODB
        # =========================

        result = bookings_collection.insert_one(
            booking_data
        )

        if payment_method == "upi":
            if razorpay_configured():
                return redirect(
                    url_for('razorpay_checkout', booking_id=str(result.inserted_id))
                )
            return redirect(
                url_for('pay_upi', booking_id=str(result.inserted_id))
            )

        return redirect(
            url_for('history')
        )

    return render_template(
        'booking.html'
    )


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

            send_mail_capped(mail, msg, timeout_seconds=6)

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

        users_collection.update_one(
            {"email": session['user_email']},
            {
                "$set": {
                    "phone": phone,
                    "gender": gender,
                    "dob": dob,
                    "address": address
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

        send_mail_capped(mail, msg, timeout_seconds=6)
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
        # UPDATE STATUS
        # =========================================

        bookings_collection.update_one(
            {"_id": ObjectId(booking_id)},
            {
                "$set": {
                    "status": "Cancelled"
                }
            }
        )

        # =========================================
        # ADD NOTIFICATION
        # =========================================

        notifications_collection.insert_one({

            "user_email": user_email,

            "title": "Booking Cancelled",

            "message": "Your water tanker booking has been cancelled",

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