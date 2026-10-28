/* Earnings: real-data chart and the "report cash sent" dialog. */
(function () {
    'use strict';
    var AF = window.AF, $ = AF.$;

    var chart = JSON.parse($('#af-earn-data').textContent);
    var host = $('#af-earn-chart');
    function draw() {
        AF.chart(host, chart.series.map(function (p) {
            return { label: p.label, value: p.earnings, tip: p.label + ': \u20B9' + AF.inr(p.earnings) + ' from ' + p.deliveries + ' deliver' + (p.deliveries === 1 ? 'y' : 'ies') };
        }), { kind: 'bar', money: true, height: 240, fit: true, label: 'Earnings over time', empty: 'No earnings in this period.' });
    }
    /* drawn 1:1 at the card's real width so the height never depends on the column; redrawn when the width changes */
    if (host) {
        draw();
        if (window.ResizeObserver) {
            var drawnAt = host.clientWidth, timer;
            new ResizeObserver(function () {
                clearTimeout(timer);
                timer = setTimeout(function () {
                    if (Math.abs(host.clientWidth - drawnAt) > 2) { drawnAt = host.clientWidth; draw(); }
                }, 120);
            }).observe(host);
        }
    }

    /* Summary cards: a very large amount shrinks a little instead of breaking over two lines
       (CSS alone cannot size text to its own length). Normal amounts keep the standard size. */
    var kpiBox = $('.earn-kpis');
    function fitKpis() {
        Array.prototype.forEach.call(document.querySelectorAll('.earn-kpi-value'), function (el) {
            el.style.fontSize = ''; el.style.whiteSpace = 'nowrap';
            var size = parseFloat(getComputedStyle(el).fontSize);
            while (el.scrollWidth > el.clientWidth + 0.5 && size > 13) { size -= 0.5; el.style.fontSize = size + 'px'; }
            if (el.scrollWidth > el.clientWidth + 0.5) el.style.whiteSpace = '';   /* cannot fit even at the minimum: let it wrap */
        });
    }
    if (kpiBox) {
        fitKpis();
        if (window.ResizeObserver) {
            var kpiWidth = kpiBox.clientWidth, kpiTimer;
            new ResizeObserver(function () {
                clearTimeout(kpiTimer);
                kpiTimer = setTimeout(function () {
                    if (Math.abs(kpiBox.clientWidth - kpiWidth) > 2) { kpiWidth = kpiBox.clientWidth; fitKpis(); }
                }, 120);
            }).observe(kpiBox);
        }
    }

    /* First row of cards ends exactly at the bottom of the first screen, so the page no longer looks finished
       there. Only on the two-column layout (>= 920px wide); stacked cards keep their natural height. */
    var firstRow = document.querySelector('.earn-rows > .earn-row:first-child'), host2 = document.querySelector('.af-earn');
    function fitFirstRow() {
        if (!firstRow || !host2) return;
        firstRow.style.removeProperty('--earn-first-h');
        if (host2.clientWidth < 920) return;
        var top = firstRow.getBoundingClientRect().top + window.pageYOffset;
        var h = Math.round(window.innerHeight - top);
        if (h > 0) firstRow.style.setProperty('--earn-first-h', h + 'px');
    }
    fitFirstRow();
    window.addEventListener('resize', fitFirstRow);
    window.addEventListener('load', fitFirstRow);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(fitFirstRow);
    if (window.ResizeObserver && kpiBox) new ResizeObserver(fitFirstRow).observe(kpiBox);

    var dlg = $('#af-cash-dialog'), form = $('#af-cash-form');
    if (!dlg) return;

    document.addEventListener('click', function (e) {
        var b = e.target.closest('[data-cash]'); if (!b) return;
        form.reset();
        form.querySelector('[name="booking_id"]').value = b.getAttribute('data-cash');
        form.querySelector('[name="amount"]').value = b.getAttribute('data-amount');
        dlg.querySelector('[data-ref]').textContent = b.getAttribute('data-ref');
        AF.showFieldErrors(form, {});
        $('#c-form-error').textContent = '';
        dlg.showModal();
    });

    form.addEventListener('submit', function (e) {
        e.preventDefault();
        var ref = form.querySelector('[name="transaction_ref"]').value.trim();
        if (!ref) { AF.showFieldErrors(form, { transaction_ref: 'Enter the UPI or bank transaction reference.' }); return; }
        var amount = parseFloat(form.querySelector('[name="amount"]').value);
        if (!(amount >= 0)) { AF.showFieldErrors(form, { amount: 'Enter the amount you sent.' }); return; }

        var shot = form.querySelector('[name="screenshot"]').files[0];
        if (shot && shot.size > 5 * 1024 * 1024) { $('#c-form-error').textContent = 'The screenshot is larger than 5 MB.'; return; }

        var data = new FormData(form); data.delete('booking_id');
        var submit = form.querySelector('[type="submit"]');
        AF.busy(submit, AF.api('/api/submit-collected-payment/' + encodeURIComponent(form.querySelector('[name="booking_id"]').value), { form: data })).then(function (r) {
            if (r.ok) {
                dlg.close();
                AF.toast(r.data.message || 'Submitted. An admin will check it against the bank statement.', 'success');
                setTimeout(function () { location.reload(); }, 900);
            } else {
                $('#c-form-error').textContent = r.data.message || 'The submission was not accepted.';
            }
        });
    });
})();
