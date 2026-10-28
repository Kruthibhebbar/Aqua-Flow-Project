/* Documents: validates on the client for fast feedback (the server validates
   again and is the authority), uploads with a real progress bar. */
(function () {
    'use strict';
    var AF = window.AF, $ = AF.$, $$ = AF.$$;
    var MAX = 5 * 1024 * 1024, EXT = /\.(pdf|png|jpe?g|webp)$/i;

    function setErr(form, name, msg) {
        var el = form.querySelector('[data-err="' + name + '"]'); if (el) el.textContent = msg || '';
        var input = form.querySelector('[name="' + name + '"]');
        if (input) { var f = input.closest('.field'); if (f) f.classList.toggle('has-error', !!msg); }
    }
    function clearErrs(form) { setErr(form, 'document', ''); setErr(form, 'expiry_date', ''); }

    $$('[data-doc-form]').forEach(function (form) {
        var file = form.querySelector('input[type="file"]'), drop = form.querySelector('.drop'), text = form.querySelector('[data-drop-text]');

        function pick() {
            clearErrs(form);
            var f = file.files[0]; if (!f) return;
            text.textContent = f.name + ' (' + (f.size / 1024 / 1024).toFixed(2) + ' MB)';
            if (!EXT.test(f.name)) setErr(form, 'document', 'Upload a PDF, PNG, JPG or WEBP file.');
            else if (f.size > MAX) setErr(form, 'document', 'File is too large. The limit is 5 MB.');
        }
        file.addEventListener('change', pick);
        drop.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); file.click(); } });
        ['dragenter', 'dragover'].forEach(function (ev) { drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add('is-over'); }); });
        ['dragleave', 'drop'].forEach(function (ev) { drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.remove('is-over'); }); });
        drop.addEventListener('drop', function (e) { if (e.dataTransfer.files.length) { file.files = e.dataTransfer.files; pick(); } });

        form.addEventListener('submit', function (e) {
            e.preventDefault(); clearErrs(form);
            var f = file.files[0];
            if (!f) return setErr(form, 'document', 'Choose a file to upload.');
            if (!EXT.test(f.name)) return setErr(form, 'document', 'Upload a PDF, PNG, JPG or WEBP file.');
            if (f.size > MAX) return setErr(form, 'document', 'File is too large. The limit is 5 MB.');
            var exp = form.querySelector('[name="expiry_date"]');
            if (exp && form.getAttribute('data-type') !== 'vehicle_rc' && !exp.value) return setErr(form, 'expiry_date', 'Enter the expiry date shown on this document.');

            var data = new FormData();
            data.append('document_type', form.getAttribute('data-type'));
            data.append('document', f);
            if (exp) data.append('expiry_date', exp.value);

            var btn = form.querySelector('[type="submit"]'), bar = form.querySelector('.progress-line'), fill = bar.firstElementChild;
            btn.classList.add('is-busy'); bar.hidden = false; fill.style.width = '0';

            var xhr = new XMLHttpRequest();
            xhr.open('POST', '/api/driver/documents/upload');
            xhr.setRequestHeader('X-CSRF-Token', AF.csrf);
            xhr.setRequestHeader('X-Requested-With', 'fetch');
            xhr.setRequestHeader('Accept', 'application/json');
            xhr.upload.onprogress = function (ev) { if (ev.lengthComputable) fill.style.width = Math.round(100 * ev.loaded / ev.total) + '%'; };
            xhr.onload = function () {
                btn.classList.remove('is-busy');
                var res; try { res = JSON.parse(xhr.responseText); } catch (err) { res = null; }
                if (!res) { bar.hidden = true; return AF.toast('Unexpected response from the server (HTTP ' + xhr.status + '). Please try again.', 'danger'); }
                if (xhr.status >= 200 && xhr.status < 300 && res.success) {
                    fill.style.width = '100%';
                    AF.toast(res.message, 'success');
                    return setTimeout(function () { location.reload(); }, 900);
                }
                bar.hidden = true;
                if (res.sessionExpired) return setTimeout(function () { location.href = AF.cfg.urls.login; }, 1200);
                Object.keys(res.errors || {}).forEach(function (k) { setErr(form, k, res.errors[k]); });
                AF.toast(res.message || 'The upload failed.', 'danger');
            };
            xhr.onerror = function () { btn.classList.remove('is-busy'); bar.hidden = true; AF.toast('Could not reach the server. Check your connection and try again.', 'danger'); };
            xhr.send(data);
        });
    });
})();
