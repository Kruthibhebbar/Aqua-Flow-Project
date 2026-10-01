"""
business_insights.py
=====================

Feature 23 - Analytics: Highest Revenue Day, Most Used Payment Method,
Most Active Driver, Highest Spending Customer, Average Booking Amount,
Average Delivery Time, Peak Booking Hour, Cancelled Orders.

Feature 24 - AI Insights: lightweight, rule-based insights computed
directly from real booking data (no external ML service, no invented
numbers) - busy-hour prediction, tanker-size recommendation, unusual
booking detection, a simple revenue forecast, and driver assignment
suggestions. These are honest statistical heuristics, not a trained
model, and are labelled as such in the UI.
"""

from datetime import datetime, timedelta
from collections import Counter, defaultdict
import statistics

from flask import render_template, session, redirect, url_for


def init_business_insights(app, bookings_collection, drivers_collection=None):

    @app.route("/business-insights")
    def business_insights():

        if "admin" not in session:
            return redirect(url_for("admin_login"))

        all_bookings = list(bookings_collection.find({"status": {"$ne": "Awaiting Payment"}}))
        delivered = [b for b in all_bookings if b.get("status") == "Delivered"]
        paid = [b for b in all_bookings if b.get("payment_status") == "Paid (Online)"]
        cancelled = [b for b in all_bookings if b.get("status") == "Cancelled"]

        def fee(b):
            return b.get("delivery_fee", 0) or 0

        # ============================================================
        # FEATURE 23 - ANALYTICS
        # ============================================================

        # 1. Highest Revenue Day
        revenue_by_day = defaultdict(float)
        for b in delivered:
            d = b.get("delivered_at")
            if isinstance(d, datetime):
                revenue_by_day[d.date()] += fee(b)
        highest_revenue_day = max(revenue_by_day.items(), key=lambda kv: kv[1]) if revenue_by_day else None

        # 2. Most Used Payment Method
        method_counts = Counter(b.get("payment_method", "Unknown") for b in paid)
        most_used_payment_method = method_counts.most_common(1)[0] if method_counts else None

        # 3. Most Active Driver
        driver_counts = Counter(
            b.get("driver_name") for b in delivered if b.get("driver_name")
        )
        most_active_driver = driver_counts.most_common(1)[0] if driver_counts else None

        # 4. Highest Spending Customer
        spend_by_customer = defaultdict(float)
        for b in paid:
            spend_by_customer[b.get("user_email", "Unknown")] += fee(b)
        highest_spending_customer = max(spend_by_customer.items(), key=lambda kv: kv[1]) if spend_by_customer else None

        # 5. Average Booking Amount
        avg_booking_amount = round(statistics.mean([fee(b) for b in all_bookings]), 2) if all_bookings else 0

        # 6. Average Delivery Time (started_at -> delivered_at, falls
        # back to created_at -> delivered_at if a booking never got a
        # "started" timestamp)
        delivery_durations = []
        for b in delivered:
            end = b.get("delivered_at")
            start = b.get("started_at") or b.get("created_at")
            if isinstance(end, datetime) and isinstance(start, datetime) and end > start:
                delivery_durations.append((end - start).total_seconds() / 60)
        avg_delivery_minutes = round(statistics.mean(delivery_durations)) if delivery_durations else None

        # 7. Peak Booking Hour
        hour_counts = Counter(
            b["created_at"].hour for b in all_bookings if isinstance(b.get("created_at"), datetime)
        )
        peak_booking_hour = hour_counts.most_common(1)[0] if hour_counts else None

        # 8. Cancelled Orders
        cancelled_count = len(cancelled)

        # ============================================================
        # FEATURE 24 - AI INSIGHTS (rule-based, from real data only)
        # ============================================================

        # Predict busy booking hours - top 3 hours by historical volume
        busy_hours = [h for h, _ in hour_counts.most_common(3)]

        # Recommend the most-booked tanker size
        size_counts = Counter(b.get("quantity") for b in all_bookings if b.get("quantity"))
        recommended_tanker_size = size_counts.most_common(1)[0] if size_counts else None

        # Detect unusually large bookings (statistical outliers, not a
        # fixed guess) - anything beyond mean + 2 standard deviations
        unusual_bookings = []
        fees = [fee(b) for b in all_bookings if fee(b) > 0]
        if len(fees) >= 5:
            mean_fee = statistics.mean(fees)
            stdev_fee = statistics.pstdev(fees)
            threshold = mean_fee + (2 * stdev_fee)
            if stdev_fee > 0:
                unusual_bookings = [
                    b for b in all_bookings
                    if fee(b) > threshold
                ][:5]

        # Forecast next day's revenue - simple 7-day trailing average,
        # explicitly labelled as such rather than pretending to be a
        # real forecasting model
        last_7_days = [datetime.now().date() - timedelta(days=i) for i in range(7)]
        last_7_days_revenue = [revenue_by_day.get(d, 0) for d in last_7_days]
        forecast_next_day_revenue = round(statistics.mean(last_7_days_revenue)) if last_7_days_revenue else 0

        # Suggest optimal driver assignment - the approved driver
        # currently carrying the fewest active (Assigned/Accepted/
        # Arrived/On The Way) orders, so new work goes to whoever has
        # the most capacity right now
        suggested_driver = None
        if drivers_collection is not None:
            active_statuses = ("Assigned", "Accepted", "Arrived", "On The Way")
            load_by_driver = Counter(
                b.get("driver_name") for b in all_bookings
                if b.get("status") in active_statuses and b.get("driver_name")
            )
            approved_drivers = list(drivers_collection.find({"approval_status": "Approved"}))
            if approved_drivers:
                least_busy = min(
                    approved_drivers,
                    key=lambda d: load_by_driver.get(d.get("fullname") or d.get("name"), 0)
                )
                driver_label = least_busy.get("fullname") or least_busy.get("name")
                suggested_driver = (
                    driver_label,
                    load_by_driver.get(driver_label, 0)
                )

        return render_template(
            "business_insights.html",
            highest_revenue_day=highest_revenue_day,
            most_used_payment_method=most_used_payment_method,
            most_active_driver=most_active_driver,
            highest_spending_customer=highest_spending_customer,
            avg_booking_amount=avg_booking_amount,
            avg_delivery_minutes=avg_delivery_minutes,
            peak_booking_hour=peak_booking_hour,
            cancelled_count=cancelled_count,
            busy_hours=busy_hours,
            recommended_tanker_size=recommended_tanker_size,
            unusual_bookings=unusual_bookings,
            forecast_next_day_revenue=forecast_next_day_revenue,
            suggested_driver=suggested_driver,
            admin_name="Admin",
            admin_email=session.get("admin_email", "admin@aquaflow.com")
        )

    return app
