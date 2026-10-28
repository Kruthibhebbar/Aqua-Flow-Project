/* Dashboard: greeting, delivery progress fill, weekly chart, route map. */
(function () {
    'use strict';
    var AF = window.AF, $ = AF.$;
    var data = JSON.parse($('#af-dash-data').textContent);

    /* greeting follows the driver's own clock, not the server's */
    var greet = $('#af-greeting');
    if (greet) {
        var h = new Date().getHours();
        var word = h < 12 ? 'Good morning' : h < 17 ? 'Good afternoon' : 'Good evening';
        greet.textContent = word + ', ' + greet.getAttribute('data-name');
    }

    /* the tank fills once when the page opens */
    var tank = $('#af-tank');
    if (tank) requestAnimationFrame(function () { requestAnimationFrame(function () { tank.style.setProperty('--fill', tank.getAttribute('data-fill')); }); });

    /* last 7 days of real earnings (redrawn when the column width changes) */
    var weekHost = $('#af-week-chart'), weekH = 200;
    function drawWeek() {
        AF.chart(weekHost, data.week.map(function (p) {
            return { label: p.label, value: p.earnings, tip: p.label + ': \u20B9' + AF.inr(p.earnings) + ' from ' + p.deliveries + ' deliver' + (p.deliveries === 1 ? 'y' : 'ies') };
        }), { kind: 'bar', money: true, height: weekH, fit: true, label: 'Earnings for the last 7 days', empty: 'No earnings in the last 7 days.' });
    }
    if (weekHost) {
        drawWeek();
        if (window.ResizeObserver) {
            var drawnAt = weekHost.clientWidth, timer;
            new ResizeObserver(function () {
                clearTimeout(timer);
                timer = setTimeout(function () {
                    if (Math.abs(weekHost.clientWidth - drawnAt) > 2) { drawnAt = weekHost.clientWidth; drawWeek(); }
                }, 120);
            }).observe(weekHost);
        }
    }

    /* First row of cards (current delivery | earnings): both cards end just inside the first screen, so their bottom edges are
       visible without scrolling. The chart takes the height that is left (120 to 320px); on the stacked layout it keeps 200px.
       The row is only ever given a min-height, so content is never clipped. */
    var firstRow = document.querySelector('.dash-rows > .dash-row:first-child');
    var BOTTOM_GAP = 5;
    function setChartHeight(h) {
        if (!weekHost || Math.abs(h - weekH) < 3) return;
        weekH = h; drawWeek();
    }
    function fitFirstRow() {
        if (!firstRow) return;
        firstRow.style.removeProperty('--dash-first-h');
        if (getComputedStyle(firstRow).gridTemplateColumns.split(' ').length < 2) { setChartHeight(200); return; }
        var top = firstRow.getBoundingClientRect().top + window.pageYOffset;
        var target = Math.round(window.innerHeight - top - BOTTOM_GAP);
        if (target <= 0) { setChartHeight(200); return; }
        if (weekHost && weekHost.querySelector('svg')) {
            var body = weekHost.parentElement, cs = getComputedStyle(body), head = body.previousElementSibling;
            var chrome = (head ? head.offsetHeight : 0) + parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom) + 2;
            setChartHeight(Math.max(120, Math.min(320, target - chrome)));
        }
        firstRow.style.setProperty('--dash-first-h', target + 'px');
    }
    fitFirstRow();
    window.addEventListener('resize', fitFirstRow);
    window.addEventListener('load', fitFirstRow);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(fitFirstRow);
    if (window.ResizeObserver) {
        ['.af-dash .ledger', '.af-dash .af-hello', '.af-dash .dash-alerts'].forEach(function (sel) {
            var el = document.querySelector(sel);
            if (el) new ResizeObserver(fitFirstRow).observe(el);
        });
    }

    /* tell the driver when an admin assigns / changes something while the page is open */
    var told = false;
    document.addEventListener('af:live', function (e) {
        if (told || e.detail.signature === data.signature) return;
        told = true;
        var host = $('#af-toasts'), el = document.createElement('div');
        el.className = 'toast is-warning';
        el.innerHTML = '<i class="fa-solid fa-bell" aria-hidden="true"></i><span>Your deliveries changed.</span><button type="button" style="color:#fff;font-weight:700">Refresh</button>';
        el.querySelector('button').addEventListener('click', function () { location.reload(); });
        host.appendChild(el);
    });

    /* ------------------------------------------------------------- map */
    var m = data.map;
    var mapEl = $('#af-map');
    if (!m || !mapEl) return;
    var status = $('#af-map-status'), fresh = $('#af-map-fresh');

    if (typeof L === 'undefined') {
        mapEl.innerHTML = '<div class="chart-empty">The map could not load (map files are blocked or offline). Your location is still shared while you are online. Use Open in Maps for directions.</div>';
        return;
    }

    var map = L.map(mapEl, { zoomControl: true, attributionControl: true }).setView([m.dest_lat, m.dest_lng], 14);
    L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png', {
        maxZoom: 19, attribution: '&copy; OpenStreetMap contributors &copy; CARTO'
    }).addTo(map);
    L.marker([m.dest_lat, m.dest_lng]).addTo(map).bindPopup('<strong>Customer</strong><br>' + AF.esc(m.address));

    var me = null, ring = null, route = null, lastFixAt = null, fitted = false;

    function paintFreshness() {
        if (!lastFixAt) return;
        var secs = Math.max(0, Math.round((Date.now() - lastFixAt) / 1000));
        var live = secs <= 30;
        fresh.hidden = false;
        fresh.className = 'tag ' + (live ? 'tone-success' : 'tone-warning');
        fresh.textContent = live ? 'Live, updated ' + secs + 's ago' : 'Last update ' + (secs < 3600 ? Math.round(secs / 60) + ' min' : Math.round(secs / 3600) + ' h') + ' ago';
    }
    setInterval(paintFreshness, 5000);

    function place(lat, lng, accuracy, at) {
        lastFixAt = at || Date.now();
        if (!me) {
            me = L.marker([lat, lng]).addTo(map).bindPopup('<strong>You</strong>');
            ring = L.circle([lat, lng], { radius: accuracy || 30, color: '#2f6bff', weight: 1, fillOpacity: 0.08 }).addTo(map);
        } else { me.setLatLng([lat, lng]); ring.setLatLng([lat, lng]).setRadius(accuracy || 30); }
        if (!fitted) { map.fitBounds([[lat, lng], [m.dest_lat, m.dest_lng]], { padding: [40, 40] }); fitted = true; }
        paintFreshness();
    }

    document.addEventListener('af:gps', function (e) { place(e.detail.lat, e.detail.lng, e.detail.accuracy, e.detail.at); status.textContent = 'Live GPS from this device.'; });
    document.addEventListener('af:gps-error', function (e) {
        var msg = {
            denied: 'Location is blocked in your browser, so the map can’t show where you are. Allow location for this site, then reload.',
            unsupported: 'This browser has no location support.',
            timeout: 'GPS signal is weak. The map will update as soon as a fix arrives.',
            unavailable: 'No GPS position is available right now.'
        }[e.detail.kind];
        if (msg) status.textContent = msg;
    });
    document.addEventListener('af:online', function (e) {
        if (!e.detail.online) status.textContent = 'You are offline, so your location is not being shared.';
    });

    /* server-side route + ETA (road distance, not straight-line) */
    var etaEl = $('#af-eta'), stopped = false;
    function pollTrack() {
        if (stopped || document.hidden) return;
        AF.api('/api/gps/track/' + m.booking_id).then(function (r) {
            var d = r.data;
            if (!r.ok || !d.success) return;
            if (d.driver_lat != null && d.driver_lng != null && !lastFixAt) {
                // Only the server's last known position: say so, never call it live.
                place(d.driver_lat, d.driver_lng, d.driver_accuracy, d.last_gps_at ? Date.parse(d.last_gps_at) : null);
                status.textContent = 'Showing your last position known to AquaFlow.';
            }
            if (d.route_geometry && d.route_geometry.length > 1) {
                if (route) map.removeLayer(route);
                route = L.polyline(d.route_geometry, { color: '#2f6bff', weight: 5, opacity: .8 }).addTo(map);
            }
            if (d.arrived) { etaEl.textContent = 'You have arrived.'; stopped = true; }
            else if (d.eta_min != null) etaEl.textContent = d.eta_min + ' min, ' + d.distance_remaining_km + ' km by road';
            else if (d.driver_online === false) etaEl.textContent = 'Not available while your GPS is offline';
            if (d.route_deviated) AF.toast('You appear to be off the planned route.', 'warning');
        });
    }
    pollTrack(); setInterval(pollTrack, 10000);
    document.addEventListener('af:arrived', function () { setTimeout(function () { location.reload(); }, 1200); });
})();