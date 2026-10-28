/* Delivery actions shared by the dashboard, My Trips and the details drawer.
   Buttons carry data-af-action + data-id. Every action calls the existing
   server routes; the server re-checks ownership and the status transition, so
   this file only decides which dialogs to show - never what is allowed. */
(function () {
    'use strict';
    var AF = window.AF, $ = AF.$;

    function refreshSoon() { setTimeout(function () { location.reload(); }, 700); }

    function run(btn, request, success) {
        return AF.busy(btn, request).then(function (r) {
            if (r.ok) { AF.toast(r.data.message || success, 'success'); refreshSoon(); }
            else { AF.toast(r.data.message || 'That did not work. Please try again.', 'danger'); }
            return r;
        });
    }

    var actions = {
        accept: function (btn, id) {
            return run(btn, AF.api('/api/accept-booking/' + id, { method: 'POST' }), 'Delivery accepted.');
        },
        decline: function (btn, id) {
            return AF.confirm({
                title: 'Decline this delivery?',
                text: 'It goes back to the admin team to be assigned to another driver. You can’t undo this.',
                confirm: 'Decline delivery', tone: 'danger'
            }).then(function (yes) {
                if (yes) return run(btn, AF.api('/api/reject-booking/' + id, { method: 'POST' }), 'Delivery declined.');
            });
        },
        start: function (btn, id) {
            var go = function () { return run(btn, AF.api('/start-delivery/' + id, { method: 'POST' }), 'Delivery started.'); };
            if (AF.cfg.online) return go();
            return AF.confirm({
                title: 'You are offline',
                text: 'Customers can’t see your live location while you are offline. Start the delivery anyway?',
                confirm: 'Start anyway'
            }).then(function (yes) { if (yes) return go(); });
        },
        arrived: function (btn, id) {
            return AF.confirm({
                title: 'Confirm you have arrived?',
                text: 'The customer is told you are at their location. Then ask them for their delivery OTP.',
                confirm: 'Yes, I have arrived'
            }).then(function (yes) {
                if (yes) return run(btn, AF.api('/arrived-location/' + id, { method: 'POST' }), 'Marked as arrived.');
            });
        },
        'send-otp': function (btn, id) {
            return AF.busy(btn, AF.api('/generate-delivery-otp/' + id, { method: 'POST' })).then(function (r) {
                AF.toast(r.ok ? 'The OTP was emailed to the customer again.' : (r.data.message || 'Could not send the OTP.'), r.ok ? 'success' : 'danger');
            });
        },
        complete: function (btn, id, el) {
            var dlg = $('#af-otp-dialog'); if (!dlg) return;
            dlg.querySelector('[name="booking_id"]').value = id;
            var ref = dlg.querySelector('[data-ref]'); if (ref) ref.textContent = el.getAttribute('data-ref') || '';
            var input = dlg.querySelector('[name="otp"]'); input.value = '';
            var err = dlg.querySelector('.field-error'); err.textContent = '';
            dlg.showModal(); input.focus();
        }
    };

    document.addEventListener('click', function (e) {
        var btn = e.target.closest('[data-af-action]'); if (!btn) return;
        var fn = actions[btn.getAttribute('data-af-action')]; if (!fn) return;
        e.preventDefault();
        fn(btn, btn.getAttribute('data-id'), btn);
    });

    /* OTP completion dialog */
    var form = $('#af-otp-form');
    if (form) {
        var otpInput = form.querySelector('[name="otp"]');
        otpInput.addEventListener('input', function () { otpInput.value = otpInput.value.replace(/\D/g, '').slice(0, 6); });
        form.addEventListener('submit', function (e) {
            e.preventDefault();
            var err = form.querySelector('.field-error'), submit = form.querySelector('[type="submit"]');
            if (!/^\d{6}$/.test(otpInput.value)) { err.textContent = 'Enter the 6-digit code the customer received.'; otpInput.focus(); return; }
            err.textContent = '';
            var data = new FormData(); data.append('otp', otpInput.value);
            AF.busy(submit, AF.api('/verify-delivery-otp/' + form.querySelector('[name="booking_id"]').value, { form: data })).then(function (r) {
                if (r.ok) { $('#af-otp-dialog').close(); AF.toast(r.data.message || 'Delivery completed.', 'success'); refreshSoon(); }
                else { err.textContent = r.data.message || 'That code was not accepted.'; otpInput.select(); }
            });
        });
    }
})();
