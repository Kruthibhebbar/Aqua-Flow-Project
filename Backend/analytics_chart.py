from flask import (
    render_template,
    session,
    redirect,
    url_for,
    jsonify
)

from datetime import (
    datetime,
    timedelta
)

from collections import defaultdict
import calendar


def init_analytics(app, bookings_collection):

    # =========================================================
    # ADVANCED ANALYTICS DASHBOARD
    # =========================================================

    @app.route("/analytics_charts")
    def analytics_charts():

        # =====================================================
        # ADMIN SECURITY
        # =====================================================

        if "admin" not in session:
            return redirect(
                url_for("admin_login")
            )

        # =====================================================
        # FETCH BOOKINGS
        # =====================================================

        bookings = list(
            bookings_collection.find()
        )

        # =====================================================
        # BASIC METRICS
        # =====================================================

        total_bookings = len(bookings)

        total_water = 0

        delivered_bookings = 0
        pending_bookings = 0
        on_the_way = 0
        cancelled_bookings = 0

        assigned_drivers = 0

        # =====================================================
        # TODAY METRICS
        # =====================================================

        today = datetime.now()

        today_bookings = 0
        today_water = 0

        # =====================================================
        # CHART DATA
        # =====================================================

        daily_bookings = [0, 0, 0, 0, 0, 0, 0]
        daily_water = [0, 0, 0, 0, 0, 0, 0]

        weekly_labels = []

        # =====================================================
        # GENERATE LAST 7 DAYS LABELS
        # =====================================================

        for i in range(6, -1, -1):

            day = today - timedelta(days=i)

            weekly_labels.append(
                calendar.day_abbr[day.weekday()]
            )

        # =====================================================
        # AREA / LOCATION ANALYTICS
        # =====================================================

        area_stats = defaultdict(int)

        # =====================================================
        # DRIVER PERFORMANCE
        # =====================================================

        driver_performance = defaultdict(int)

        # =====================================================
        # PROCESS BOOKINGS
        # =====================================================

        for booking in bookings:

            # ================================================
            # WATER QUANTITY
            # ================================================

            quantity = 0

            try:

                quantity = int(

                    str(
                        booking.get(
                            "quantity",
                            0
                        )
                    )

                    .replace("Liters", "")
                    .replace("L", "")
                    .replace(",", "")
                    .strip()
                )

            except:

                quantity = 0

            total_water += quantity

            # ================================================
            # STATUS COUNTS
            # ================================================

            status = booking.get(
                "status",
                "Pending"
            )

            if status == "Delivered":
                delivered_bookings += 1

            elif status == "Pending":
                pending_bookings += 1

            elif status == "On The Way":
                on_the_way += 1

            elif status == "Cancelled":
                cancelled_bookings += 1

            # ================================================
            # DRIVER ASSIGNED
            # ================================================

            driver_name = booking.get(
                "driver_name",
                "Not Assigned"
            )

            if driver_name != "Not Assigned":

                assigned_drivers += 1

                driver_performance[
                    driver_name
                ] += 1

            # ================================================
            # AREA ANALYTICS
            # ================================================

            address = booking.get(
                "address",
                "Unknown"
            )

            area_stats[address] += 1

            # ================================================
            # DATE ANALYTICS
            # ================================================

            created_at = booking.get(
                "created_at"
            )

            if created_at:

                # TODAY BOOKINGS

                if created_at.date() == today.date():

                    today_bookings += 1
                    today_water += quantity

                # WEEKLY CHARTS

                diff = (
                    today.date() -
                    created_at.date()
                ).days

                if 0 <= diff < 7:

                    index = 6 - diff

                    daily_bookings[index] += 1

                    daily_water[index] += quantity

        # =====================================================
        # EFFICIENCY SCORE
        # =====================================================

        efficiency_score = 0

        if total_bookings > 0:

            efficiency_score = int(

                (
                    delivered_bookings /
                    total_bookings
                ) * 100
            )

        # =====================================================
        # AVERAGE DAILY BOOKINGS
        # =====================================================

        avg_daily_bookings = round(

            sum(daily_bookings) / 7,

            1
        )

        # =====================================================
        # PEAK WATER DELIVERY
        # =====================================================

        peak_water = max(daily_water)

        # =====================================================
        # FORECAST
        # =====================================================

        forecast_bookings = int(

            avg_daily_bookings * 7 * 1.15
        )

        projected_water = int(

            (total_water / 7) * 7 * 1.12
        )

        # =====================================================
        # TOP DRIVER
        # =====================================================

        top_driver = "No Driver"

        if driver_performance:

            top_driver = max(

                driver_performance,

                key=driver_performance.get
            )

        # =====================================================
        # MOST ACTIVE AREA
        # =====================================================

        active_area = "Unknown"

        if area_stats:

            active_area = max(

                area_stats,

                key=area_stats.get
            )

        # =====================================================
        # RECENT BOOKINGS
        # =====================================================

        recent_bookings = list(

            bookings_collection.find()

            .sort("created_at", -1)

            .limit(10)
        )

        # =====================================================
        # RENDER DASHBOARD
        # =====================================================

        return render_template(

            "analytics_charts.html",

            # MAIN METRICS
            total_bookings=total_bookings,
            total_water=total_water,
            efficiency_score=efficiency_score,
            avg_daily_bookings=avg_daily_bookings,

            # STATUS
            delivered_bookings=delivered_bookings,
            pending_bookings=pending_bookings,
            on_the_way=on_the_way,
            cancelled_bookings=cancelled_bookings,

            # DRIVER
            assigned_drivers=assigned_drivers,
            top_driver=top_driver,

            # AREA
            active_area=active_area,

            # TODAY
            today_bookings=today_bookings,
            today_water=today_water,

            # FORECAST
            forecast_bookings=forecast_bookings,
            projected_water=projected_water,

            # PEAK
            peak_water=peak_water,

            # CHARTS
            daily_bookings=daily_bookings,
            daily_water=daily_water,
            weekly_labels=weekly_labels,

            # RECENT
            recent_bookings=recent_bookings
        )

    # =========================================================
    # LIVE ANALYTICS API
    # =========================================================

    @app.route("/api/live-analytics")
    def live_analytics():

        if "admin" not in session:

            return jsonify({
                "success": False
            })

        total_bookings = bookings_collection.count_documents({})

        delivered = bookings_collection.count_documents({
            "status": "Delivered"
        })

        pending = bookings_collection.count_documents({
            "status": "Pending"
        })

        on_the_way = bookings_collection.count_documents({
            "status": "On The Way"
        })

        return jsonify({

            "success": True,

            "total_bookings": total_bookings,

            "delivered": delivered,

            "pending": pending,

            "on_the_way": on_the_way
        })