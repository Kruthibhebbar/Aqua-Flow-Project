from flask import (
    render_template,
    redirect,
    url_for,
    session,
    request,
    jsonify
)

from bson.objectid import ObjectId
from datetime import datetime



def init_driver_assignment(
    app,
    bookings_collection,
    users_collection,
    drivers_collection,
    notifications_collection
):

    # =========================================================
    # DRIVER ASSIGNMENT DASHBOARD
    # =========================================================

    @app.route("/driver_assignment")
    def driver_assignment_dashboard():

        # =====================================================
        # ADMIN SECURITY
        # =====================================================

        if "admin" not in session:
            return redirect(
                url_for("admin_login")
            )

        # =====================================================
        # SEARCH + FILTERS
        # =====================================================

        search = request.args.get(
            "search",
            ""
        ).strip()

        status_filter = request.args.get(
            "status",
            ""
        ).strip()

        query = {}

        # =====================================================
        # SEARCH QUERY
        # =====================================================

        if search:

            query["$or"] = [

                {
                    "fullname": {
                        "$regex": search,
                        "$options": "i"
                    }
                },

                {
                    "driver_name": {
                        "$regex": search,
                        "$options": "i"
                    }
                },

                {
                    "truck_number": {
                        "$regex": search,
                        "$options": "i"
                    }
                },

                {
                    "status": {
                        "$regex": search,
                        "$options": "i"
                    }
                }
            ]

        # =====================================================
        # STATUS FILTER
        # =====================================================

        if status_filter:
            query["status"] = status_filter

        # =====================================================
        # FETCH BOOKINGS
        # =====================================================

        bookings = list(

            bookings_collection.find(query)

            .sort("created_at", -1)

        )

        # =====================================================
        # SAFE DEFAULTS + PROGRESS SYSTEM
        # =====================================================

        status_progress = {

            "Pending": 10,

            "Approved": 35,

            "On The Way": 75,

            "Delivered": 100
        }

        total_water = 0

        for booking in bookings:

            # SAFE FIELDS

            booking["fullname"] = booking.get(
                "fullname",
                "Unknown Customer"
            )

            booking["driver_name"] = booking.get(
                "driver_name",
                "Not Assigned"
            )

            booking["driver_phone"] = booking.get(
                "driver_phone",
                "Not Available"
            )

            booking["truck_number"] = booking.get(
                "truck_number",
                "Not Assigned"
            )

            booking["live_location"] = booking.get(
                "live_location",
                "Waiting For Dispatch"
            )

            booking["ETA"] = booking.get(
                "ETA",
                "Pending"
            )

            booking["status"] = booking.get(
                "status",
                "Pending"
            )

            booking["created_at"] = booking.get(
                "created_at",
                datetime.now()
            )

            # =================================================
            # PROGRESS PERCENT
            # =================================================

            booking["progress_percent"] = status_progress.get(

                booking["status"],

                10
            )

            # =================================================
            # TOTAL WATER
            # =================================================

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

                booking["quantity"] = quantity

                total_water += quantity

            except:

                booking["quantity"] = 0

        # =====================================================
        # ANALYTICS
        # =====================================================

        total_bookings = bookings_collection.count_documents({})

        pending_bookings = bookings_collection.count_documents({
            "status": "Pending"
        })

        approved_bookings = bookings_collection.count_documents({
            "status": "Approved"
        })

        on_the_way = bookings_collection.count_documents({
            "status": "On The Way"
        })

        delivered_bookings = bookings_collection.count_documents({
            "status": "Delivered"
        })

        assigned_drivers = bookings_collection.count_documents({

            "driver_name": {
                "$ne": "Not Assigned"
            }
        })

        # =====================================================
        # ACTIVE DELIVERIES
        # =====================================================

        active_deliveries = list(

            bookings_collection.find({

                "status": "On The Way"

            })

            .sort("created_at", -1)

            .limit(5)
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
        # PROFESSIONAL DRIVER DATA - FIXED ✅
        # =====================================================

        drivers = list(
            drivers_collection.find({
                "approval_status": "Approved",
                "online_status": "Online"
            })
        )

        # =====================================================
        # PAGE RENDER
        # =====================================================

        return render_template(

            "driver_assignment.html",

            bookings=bookings,

            drivers=drivers,

            search=search,

            status_filter=status_filter,

            total_bookings=total_bookings,

            pending_bookings=pending_bookings,

            approved_bookings=approved_bookings,

            on_the_way=on_the_way,

            delivered_bookings=delivered_bookings,

            assigned_drivers=assigned_drivers,

            total_water=total_water,

            active_deliveries=active_deliveries,

            recent_bookings=recent_bookings
        )

    # =========================================================
    # ASSIGN DRIVER
    # =========================================================

    @app.route(
        "/assign-driver/<booking_id>",
        methods=["POST"]
    )
    def assign_driver(booking_id):

        # =====================================================
        # ADMIN SECURITY
        # =====================================================

        if "admin" not in session:
            return redirect(
                url_for("admin_login")
            )

        # =====================================================
        # GET FORM DATA
        # =====================================================

        driver_name = request.form.get("driver_name")
        driver_email = request.form.get("driver_email")
        driver_phone = request.form.get("driver_phone")
        truck_number = request.form.get("truck_number")
        live_location = request.form.get("live_location")
        ETA = request.form.get("ETA")

        # =====================================================
        # GET BOOKING
        # =====================================================

        booking = bookings_collection.find_one({
            "_id": ObjectId(booking_id)
        })

        # =====================================================
        # UPDATE DATABASE
        # =====================================================

        bookings_collection.update_one(
            {"_id": ObjectId(booking_id)},
            {
                "$set": {
                    "driver_name": driver_name,
                    "driver_email": driver_email,
                    "driver_phone": driver_phone,
                    "truck_number": truck_number,
                    "live_location": live_location,
                    "ETA": ETA,
                    "status": "Assigned",
                    "driver_response": "Pending",
                    "last_updated": datetime.now(),
                    "driver_assigned_at": datetime.now()
                }
            }
        )

        # =====================================================
        # NOTIFICATION TO USER
        # =====================================================
        notifications_collection.insert_one({
            "user_email": booking["user_email"],
            "title": "🚛 Driver Assigned - Awaiting Confirmation",
            "message": f"""
🚛 Driver {driver_name} has been assigned to your delivery.

⏳ Status: Waiting for driver to accept the request

📋 Booking ID: {str(booking['_id'])[:8]}

You will be notified once the driver accepts.
""",
            "type": "waiting_driver",
            "status": "warning",
            "icon": "⏳",
            "read": False,
            "created_at": datetime.now(),
            "time": "Just now"
        })

        # =====================================================
        # NOTIFICATION TO DRIVER
        # =====================================================
        notifications_collection.insert_one({
            "driver_email": driver_email,
            "title": "📦 New Water Delivery Request",
            "message": f"""
You have a new delivery request!

👨 Customer: {booking['fullname']}
📍 Address: {booking['address'][:100]}
💧 Quantity: {booking['quantity']} Liters
📅 Delivery: {booking['delivery_date']} at {booking['delivery_time']}
💰 Payment: {booking.get('payment_status', 'Pending')}

Please login to your driver portal to ACCEPT or REJECT this request.
""",
            "booking_id": str(booking["_id"]),
            "type": "driver_request",
            "status": "Pending",
            "read": False,
            "created_at": datetime.now(),
            "time": "Just now"
        })

        # =====================================================
        # REDIRECT
        # =====================================================

        return redirect(
            url_for("driver_assignment_dashboard")
        )
    
    # =========================================================
    # DELETE BOOKING
    # =========================================================

    @app.route(
        "/delete-booking/<booking_id>"
    )
    def delete_booking(booking_id):

        if "admin" not in session:
            return redirect(
                url_for("admin_login")
            )

        bookings_collection.delete_one({

            "_id": ObjectId(booking_id)

        })

        return redirect(
            url_for("driver_assignment_dashboard")
        )

    # =========================================================
    # QUICK STATUS UPDATE
    # =========================================================

    @app.route(
        "/update-driver-status/<booking_id>",
        methods=["POST"]
    )
    def update_driver_status(booking_id):

        if "admin" not in session:

            return jsonify({
                "success": False
            })

        status = request.form.get(
            "status"
        )

        bookings_collection.update_one(

            {
                "_id": ObjectId(booking_id)
            },

            {
                "$set": {

                    "status": status,

                    "last_updated": datetime.now()
                }
            }
        )

        return jsonify({

            "success": True,

            "message": "Status Updated"
        })

    # =========================================================
    # LIVE DELIVERY API
    # =========================================================

    @app.route("/api/live-deliveries")
    def live_deliveries():

        if "admin" not in session:

            return jsonify({
                "success": False
            })

        deliveries = list(

            bookings_collection.find({

                "status": "On The Way"

            })

            .sort("created_at", -1)
        )

        for item in deliveries:

            item["_id"] = str(
                item["_id"]
            )

        return jsonify({

            "success": True,

            "deliveries": deliveries
        })

    # =========================================================
    # DRIVER ASSIGNMENT DETAIL VIEW
    # =========================================================
    
    @app.route("/driver-assignment/<booking_id>")
    def driver_assignment_detail(booking_id):

        booking = bookings_collection.find_one({

            "_id": ObjectId(booking_id)

        })

        drivers = list(
            drivers_collection.find({
                "approval_status": "Approved",
                "online_status": "Online"
            })
        )

        return render_template(

            "driver_assignment.html",

            booking=booking,

            drivers=drivers

        )

    # =========================================================
    # API LATEST BOOKINGS FOR AUTO-REFRESH - FIXED VERSION
    # =========================================================

    @app.route("/api/latest-bookings")
    def api_latest_bookings():
        """API endpoint for auto-refresh to get latest MongoDB data"""
        
        # Debug print
        print("📡 API /api/latest-bookings called")
        
        if "admin" not in session:
            print("❌ Unauthorized: No admin in session")
            return jsonify({"success": False, "error": "Unauthorized"})
        
        try:
            # Fetch latest bookings
            bookings = list(bookings_collection.find().sort("created_at", -1))
            print(f"✅ Found {len(bookings)} bookings in MongoDB")
            
            # Convert bookings for JSON response (FIXED)
            bookings_list = []
            for booking in bookings:
                try:
                    # Convert ObjectId to string
                    booking_id = str(booking["_id"])
                    
                    # Get quantity as integer safely
                    quantity_raw = booking.get("quantity", 0)
                    try:
                        if isinstance(quantity_raw, str):
                            water_qty = int(quantity_raw.replace("Liters", "").replace("L", "").replace(",", "").strip())
                        else:
                            water_qty = int(quantity_raw)
                    except:
                        water_qty = 0
                    
                    bookings_list.append({
                        "id": booking_id,
                        "_id": booking_id,
                        "bookingRef": "#" + booking_id[:8],
                        "customer": booking.get("fullname", "Unknown Customer"),
                        "driver": booking.get("driver_name", "Not Assigned"),
                        "driverEmail": booking.get("driver_email", ""),
                        "phone": booking.get("driver_phone", "Not Available"),
                        "truckNumber": booking.get("truck_number", "Not Assigned"),
                        "waterQuantity": water_qty,
                        "status": booking.get("status", "Pending"),
                        "liveLocation": booking.get("live_location", "Not Set"),
                        "eta": booking.get("ETA", "—"),
                        "created_at": booking.get("created_at", datetime.now()).isoformat() if booking.get("created_at") else None
                    })
                except Exception as e:
                    print(f"⚠️ Error processing booking {booking.get('_id')}: {e}")
                    continue
            
            # Fetch latest drivers
            drivers_list = []
            try:
                drivers = list(drivers_collection.find({
                    "approval_status": "Approved",
                    "online_status": "Online"
                }))
                print(f"✅ Found {len(drivers)} drivers")
                
                for driver in drivers:
                    driver_name = driver.get("name", "Unknown")
                    drivers_list.append({
                        "id": str(driver["_id"]),
                        "_id": str(driver["_id"]),
                        "name": driver_name,
                        "phone": driver.get("phone", ""),
                        "email": driver.get("email", driver_name.lower().replace(" ", ".") + "@transit.com"),
                        "truck": driver.get("truck_number", "Not Assigned"),
                        "status": driver.get("status", "Available"),
                        "currentLocation": driver.get("current_location", "Depot")
                    })
            except Exception as e:
                print(f"⚠️ Error fetching drivers: {e}")
            
            response_data = {
                "success": True,
                "bookings": bookings_list,
                "drivers": drivers_list,
                "timestamp": datetime.now().isoformat(),
                "count": len(bookings_list)
            }
            
            # Add cache-control headers
            response = jsonify(response_data)
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
            
            print(f"📤 Returning {len(bookings_list)} bookings and {len(drivers_list)} drivers")
            return response
            
        except Exception as e:
            print(f"❌ API Error: {str(e)}")
            import traceback
            traceback.print_exc()
            return jsonify({
                "success": False, 
                "error": str(e),
                "message": "Internal server error"
            })

    # =========================================================
    # TEST API ENDPOINT FOR DEBUGGING
    # =========================================================
    
    @app.route("/api/test")
    def api_test():
        """Test endpoint to check if API is working"""
        if "admin" not in session:
            return jsonify({"success": False, "error": "Unauthorized"})
        
        return jsonify({
            "success": True,
            "message": "API is working!",
            "session_admin": True,
            "timestamp": datetime.now().isoformat()
        })