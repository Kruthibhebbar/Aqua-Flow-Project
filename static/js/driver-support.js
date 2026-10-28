/* Help & Support. A request is only reported as sent after the server saved it. */
(function () {
    'use strict';
    var AF = window.AF, $ = AF.$;

    var form = $('#sp-form'), msg = form.elements.message, count = $('#sp-count');
    msg.addEventListener('input', function () { count.textContent = msg.value.length + ' / 1000'; });

    form.addEventListener('submit', function (e) {
        e.preventDefault();
        var body = {}; Array.prototype.forEach.call(form.elements, function (el) { if (el.name) body[el.name] = el.value; });
        var local = {};
        if (!body.category) local.category = 'Choose what this is about.';
        if (body.subject.trim().length < 4) local.subject = 'Give it a short subject.';
        if (body.message.trim().length < 10) local.message = 'Describe the problem in at least 10 characters.';
        if (Object.keys(local).length) return AF.showFieldErrors(form, local);

        AF.busy(form.querySelector('[type="submit"]'), AF.api('/api/driver/support', { json: body })).then(function (r) {
            if (!r.ok) { AF.showFieldErrors(form, r.data.errors || {}); return AF.toast(r.data.message || 'We couldn’t send that.', 'danger'); }
            AF.showFieldErrors(form, {}); form.reset(); count.textContent = '0 / 1000';
            AF.toast(r.data.message, 'success');
            setTimeout(function () { location.reload(); }, 1400);
        });
    });

    var sos = $('#sp-sos-form');
    sos.addEventListener('submit', function (e) {
        e.preventDefault();
        var err = $('#sos-err'); err.textContent = '';
        AF.busy(sos.querySelector('[type="submit"]'), AF.api('/api/driver/support/emergency', { json: { message: sos.elements.message.value } })).then(function (r) {
            if (!r.ok) { err.textContent = r.data.message || 'The alert was not recorded.'; return; }
            $('#sp-sos-dialog').close(); sos.reset();
            AF.toast(r.data.message, 'warning', 12000);
            setTimeout(function () { location.reload(); }, 2500);
        });
    });

    /* ---- Request conversation dialog (opens from "Your recent requests") ---- */
    var data = {}, current = null;
    try { JSON.parse(($('#sp-data') || {}).textContent || '[]').forEach(function (r) { data[r.id] = r; }); } catch (e) { data = {}; }
    var dlg = $('#sp-req-dialog'), thread = $('#spr-thread'), rform = $('#spr-form'), rmsg = $('#spr-msg'), rerr = $('#spr-err');

    function el(tag, cls, text) { var n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; }
    function bubble(who, name, when, text) {
        var b = el('div', 'sp-bub ' + who); var h = el('div', 'sp-bub-h');
        h.appendChild(el('b', null, name)); h.appendChild(el('span', null, when)); b.appendChild(h); b.appendChild(el('p', null, text)); return b;
    }
    function statusTag(st) {
        var label = st === 'Answered' ? 'Replied' : st, tone = st === 'Resolved' ? 'success' : (st === 'Answered' ? 'info' : 'warning');
        return el('span', 'tag tone-' + tone, label);
    }
    function paint(r) {
        current = r; data[r.id] = r;
        $('#spr-title').textContent = r.subject;
        var tags = $('#spr-tags'); tags.textContent = '';
        tags.appendChild(el('span', 'sp-ticket', r.ticket)); tags.appendChild(el('span', 'tag' + (r.emergency ? ' tone-danger' : ' tone-accent'), r.category_label));
        tags.appendChild(statusTag(r.status));
        if (r.booking) tags.appendChild(el('span', 'tag', 'Booking ' + r.booking));
        thread.textContent = '';
        thread.appendChild(bubble('me', 'You', r.when, r.message));
        r.replies.forEach(function (m) { thread.appendChild(bubble(m.by === 'admin' ? 'team' : 'me', m.by === 'admin' ? m.name : 'You', m.when, m.text)); });
        if (!r.replies.length && !r.emergency) {
            var w = el('div', 'sp-wait'); w.appendChild(el('i', 'fa-solid fa-hourglass-half')); w.appendChild(el('span', null, 'Waiting for the AquaFlow team. Their reply will appear here and in your notifications.')); thread.appendChild(w);
        }
        rform.hidden = !!r.emergency;
        rerr.textContent = '';
        rmsg.placeholder = r.status === 'Resolved' ? 'Still need help? Replying will reopen this request.' : 'Write a follow-up to the support team...';
        setTimeout(function () { thread.scrollTop = thread.scrollHeight; }, 30);
    }
    function openReq(id) { var r = data[id]; if (!r || !dlg.showModal) return; paint(r); rmsg.value = ''; $('#spr-count').textContent = '0 / 1000'; if (!dlg.open) dlg.showModal(); }

    document.addEventListener('click', function (e) {
        var b = e.target.closest('[data-sp-open]'); if (b) openReq(b.getAttribute('data-sp-open'));
    });
    if (rmsg) rmsg.addEventListener('input', function () { $('#spr-count').textContent = rmsg.value.length + ' / 1000'; });
    if (rform) rform.addEventListener('submit', function (e) {
        e.preventDefault(); if (!current) return;
        var text = rmsg.value.trim(); rerr.textContent = '';
        if (text.length < 2) { rerr.textContent = 'Write a reply of at least 2 characters.'; return; }
        AF.busy(rform.querySelector('[type="submit"]'), AF.api('/api/driver/support/' + current.id + '/reply', { json: { message: text } })).then(function (r) {
            if (!r.ok) { rerr.textContent = r.data.message || 'Your reply was not sent.'; return; }
            rmsg.value = ''; $('#spr-count').textContent = '0 / 1000'; paint(r.data.request);
            AF.toast(r.data.message, 'success');
            var row = document.querySelector('[data-sp-open="' + current.id + '"] .tag'); if (row) row.replaceWith(statusTag(current.status));
        });
    });
    if (window.SP_OPEN_TICKET) {
        Object.keys(data).some(function (id) { if (data[id].ticket === window.SP_OPEN_TICKET) { openReq(id); return true; } });
    }
})();
