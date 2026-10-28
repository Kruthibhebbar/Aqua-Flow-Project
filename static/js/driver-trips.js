/* My Trips: details drawer. Data comes from /api/driver/trips/<id>, which only
   returns a trip that belongs to the logged-in driver. */
(function () {
    'use strict';
    var AF = window.AF, $ = AF.$;
    var drawer = $('#af-trip-drawer'), body = $('#af-trip-body'), title = $('#af-trip-title'), sub = $('#af-trip-sub');
    if (!drawer) return;

    var TONE = { success: 'tone-success', warning: 'tone-warning', danger: 'tone-danger', info: 'tone-info', primary: 'tone-primary', accent: 'tone-accent', muted: '' };

    function button(action, t) {
        var id = AF.esc(t.id), ref = AF.esc(t.ref);
        return {
            accept: '<button type="button" class="btn btn-primary" data-af-action="accept" data-id="' + id + '">Accept</button>',
            decline: '<button type="button" class="btn btn-danger" data-af-action="decline" data-id="' + id + '">Decline</button>',
            start: '<button type="button" class="btn btn-aqua" data-af-action="start" data-id="' + id + '">Start delivery</button>',
            arrived: '<button type="button" class="btn" data-af-action="arrived" data-id="' + id + '">Mark arrived</button>',
            complete: '<button type="button" class="btn btn-aqua" data-af-action="complete" data-id="' + id + '" data-ref="' + ref + '">Complete with OTP</button>',
            submit_cash: '<a class="btn" href="/driver-earnings#cash-due">Submit cash</a>'
        }[action] || '';
    }

    function row(label, value) { return '<dt>' + AF.esc(label) + '</dt><dd>' + value + '</dd>'; }

    function render(t) {
        title.textContent = 'Trip ' + t.ref;
        sub.innerHTML = '<span class="tag ' + (TONE[t.tone] || '') + '">' + AF.esc(t.status_label) + '</span>';
        var facts = '';
        facts += row('Delivery address', AF.esc(t.address || 'Not provided'));
        facts += row('Scheduled', AF.esc((t.date ? new Date(t.date + 'T00:00:00').toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' }) : 'Not set') + (t.time ? ' at ' + t.time : '')));
        facts += row('Water', AF.esc(AF.inr(t.litres) + ' L, ' + t.water_type));
        if (t.vehicle) facts += row('Vehicle', AF.esc(t.vehicle));
        if (t.customer_name) facts += row('Customer', AF.esc(t.customer_name));
        if (t.customer_phone) facts += row('Phone', '<a href="tel:' + AF.esc(t.customer_phone) + '">' + AF.esc(t.customer_phone) + '</a>');
        facts += row('Delivery fee', '&#8377;' + AF.inr(t.fee));
        facts += row('Your earnings', '&#8377;' + AF.inr(t.share) + ' <span class="muted small">(platform commission &#8377;' + AF.inr(t.commission) + ')</span>');
        facts += row('Payment', t.paid_online ? 'Paid online, nothing to collect' : (t.collect_cash ? 'Collect &#8377;' + AF.inr(t.fee) + ' in cash' : AF.esc(t.payment_method)));
        if (t.cash_label) facts += row('Cash to company', '<span class="tag ' + (TONE[t.cash_tone] || '') + '">' + AF.esc(t.cash_label) + '</span>');

        var steps = t.timeline.map(function (s) {
            return '<li style="display:flex;gap:10px;align-items:center"><i class="fa-solid ' + (s.at ? 'fa-circle-check ok' : 'fa-circle todo') + '" style="' + (s.at ? 'color:var(--success)' : 'color:var(--line-strong)') + '" aria-hidden="true"></i><span' + (s.at ? '' : ' class="muted"') + '>' + AF.esc(s.label) + '</span><span class="muted small" style="margin-left:auto">' + (s.at ? AF.esc(s.at) : '') + '</span></li>';
        }).join('');

        var actions = (t.actions || []).map(function (a) { return button(a, t); }).join('');
        body.innerHTML = '<dl class="kv">' + facts + '</dl>' +
            '<h3 style="margin:22px 0 10px;font-size:15px">Progress</h3><ul class="checklist">' + steps + '</ul>' +
            (actions ? '<div class="row" style="margin-top:22px">' + actions + '</div>' : '');
    }

    function open(id) {
        title.textContent = 'Trip details'; sub.textContent = '';
        body.innerHTML = '<div class="skeleton" style="height:20px;margin-bottom:12px"></div><div class="skeleton" style="height:20px;margin-bottom:12px;width:70%"></div><div class="skeleton" style="height:20px;width:85%"></div>';
        drawer.showModal();
        AF.api('/api/driver/trips/' + encodeURIComponent(id)).then(function (r) {
            if (r.ok && r.data.trip) return render(r.data.trip);
            body.innerHTML = '<div class="empty compact"><i class="fa-solid fa-circle-exclamation" aria-hidden="true"></i><h3>Couldn’t load this trip</h3><p>' + AF.esc(r.data.message || 'Please try again.') + '</p></div>';
        });
    }

    document.addEventListener('click', function (e) {
        var b = e.target.closest('[data-trip-details]');
        if (b) { e.preventDefault(); open(b.getAttribute('data-trip-details')); }
    });
    // Actions started from inside the drawer close it first so a result toast isn't hidden behind it.
    drawer.addEventListener('click', function (e) {
        var a = e.target.closest('[data-af-action]');
        if (a && a.getAttribute('data-af-action') === 'complete') drawer.close();
    });
})();
