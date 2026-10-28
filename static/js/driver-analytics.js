/* Analytics: every figure comes from /api/driver/analytics, scoped to the
   logged-in driver on the server. Nothing here invents or smooths data. */
(function () {
    'use strict';
    var AF = window.AF, $ = AF.$, $$ = AF.$$;
    var form = $('#an-form'), range = $('#an-range');
    var inflight = 0;

    function syncCustom() { $$('[data-custom]').forEach(function (el) { el.hidden = range.value !== 'custom'; }); }
    range.addEventListener('change', syncCustom);

    /* restore from the URL so a filtered view can be bookmarked / reloaded */
    var q = new URLSearchParams(location.search);
    ['range', 'from', 'to', 'bucket'].forEach(function (k) { if (q.get(k) && form.elements[k]) form.elements[k].value = q.get(k); });
    syncCustom();

    function delta(v, label) {
        if (v === null || v === undefined) return '';
        var up = v >= 0;
        return '<span style="color:' + (up ? 'var(--success)' : 'var(--danger)') + '"><i class="fa-solid ' + (up ? 'fa-arrow-trend-up' : 'fa-arrow-trend-down') + '" aria-hidden="true"></i> ' + Math.abs(v) + '%</span> vs previous ' + label;
    }

    /* One KPI card. `cls` goes on the value element itself ('is-money' | 'is-na') so the
       typography rules always match; unavailable metrics render a compact dash, never a fake 0. */
    function item(icon, label, value, note, cls) {
        return '<div class="an-kpi"><div class="an-kpi-label"><i class="fa-solid ' + icon + '" aria-hidden="true"></i><span>' + label + '</span></div>' +
            '<div class="an-kpi-value ' + (cls || '') + '">' + value + '</div><div class="an-kpi-note">' + (note || '') + '</div></div>';
    }
    var na = '<span aria-hidden="true">\u2014</span><span class="af-sr">Not available</span>';
    function compare(v, label) { return v === null || v === undefined ? 'No prior data' : delta(v, label); }
    var last = null;

    function paint(d) {
        var k = d.kpis, days = d.range.days;
        $('#an-range-text').textContent = d.range.from + ' to ' + d.range.to + ' (' + days + ' day' + (days === 1 ? '' : 's') + ', by ' + d.range.bucket + ')';
        var kp = $('#an-kpis'); kp.removeAttribute('aria-busy');
        kp.innerHTML =
            item('fa-circle-check', 'Deliveries', k.deliveries, compare(k.deliveries_delta, 'period')) +
            item('fa-droplet', 'Water delivered', AF.inr(k.litres) + '<small>L</small>', 'Litres this period') +
            item('fa-indian-rupee-sign', 'Earnings', '\u20B9' + AF.inr(k.earnings), compare(k.earnings_delta, 'period'), 'is-money') +
            (k.completion_rate === null
                ? item('fa-flag-checkered', 'Completion rate', na, 'No finished orders', 'is-na')
                : item('fa-flag-checkered', 'Completion rate', k.completion_rate + '%', 'Of scheduled orders')) +
            item('fa-ban', 'Cancelled', k.cancelled, k.cancellation_rate === null ? 'No finished orders' : k.cancellation_rate + '% of finished orders') +
            (k.avg_delivery_minutes === null
                ? item('fa-stopwatch', 'Average trip time', na, 'No timed trips yet', 'is-na')
                : item('fa-stopwatch', 'Average trip time', k.avg_delivery_minutes + '<small>min</small>', 'From ' + k.avg_delivery_samples + ' timed trip' + (k.avg_delivery_samples === 1 ? '' : 's')));

        last = d;
        $('#an-export').hidden = d.empty || !d.series.length;

        $('#an-empty').hidden = !d.empty;
        $('#an-charts').hidden = d.empty;
        if (d.empty) return;

        var s = d.series;
        $('#ch-deliveries-sub').textContent = 'By ' + d.range.bucket;
        drawDeliveries();
        AF.chart($('#ch-earnings'), s.map(function (p) { return { label: p.label, value: p.earnings }; }),
            { kind: 'line', money: true, label: 'Earnings', empty: 'No earnings in this period.' });
        AF.chart($('#ch-litres'), s.map(function (p) { return { label: p.label, value: p.litres, tip: p.label + ': ' + AF.inr(p.litres) + ' L' }; }),
            { kind: 'bar', label: 'Water delivered', empty: 'No water delivered in this period.' });

        var rh = $('#ch-rating');
        if (d.ratings.length) AF.chart(rh, d.ratings.map(function (p) { return { label: p.label, value: p.rating, tip: p.label + ': ' + p.rating + ' / 5' }; }), { kind: 'line', label: 'Average customer rating' });
        else rh.innerHTML = '<div class="chart-empty"><div><i class="fa-regular fa-star" aria-hidden="true" style="font-size:24px;color:var(--line-strong)"></i><p style="margin-top:8px">No customer ratings have been recorded yet, so there is no trend to show.</p></div></div>';

        $('#an-table tbody').innerHTML = s.map(function (p) {
            return '<tr><td>' + AF.esc(p.label) + '</td><td class="num">' + p.deliveries + '</td><td class="num">' + AF.inr(p.litres) + '</td><td class="num">\u20B9' + AF.inr(p.earnings) + '</td></tr>';
        }).join('');
    }

    /* the wide chart is drawn 1:1 at its real width, so redraw when the box resizes */
    function drawDeliveries() {
        if (!last || last.empty) return;
        AF.chart($('#ch-deliveries'), last.series.map(function (p) { return { label: p.label, value: p.deliveries, tip: p.label + ': ' + p.deliveries + ' deliver' + (p.deliveries === 1 ? 'y' : 'ies') }; }),
            { kind: 'bar', integer: true, fit: true, maxLabels: $('#ch-deliveries').clientWidth < 480 ? 4 : 8, height: 260, label: 'Deliveries completed', empty: 'No deliveries in this period.' });
    }
    var resizeTimer, lastW = 0;
    window.addEventListener('resize', function () {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(function () {
            var w = $('#ch-deliveries').clientWidth;
            if (w && w !== lastW) { lastW = w; drawDeliveries(); }
        }, 150);
    });

    /* CSV of exactly what the charts show (client-side, from the API response) */
    $('#an-export').addEventListener('click', function () {
        if (!last || last.empty) return;
        var rows = [['Period', 'Deliveries', 'Water (L)', 'Earnings (INR)']].concat(last.series.map(function (p) { return [p.label, p.deliveries, p.litres, p.earnings]; }));
        var csv = rows.map(function (r) { return r.map(function (c) { c = String(c); return /[",\n]/.test(c) ? '"' + c.replace(/"/g, '""') + '"' : c; }).join(','); }).join('\r\n');
        var a = document.createElement('a');
        a.href = URL.createObjectURL(new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8' }));
        a.download = 'aquaflow-analytics-' + last.range.from + '_to_' + last.range.to + '.csv';
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
    });

    function load() {
        var params = new URLSearchParams();
        params.set('range', range.value);
        if (range.value === 'custom') { params.set('from', form.elements.from.value); params.set('to', form.elements.to.value); }
        if (form.elements.bucket.value) params.set('bucket', form.elements.bucket.value);

        var token = ++inflight;
        $('#an-error').hidden = true;
        $('#an-kpis').setAttribute('aria-busy', 'true');
        history.replaceState(null, '', location.pathname + '?' + params.toString());

        AF.api('/api/driver/analytics?' + params.toString()).then(function (r) {
            if (token !== inflight) return;           // a newer request replaced this one
            if (!r.ok) {
                $('#an-error-text').textContent = r.data.message || 'We couldn’t load your analytics.';
                $('#an-error').hidden = false;
                $('#an-kpis').removeAttribute('aria-busy');
                return;
            }
            paint(r.data);
        });
    }

    form.addEventListener('submit', function (e) { e.preventDefault(); load(); });
    $('#an-retry').addEventListener('click', load);
    load();
})();