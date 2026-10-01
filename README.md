# AquaFlow — Water Tanker Booking & Delivery Tracking System

AquaFlow is a full-stack web application for booking water tanker deliveries, tracking them
in real time, and verifying delivery completion with a secure OTP handoff between the
customer and the driver. It has three separate user roles — **Customer**, **Driver**, and
**Admin** — each with their own dashboard and workflow.

## Features

### Customer
- Sign up / log in with bcrypt-hashed passwords and email OTP verification
- Book a water tanker delivery (quantity, address, preferred date/time, water type)
- Track booking status live (Pending → Approved → On The Way → Arrived → Delivered)
- Receive a one-time delivery OTP by email once the admin approves the booking
- View booking history, profile, and notifications

### Driver
- Separate driver login with email OTP verification
- Dashboard showing active, completed, and cancelled deliveries
- Live GPS tracking view (Leaflet.js) and ETA estimation
- Start delivery → mark arrived → complete delivery by asking the customer for
  their OTP and verifying it against the one emailed at booking approval
  *(the driver never sees or generates this OTP themselves — see "Security notes" below)*
- Earnings dashboard with weekly chart (Chart.js), completed-delivery stats, and analytics
- Emergency alert button to notify admin
- Document upload for driver verification

### Admin
- Approve / cancel / mark-delivered bookings
- Auto-generates and emails the delivery OTP to the customer on approval
- Dashboard with live stats: total users, orders, revenue, active deliveries
- Assign drivers to pending bookings

## Tech Stack

| Layer      | Technology                                             |
|------------|---------------------------------------------------------|
| Backend    | Python, Flask                                           |
| Database   | MongoDB (via PyMongo)                                   |
| Auth       | bcrypt password hashing, Flask sessions, email OTP      |
| Email      | Flask-Mail (Gmail SMTP)                                 |
| Frontend   | Jinja2 templates, Bootstrap 5, vanilla JS + jQuery       |
| Maps/Charts| Leaflet.js (live tracking), Chart.js (earnings/analytics)|

## Project Structure

```
Backend/
  app.py                     # Main Flask app, routing, auth, admin routes
  driver_auth.py             # Driver login + OTP verification
  driver_dashboard.py        # Driver dashboard, delivery OTP verification, earnings
  driver_management.py       # Admin-side driver management
  driver_assignment.py       # Assigning drivers to bookings
  driver_notifications.py    # Driver-facing notifications, accept/reject booking
  booking_management.py      # Admin booking management
  notification.py            # Customer notifications
  new_driver_requests.py     # New driver signup approval flow
  start_delivery.py          # Start-delivery endpoint (Blueprint)
  reject_booking.py          # Legacy reject-booking endpoint (superseded, see notes)
templates/                   # Jinja2 HTML templates for all pages
static/                      # CSS, images, uploaded driver documents
.env                         # Environment variables (not committed)
```

## Setup

1. **Clone the repo and enter the backend folder**
   ```bash
   git clone <your-repo-url>
   cd Backend
   ```

2. **Create a virtual environment and install dependencies**
   ```bash
   python -m venv venv
   venv\Scripts\activate        # Windows
   source venv/bin/activate     # macOS/Linux
   pip install -r requirements.txt
   ```

3. **Create a `.env` file** in the project root with:
   ```
   SECRET_KEY=your-flask-secret-key
   MONGO_URI=your-mongodb-connection-string
   MAIL_USERNAME=your-gmail-address
   MAIL_PASSWORD=your-gmail-app-password
   ```
   > Use a [Gmail App Password](https://support.google.com/accounts/answer/185833), not your
   > regular Gmail password — Google blocks plain SMTP logins by default.

4. **Run the app**
   ```bash
   cd Backend
   python app.py
   ```
   The app runs at `http://127.0.0.1:5000` by default.

## Testing

Automated tests live in `tests/` and run against a fake, in-memory MongoDB
(via `mongomock`) — no real database connection needed, and nothing in the
tests touches your actual data.

```bash
pip install -r requirements-dev.txt
pytest
```

`pytest.ini` at the project root already points pytest at `Backend/` and
`tests/`, so this works from the repo root without any extra setup.

What's covered: signup validation + OTP verification, login (correct/wrong
password, unknown account, blocked account), booking creation validation
(missing map pin, invalid quantity/phone, no drivers available), a customer
cancelling their own unpaid booking, the admin block/unblock + messaging
endpoints, the delivery OTP handoff (correct code, wrong code, brute-force
lockout, wrong driver), coupon application (percent discount, invalid code,
one-time-per-customer), and wallet recharge signature verification (valid,
forged, and duplicate-callback cases). 32 tests in total.
See `tests/helpers.py` for reusable fixtures — `create_user()`, `login()`,
`submit_booking()`, `login_as_admin()`, `login_as_driver()` — when adding more.

## Continuous Integration (CI)

Every push and pull request to `main` automatically runs, via GitHub Actions
(`.github/workflows/ci.yml`): lint (`ruff check Backend/`) then the full test
suite (`pytest`) — in a clean, disposable environment, the same way a
teammate's machine would run it. The result shows up as a ✅ or ❌ directly
on the commit/PR on GitHub — proof the code was actually verified, not just
"looked fine locally."

The lint rule set (`ruff.toml`) is deliberately narrow for now — real bugs
only (unused imports/variables, undefined names, syntax errors, duplicate
dict keys), not style opinions — so CI fails on things worth fixing, not on
noise. It can be widened later as the codebase's style settles.

## Security notes

- Passwords are hashed with **bcrypt** before being stored — never stored in plain text.
- Delivery completion uses an **OTP handoff pattern**: the OTP is generated once, at booking
  approval time, and emailed only to the customer. The driver must ask the customer for it
  verbally at the door and enter it to confirm delivery — the driver's dashboard never
  generates or displays the real OTP itself. This prevents a driver from marking a delivery
  complete without the customer actually being present.
- Delivery OTP verification is rate-limited to 5 failed attempts per booking to slow down
  brute-force guessing.

## Known limitations / possible improvements

This was built as a learning project, and a few things would need cleanup before treating it
as production-grade:

- `reject_booking.py` contains an older, now-unused duplicate of the reject-booking route
  (the active implementation lives in `driver_notifications.py`) — kept for reference but
  should be removed.
- `analytics_chart.py` is not currently wired into `app.py`.
- No automated test suite yet.
- No pagination on admin booking/user tables — would need it at scale.
- Driver/customer live location is currently simulated on the frontend rather than pulled
  from a real GPS source.

## License

This project is for educational/portfolio purposes.
