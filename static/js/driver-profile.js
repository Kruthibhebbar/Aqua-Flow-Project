/* Profile: details form, photo upload with preview, password change.
   Success is only shown after the server confirms the save. */
(function () {
    'use strict';
    var AF = window.AF, $ = AF.$;

    /* ----------------------------------------------------------- details */
    var form = $('#pf-form'), save = $('#pf-save'), cancel = $('#pf-cancel');
    var initial = {};
    function snapshot() { Array.prototype.forEach.call(form.elements, function (el) { if (el.name) initial[el.name] = el.value; }); }
    function dirty() { return Array.prototype.some.call(form.elements, function (el) { return el.name && initial[el.name] !== el.value; }); }
    snapshot();
    form.addEventListener('input', function () { save.disabled = !dirty(); });
    cancel.addEventListener('click', function () {
        Array.prototype.forEach.call(form.elements, function (el) { if (el.name) el.value = initial[el.name]; });
        AF.showFieldErrors(form, {}); save.disabled = true;
    });
    form.addEventListener('submit', function (e) {
        e.preventDefault();
        var body = {}; Array.prototype.forEach.call(form.elements, function (el) { if (el.name) body[el.name] = el.value; });
        AF.busy(save, AF.api('/api/driver/profile', { json: body })).then(function (r) {
            if (!r.ok) {
                AF.showFieldErrors(form, r.data.errors || {});
                return AF.toast(r.data.message || 'We couldn’t save your changes.', 'danger');
            }
            AF.showFieldErrors(form, {});
            var p = r.data.profile || {};                  // what the server actually stored
            Object.keys(p).forEach(function (k) { if (form.elements[k]) form.elements[k].value = p[k] == null ? '' : p[k]; });
            snapshot(); save.disabled = true;
            AF.toast(r.data.message || 'Profile saved.', 'success');
        });
    });
    window.addEventListener('beforeunload', function (e) { if (dirty()) { e.preventDefault(); e.returnValue = ''; } });

    /* ------------------------------------------------------------- photo */
    var input = $('#pf-photo-input'), img = $('#pf-photo'), initials = $('#pf-initials');
    var bar = $('#pf-photo-actions'), err = $('#pf-photo-err'), original = img.getAttribute('src'), picked = null;
    $('#pf-photo-btn').addEventListener('click', function () { input.click(); });
    input.addEventListener('change', function () {
        err.textContent = ''; picked = input.files[0]; if (!picked) return;
        if (!/\.(png|jpe?g|webp)$/i.test(picked.name)) { err.textContent = 'Choose a PNG, JPG or WEBP image.'; bar.hidden = false; picked = null; return; }
        if (picked.size > 5 * 1024 * 1024) { err.textContent = 'That image is larger than 5 MB.'; bar.hidden = false; picked = null; return; }
        img.src = URL.createObjectURL(picked); img.hidden = false; if (initials) initials.hidden = true; bar.hidden = false;
    });
    $('#pf-photo-cancel').addEventListener('click', function () {
        picked = null; input.value = ''; err.textContent = ''; bar.hidden = true;
        if (original) img.src = original; else { img.hidden = true; if (initials) initials.hidden = false; }
    });
    $('#pf-photo-save').addEventListener('click', function () {
        if (!picked) return;
        var data = new FormData(); data.append('photo', picked);
        AF.busy($('#pf-photo-save'), AF.api('/api/driver/profile/photo', { form: data })).then(function (r) {
            if (!r.ok) { err.textContent = r.data.message || 'The photo wasn’t saved.'; return; }
            original = r.data.photo_url; img.src = original + '?t=' + Date.now(); bar.hidden = true; picked = null; input.value = '';
            var side = document.querySelector('.af-me .af-avatar');
            if (side && side.tagName === 'IMG') side.src = original; else if (side) { var n = document.createElement('img'); n.className = 'af-avatar'; n.src = original; n.alt = ''; side.replaceWith(n); }
            AF.toast(r.data.message || 'Photo updated.', 'success');
        });
    });

    /* ---------------------------------------------------------- password */
    var pw = $('#pw-form');
    pw.addEventListener('submit', function (e) {
        e.preventDefault();
        var body = {}; Array.prototype.forEach.call(pw.elements, function (el) { if (el.name) body[el.name] = el.value; });
        AF.busy(pw.querySelector('[type="submit"]'), AF.api('/api/driver/password', { json: body })).then(function (r) {
            if (!r.ok) { AF.showFieldErrors(pw, r.data.errors || {}); return AF.toast(r.data.message || 'Password not changed.', 'danger'); }
            AF.showFieldErrors(pw, {}); pw.reset(); AF.toast('Password changed.', 'success');
        });
    });
})();
