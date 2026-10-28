/* AquaFlow Driver Operations Suite - shared shell behaviour.
   API helper, toasts, dialogs, navigation, notifications, Online/GPS, charts. */
(function () {
    'use strict';

    var cfg = JSON.parse(document.getElementById('af-config').textContent);
    var csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
    var AF = window.AF = { cfg: cfg, csrf: csrf };
    var $ = function (sel, root) { return (root || document).querySelector(sel); };
    var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };
    AF.$ = $; AF.$$ = $$;

    function safeStorage(op, key, val) {
        try { return op === 'get' ? localStorage.getItem(key) : localStorage.setItem(key, val); } catch (e) { return null; }
    }

    /* ------------------------------------------------------------ helpers */
    AF.esc = function (s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
        });
    };
    AF.inr = function (n) { return new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 }).format(Math.round(Number(n) || 0)); };

    AF.toast = function (message, tone, ms) {
        tone = tone || 'success';
        var host = $('#af-toasts'); if (!host) return;
        var icon = { success: 'fa-circle-check', danger: 'fa-circle-exclamation', warning: 'fa-triangle-exclamation', info: 'fa-circle-info' }[tone] || 'fa-circle-info';
        var el = document.createElement('div');
        el.className = 'toast is-' + tone;
        el.innerHTML = '<i class="fa-solid ' + icon + '" aria-hidden="true"></i><span></span><button type="button" aria-label="Dismiss">&times;</button>';
        el.querySelector('span').textContent = message;
        el.querySelector('button').addEventListener('click', function () { el.remove(); });
        host.appendChild(el);
        setTimeout(function () { el.remove(); }, ms || (tone === 'danger' ? 7000 : 4500));
    };

    /* Every request goes through here: CSRF header, session-expiry handling,
       and a human message for network / server failures. Never throws. */
    AF.api = function (url, opts) {
        opts = opts || {};
        var headers = { 'Accept': 'application/json', 'X-Requested-With': 'fetch', 'X-CSRF-Token': csrf };
        var init = { method: (opts.method || 'GET').toUpperCase(), headers: headers, credentials: 'same-origin' };
        if (opts.keepalive) init.keepalive = true;
        if (opts.json !== undefined) {
            headers['Content-Type'] = 'application/json';
            init.body = JSON.stringify(opts.json);
            if (init.method === 'GET') init.method = 'POST';
        } else if (opts.form) {
            init.body = opts.form;
            if (init.method === 'GET') init.method = 'POST';
        }
        return fetch(url, init).then(function (res) {
            if (res.redirected && /driver-login/.test(res.url)) {
                AF.toast('Your session expired. Please log in again.', 'warning');
                setTimeout(function () { location.href = cfg.urls.login; }, 1500);
                return { ok: false, status: 401, data: { success: false, message: 'Your session expired. Please log in again.' } };
            }
            var type = res.headers.get('content-type') || '';
            if (type.indexOf('application/json') === -1) {
                return { ok: false, status: res.status, data: { success: false, message: 'Unexpected server response (HTTP ' + res.status + '). Please try again.' } };
            }
            return res.json().then(function (data) {
                if (data && data.sessionExpired) {
                    AF.toast(data.message || 'Your session expired.', 'warning');
                    setTimeout(function () { location.href = cfg.urls.login; }, 1500);
                }
                return { ok: res.ok && data.success !== false, status: res.status, data: data };
            });
        }).catch(function () {
            return { ok: false, status: 0, data: { success: false, message: 'Could not reach the server. Check your connection and try again.' } };
        });
    };

    /* Disable a button and show a spinner while `promise` runs. */
    AF.busy = function (btn, promise) {
        if (!btn) return promise;
        btn.classList.add('is-busy'); btn.setAttribute('aria-busy', 'true');
        var done = function () { btn.classList.remove('is-busy'); btn.removeAttribute('aria-busy'); };
        promise.then(done, done);
        return promise;
    };

    /* Show server-side field errors beside their inputs. */
    AF.showFieldErrors = function (form, errors) {
        $$('.field', form).forEach(function (f) { f.classList.remove('has-error'); var e = $('.field-error', f); if (e) e.textContent = ''; });
        Object.keys(errors || {}).forEach(function (name) {
            var input = form.querySelector('[name="' + name + '"]'); if (!input) return;
            var field = input.closest('.field'); if (!field) return;
            field.classList.add('has-error');
            var holder = $('.field-error', field);
            if (!holder) { holder = document.createElement('div'); holder.className = 'field-error'; holder.setAttribute('role', 'alert'); field.appendChild(holder); }
            holder.textContent = errors[name];
        });
        var first = form.querySelector('.has-error .input, .has-error .select, .has-error .textarea');
        if (first) first.focus();
    };

    /* ------------------------------------------------------------ dialogs */
    document.addEventListener('click', function (e) {
        var closer = e.target.closest('[data-dialog-close]');
        if (closer) { var d = closer.closest('dialog'); if (d) d.close(); }
        if (e.target.tagName === 'DIALOG' && e.target.classList.contains('af-dialog')) e.target.close();
        var opener = e.target.closest('[data-dialog-open]');
        if (opener) { var t = document.getElementById(opener.getAttribute('data-dialog-open')); if (t && t.showModal) t.showModal(); }
    });

    /* Promise-based confirm dialog: AF.confirm({title, text, confirm, tone}) -> true/false */
    AF.confirm = function (o) {
        return new Promise(function (resolve) {
            var d = document.createElement('dialog'); d.className = 'af-dialog';
            d.innerHTML = '<div class="af-dialog-head"><h2></h2><p></p></div><div class="af-dialog-foot" style="padding-top:18px">' +
                '<button type="button" class="btn" data-v="0">Cancel</button>' +
                '<button type="button" class="btn ' + (o.tone === 'danger' ? 'btn-solid-danger' : 'btn-primary') + '" data-v="1"></button></div>';
            d.querySelector('h2').textContent = o.title; d.querySelector('p').textContent = o.text || '';
            d.querySelector('[data-v="1"]').textContent = o.confirm || 'Confirm';
            var result = false;
            d.addEventListener('click', function (e) { var b = e.target.closest('[data-v]'); if (b) { result = b.getAttribute('data-v') === '1'; d.close(); } });
            d.addEventListener('close', function () { d.remove(); resolve(result); });
            document.body.appendChild(d); d.showModal();
        });
    };

    /* --------------------------------------------------------- navigation */
    var app = $('#af-app');
    var collapseBtn = $('#af-collapse');
    function setCollapsed(on) {
        app.classList.toggle('is-collapsed', on);
        if (collapseBtn) collapseBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
        safeStorage('set', 'af-collapsed', on ? '1' : '0');
    }
    if (safeStorage('get', 'af-collapsed') === '1') setCollapsed(true);
    if (collapseBtn) collapseBtn.addEventListener('click', function () { setCollapsed(!app.classList.contains('is-collapsed')); });

    var menuBtn = $('#af-menu-btn');
    function setNav(open) {
        document.body.classList.toggle('nav-open', open);
        if (menuBtn) menuBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
    if (menuBtn) menuBtn.addEventListener('click', function () { setNav(!document.body.classList.contains('nav-open')); });
    $$('[data-af-nav-close]').forEach(function (el) { el.addEventListener('click', function () { setNav(false); }); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') { setNav(false); closePops(); } });

    /* popovers (notifications, account) */
    var pops = [['#af-bell', '#af-bell-panel'], ['#af-user', '#af-user-panel']];
    function closePops(except) {
        pops.forEach(function (p) {
            var b = $(p[0]), panel = $(p[1]); if (!b || !panel || panel === except) return;
            panel.hidden = true; b.setAttribute('aria-expanded', 'false');
        });
    }
    pops.forEach(function (p) {
        var b = $(p[0]), panel = $(p[1]); if (!b || !panel) return;
        b.addEventListener('click', function (e) {
            e.stopPropagation(); closePops(panel);
            panel.hidden = !panel.hidden; b.setAttribute('aria-expanded', panel.hidden ? 'false' : 'true');
        });
        panel.addEventListener('click', function (e) { e.stopPropagation(); });
    });
    document.addEventListener('click', function () { closePops(); });

    /* clock */
    function tick() {
        var now = new Date(), t = $('#af-time'), d = $('#af-date');
        if (t) t.textContent = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        if (d) d.textContent = now.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' });
    }
    tick(); setInterval(tick, 20000);

    /* ------------------------------------------------------ notifications */
    AF.setUnread = function (n) {
        $$('[data-af-unread]').forEach(function (el) { el.textContent = n > 99 ? '99+' : n; el.hidden = !n; });
    };
    $$('#af-bell-panel .note-row').forEach(function (row) {
        row.addEventListener('click', function () {
            if (!row.classList.contains('is-unread')) return;
            AF.api('/api/driver/notifications/' + row.getAttribute('data-note-id') + '/read', { method: 'POST', keepalive: true });
        });
    });
    var readAll = $('#af-read-all');
    if (readAll) readAll.addEventListener('click', function () {
        AF.busy(readAll, AF.api(cfg.urls.readAll, { method: 'POST' })).then(function (r) {
            if (!r.ok) return AF.toast(r.data.message || 'Could not update notifications.', 'danger');
            $$('.note-row.is-unread').forEach(function (n) { n.classList.remove('is-unread'); });
            AF.setUnread(0);
        });
    });

    /* Light poll so the badge (and an open dashboard) notice changes. */
    function poll() {
        if (document.hidden) return;
        AF.api(cfg.urls.live).then(function (r) {
            if (!r.ok) return;
            AF.setUnread(r.data.unread || 0);
            document.dispatchEvent(new CustomEvent('af:live', { detail: r.data }));
        });
    }
    setInterval(poll, 45000);

    /* ------------------------------------------------------- Online + GPS */
    var GPS_MS = 5000;
    var gps = { watch: null, timer: null, last: null, denied: false };
    var chip = $('#af-gps');
    var sw = $('#af-online');

    function setChip(state, text) {
        if (!chip) return;
        chip.setAttribute('data-state', state);
        chip.querySelector('.gps-text').textContent = text;
    }
    function paintOnline(on) {
        cfg.online = on;
        if (sw) { sw.setAttribute('aria-checked', on ? 'true' : 'false'); }
        var t = $('#af-online-text'); if (t) t.textContent = on ? 'Online' : 'Offline';
        $$('[data-af-status-text]').forEach(function (el) { el.textContent = on ? 'Online' : 'Offline'; });
        $$('[data-af-status-dot]').forEach(function (el) { el.classList.toggle('is-on', on); });
        document.dispatchEvent(new CustomEvent('af:online', { detail: { online: on } }));
    }
    function sendPing() {
        if (!gps.last) return;
        var c = gps.last.coords;
        AF.api(cfg.urls.gps, { json: {
            lat: c.latitude, lng: c.longitude, accuracy: c.accuracy, speed: c.speed, heading: c.heading,
            timestamp: new Date().toISOString(), internet_status: navigator.onLine ? 'online' : 'offline'
        } }).then(function (r) {
            if (r.data && r.data.arrived) {
                AF.toast("You've arrived at the customer's location.", 'success');
                document.dispatchEvent(new CustomEvent('af:arrived'));
            }
        });
    }
    function onFix(pos) {
        gps.last = pos; gps.denied = false;
        setChip('active', 'GPS live');
        if (!gps.timer) { gps.timer = setInterval(sendPing, GPS_MS); sendPing(); }
        document.dispatchEvent(new CustomEvent('af:gps', { detail: {
            lat: pos.coords.latitude, lng: pos.coords.longitude, accuracy: pos.coords.accuracy, at: Date.now()
        } }));
    }
    function onGpsError(err) {
        if (err && err.code === 1) {
            gps.denied = true; stopGps();
            setChip('denied', 'Location blocked');
            document.dispatchEvent(new CustomEvent('af:gps-error', { detail: { kind: 'denied' } }));
            if (cfg.online) {
                // Going online requires a live fix, so a blocked location means offline.
                AF.api(cfg.urls.status, { json: { status: 'Offline' } }).then(function () { paintOnline(false); });
                AF.toast('Location is blocked, so you were set Offline. Allow location in your browser to go online.', 'danger');
            }
        } else if (err && err.code === 3) {
            setChip('searching', 'Weak GPS signal');
            document.dispatchEvent(new CustomEvent('af:gps-error', { detail: { kind: 'timeout' } }));
        } else {
            setChip('unavailable', 'GPS unavailable');
            document.dispatchEvent(new CustomEvent('af:gps-error', { detail: { kind: 'unavailable' } }));
        }
    }
    function startGps() {
        if (!navigator.geolocation) { setChip('unsupported', 'No GPS support'); document.dispatchEvent(new CustomEvent('af:gps-error', { detail: { kind: 'unsupported' } })); return; }
        if (gps.watch !== null) return;
        setChip('searching', 'Finding GPS...');
        gps.watch = navigator.geolocation.watchPosition(onFix, onGpsError, { enableHighAccuracy: true, timeout: 15000, maximumAge: 5000 });
    }
    function stopGps() {
        if (gps.watch !== null && navigator.geolocation) navigator.geolocation.clearWatch(gps.watch);
        if (gps.timer) clearInterval(gps.timer);
        gps.watch = null; gps.timer = null; gps.last = null;
    }
    AF.gps = { latest: function () { return gps.last; }, isLive: function () { return !!gps.last; } };

    function goOffline() {
        return AF.api(cfg.urls.status, { json: { status: 'Offline' } }).then(function (r) {
            if (!r.ok) { AF.toast(r.data.message || 'Could not update your status.', 'danger'); return; }
            stopGps(); setChip('idle', 'GPS off'); paintOnline(false);
            AF.toast('You are offline. Location sharing has stopped.', 'info');
        });
    }
    function goOnline() {
        if (!navigator.geolocation) { AF.toast('This browser has no location support, so you can’t go online.', 'danger'); return Promise.resolve(); }
        setChip('searching', 'Finding GPS...');
        // A real fix is required first - the same rule the dashboard always had.
        return new Promise(function (resolve) {
            navigator.geolocation.getCurrentPosition(function (pos) {
                AF.api(cfg.urls.status, { json: { status: 'Online' } }).then(function (r) {
                    if (!r.ok) { AF.toast(r.data.message || 'Could not update your status.', 'danger'); setChip('idle', 'GPS off'); return resolve(); }
                    paintOnline(true); startGps(); onFix(pos);
                    AF.toast('You are online and can receive deliveries.', 'success'); resolve();
                });
            }, function (err) { onGpsError(err); AF.toast('Allow location access to go online.', 'danger'); resolve(); },
            { enableHighAccuracy: true, timeout: 15000 });
        });
    }
    if (sw) sw.addEventListener('click', function () {
        if (sw.disabled) return;
        sw.disabled = true;
        (cfg.online ? goOffline() : goOnline()).then(function () { sw.disabled = false; });
    });
    if (cfg.online) startGps(); else setChip('idle', 'GPS off');

    /* -------------------------------------------------------------- charts */
    function niceMax(v) {
        if (v <= 0) return 1;
        var pow = Math.pow(10, Math.floor(Math.log10(v))), f = v / pow;
        return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * pow;
    }
    function tipFor(host) {
        var tip = host.querySelector('.chart-tip');
        if (!tip) { tip = document.createElement('div'); tip.className = 'chart-tip'; tip.hidden = true; host.appendChild(tip); }
        return tip;
    }
    /* points: [{label, value, tip}] ; opts: {kind:'bar'|'line', unit, money, empty} */
    AF.chart = function (host, points, opts) {
        opts = opts || {};
        host.innerHTML = '';
        var total = points.reduce(function (s, p) { return s + (p.value || 0); }, 0);
        if (!points.length || total === 0) {
            host.innerHTML = '<div class="chart-empty"><div><i class="fa-regular fa-chart-bar" aria-hidden="true" style="font-size:24px;color:var(--line-strong)"></i><p style="margin-top:8px">' + AF.esc(opts.empty || 'No activity in this period.') + '</p></div></div>';
            return;
        }
        /* opts.fit: draw 1:1 at the box's real width so text never scales and the height never depends on the column */
        var W = (opts.fit && host.clientWidth > 120) ? Math.round(host.clientWidth) : 640, H = opts.height || 240, L = 46, R = 12, T = 14, B = 30;
        var max = niceMax(Math.max.apply(null, points.map(function (p) { return p.value; })));
        if (opts.integer) max = Math.ceil(max / 4) * 4 || 4;   /* counts: whole-number gridlines (0,1,2,3,4 rather than 1,1,1,0,0) */
        var iw = W - L - R, ih = H - T - B, n = points.length;
        var fmt = function (v) { return (opts.money ? '\u20B9' : '') + AF.inr(v); };
        var ns = 'http://www.w3.org/2000/svg';
        var svg = document.createElementNS(ns, 'svg');
        svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
        svg.setAttribute('role', 'img');
        svg.setAttribute('aria-label', opts.label || 'Chart');
        var g = '';
        for (var i = 0; i <= 4; i++) {
            var y = T + ih - (ih * i / 4), v = max * i / 4;
            g += '<line class="gridline" x1="' + L + '" x2="' + (W - R) + '" y1="' + y + '" y2="' + y + '"/>' +
                 '<text class="axis" x="' + (L - 8) + '" y="' + (y + 4) + '" text-anchor="end">' + AF.esc(fmt(v)) + '</text>';
        }
        var step = Math.max(1, Math.ceil(n / (opts.maxLabels || 8))), band = iw / n, xs = [];
        points.forEach(function (p, idx) {
            var cx = L + band * idx + band / 2; xs.push(cx);
            if (idx % step === 0) g += '<text class="axis" x="' + cx + '" y="' + (H - 9) + '" text-anchor="middle">' + AF.esc(p.label) + '</text>';
        });
        var body = '';
        if (opts.kind === 'line') {
            var pts = points.map(function (p, idx) { return xs[idx] + ',' + (T + ih - ih * p.value / max); });
            body += '<polygon class="area" points="' + xs[0] + ',' + (T + ih) + ' ' + pts.join(' ') + ' ' + xs[n - 1] + ',' + (T + ih) + '"/>';
            body += '<polyline class="line" points="' + pts.join(' ') + '"/>';
            points.forEach(function (p, idx) {
                body += '<circle class="dot" tabindex="0" data-i="' + idx + '" cx="' + xs[idx] + '" cy="' + (T + ih - ih * p.value / max) + '" r="4.5"><title>' + AF.esc(p.label + ': ' + fmt(p.value)) + '</title></circle>';
            });
        } else {
            var bw = Math.min(40, band * 0.62);
            points.forEach(function (p, idx) {
                var h = ih * p.value / max;
                body += '<rect class="bar" tabindex="0" data-i="' + idx + '" x="' + (xs[idx] - bw / 2) + '" y="' + (T + ih - Math.max(h, p.value ? 2 : 0)) + '" width="' + bw + '" height="' + Math.max(h, p.value ? 2 : 0) + '" rx="4"><title>' + AF.esc(p.label + ': ' + fmt(p.value)) + '</title></rect>';
            });
        }
        svg.innerHTML = g + body;
        host.appendChild(svg);

        var tip = tipFor(host);
        function show(el) {
            var i = Number(el.getAttribute('data-i')), p = points[i];
            tip.textContent = p.tip || (p.label + ': ' + fmt(p.value));
            var hb = host.getBoundingClientRect(), eb = el.getBoundingClientRect();
            tip.style.left = (eb.left - hb.left + eb.width / 2) + 'px'; tip.style.top = (eb.top - hb.top) + 'px'; tip.hidden = false;
        }
        $$('[data-i]', svg).forEach(function (el) {
            el.addEventListener('mouseenter', function () { show(el); });
            el.addEventListener('focus', function () { show(el); });
            el.addEventListener('mouseleave', function () { tip.hidden = true; });
            el.addEventListener('blur', function () { tip.hidden = true; });
        });
    };
})();