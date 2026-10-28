"""
driver_documents.py
=====================================================
The driver Documents Centre: upload, replace, view and (admin side)
review of a driver's compliance documents.

HOW THIS DIFFERS FROM THE OLD UPLOAD
------------------------------------
* Files are stored OUTSIDE /static (in ``private_uploads/driver_documents``)
  under a random server-generated name, and are only ever served through
  the ownership-checked routes below. Nothing a driver uploads is
  reachable by guessing a public URL.
* Uploads are validated on the server: extension allow-list, declared
  MIME type, file signature (a renamed .exe is rejected, images must
  decode with Pillow), and a size limit.
* Status values follow what the admin side already understands:
  "Pending Review" -> "Verified" / "Rejected". "Expired" is never
  stored - it is derived from the expiry date the driver entered.
* An admin can now actually review a document (approve / reject with a
  reason). Nothing is ever shown as Verified unless an admin did that.
=====================================================
"""

import logging
import os
import uuid
from datetime import date, datetime, timedelta

from bson.objectid import ObjectId
from flask import (
    abort,
    jsonify,
    redirect,
    request,
    send_file,
    session,
    url_for,
    flash,
)
from werkzeug.utils import secure_filename

from image_utils import compress_image_file

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# What a driver can upload. The first three are the documents the platform
# already required (and the admin driver card already counts); insurance is
# an optional extra and does not change any existing "x of 3 verified" count.
# ---------------------------------------------------------------------------
DOCUMENT_TYPES = {
    "driving_license": {
        "label": "Driving licence",
        "icon": "fa-id-card",
        "required": True,
        "expiry": "required",
        "help": "Front side of your valid driving licence. Enter the expiry date printed on it.",
    },
    "aadhaar": {
        "label": "Aadhaar card",
        "icon": "fa-fingerprint",
        "required": True,
        "expiry": None,
        "help": "Identity proof. A clear photo or scan of the front of your Aadhaar card.",
    },
    "vehicle_rc": {
        "label": "Vehicle registration (RC)",
        "icon": "fa-truck-front",
        "required": True,
        "expiry": "optional",
        "help": "Registration certificate of the tanker. Add the validity date if it shows one.",
    },
    "vehicle_insurance": {
        "label": "Vehicle insurance",
        "icon": "fa-shield-halved",
        "required": False,
        "expiry": "required",
        "help": "Optional. Current insurance certificate for your tanker, with its expiry date.",
    },
}

ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
ALLOWED_MIME = {
    ".pdf": {"application/pdf"},
    ".png": {"image/png"},
    ".jpg": {"image/jpeg", "image/pjpeg"},
    ".jpeg": {"image/jpeg", "image/pjpeg"},
    ".webp": {"image/webp"},
}
MAX_DOCUMENT_BYTES = 5 * 1024 * 1024          # 5 MB
EXPIRY_WARNING_DAYS = 30
REJECTION_REASON_MAX = 200

# Legacy uploads (before this module) lived under /static and are still
# referenced by file_path in the database - they are resolved safely below
# and their public URL is blocked.
LEGACY_PUBLIC_PREFIX = "/static/uploads/driver_documents/"


# =====================================================
# PURE HELPERS (no Flask request context needed)
# =====================================================

def project_root(app):
    """Folder that contains Backend/, static/ and templates/."""
    return os.path.abspath(os.path.join(app.root_path, os.pardir))


def private_documents_dir(app):
    configured = app.config.get("DRIVER_DOCUMENTS_DIR")
    base = configured or os.path.join(project_root(app), "private_uploads", "driver_documents")
    return os.path.abspath(base)


def parse_date(value):
    """'YYYY-MM-DD' (or a date/datetime) -> date, else None."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def _lower_first(text):
    """'Vehicle registration (RC)' -> 'vehicle registration (RC)' (keeps acronyms intact)."""
    return text[:1].lower() + text[1:]


def human_size(num_bytes):
    try:
        size = float(num_bytes or 0)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB"):
        if size < 1024 or unit == "MB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def _file_size(file_storage):
    stream = file_storage.stream
    position = stream.tell()
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(position)
    return size


def _looks_like_declared_type(file_storage, ext):
    """Check the real bytes, not just the filename / declared MIME."""
    stream = file_storage.stream
    stream.seek(0)
    head = stream.read(16)
    stream.seek(0)
    if ext == ".pdf":
        return head.startswith(b"%PDF-")
    try:
        from PIL import Image
        with Image.open(stream) as img:
            fmt = (img.format or "").upper()
            img.verify()
    except Exception:
        return False
    finally:
        stream.seek(0)
    expected = {".png": {"PNG"}, ".jpg": {"JPEG"}, ".jpeg": {"JPEG"}, ".webp": {"WEBP"}}[ext]
    return fmt in expected


def validate_document_upload(file_storage, document_type, expiry_raw, today=None):
    """
    Validate everything about an upload. Returns (clean, errors) where
    ``errors`` maps a field name to a message the UI shows beside that field.
    """
    today = today or date.today()
    errors = {}
    clean = {}

    spec = DOCUMENT_TYPES.get(document_type)
    if not spec:
        errors["document_type"] = "Choose which document you are uploading."

    expiry_date = None
    if spec:
        expiry_raw = (expiry_raw or "").strip()
        if spec["expiry"] == "required" and not expiry_raw:
            errors["expiry_date"] = "Enter the expiry date shown on this document."
        elif expiry_raw:
            if spec["expiry"] is None:
                expiry_raw = ""   # this type has no expiry; ignore anything sent
            else:
                expiry_date = parse_date(expiry_raw)
                if expiry_date is None:
                    errors["expiry_date"] = "Enter the date as YYYY-MM-DD."
                elif expiry_date < today:
                    errors["expiry_date"] = "This date is in the past - upload a current document."
                elif expiry_date > today + timedelta(days=365 * 30):
                    errors["expiry_date"] = "That expiry date looks wrong."

    if file_storage is None or not getattr(file_storage, "filename", ""):
        errors["document"] = "Choose a file to upload."
    else:
        ext = os.path.splitext(file_storage.filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            errors["document"] = "Upload a PDF, PNG, JPG or WEBP file."
        else:
            declared = (file_storage.mimetype or "").lower()
            size = _file_size(file_storage)
            if declared and declared != "application/octet-stream" and declared not in ALLOWED_MIME[ext]:
                errors["document"] = "The file type doesn't match its extension."
            elif size <= 0:
                errors["document"] = "That file is empty."
            elif size > MAX_DOCUMENT_BYTES:
                errors["document"] = f"File is too large ({human_size(size)}). The limit is {human_size(MAX_DOCUMENT_BYTES)}."
            elif not _looks_like_declared_type(file_storage, ext):
                errors["document"] = "That file is damaged or isn't a real " + ("PDF." if ext == ".pdf" else "image.")
            else:
                clean.update({"ext": ext, "size": size,
                              "mime": sorted(ALLOWED_MIME[ext])[0]})

    if errors:
        return None, errors

    clean["document_type"] = document_type
    clean["expiry_date"] = expiry_date.isoformat() if expiry_date else None
    return clean, {}


MAX_PHOTO_BYTES = 5 * 1024 * 1024
PHOTO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def validate_image_upload(file_storage, max_bytes=MAX_PHOTO_BYTES):
    """
    Validate a profile-photo upload the same way documents are validated:
    extension, declared MIME, size and a real decode with Pillow.
    Returns (extension, None) on success or (None, message).
    """
    if file_storage is None or not getattr(file_storage, "filename", ""):
        return None, "Choose an image to upload."
    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in PHOTO_EXTENSIONS:
        return None, "Upload a PNG, JPG or WEBP image."
    declared = (file_storage.mimetype or "").lower()
    if declared and declared != "application/octet-stream" and declared not in ALLOWED_MIME[ext]:
        return None, "The file type doesn't match its extension."
    size = _file_size(file_storage)
    if size <= 0:
        return None, "That file is empty."
    if size > max_bytes:
        return None, f"Image is too large ({human_size(size)}). The limit is {human_size(max_bytes)}."
    if not _looks_like_declared_type(file_storage, ext):
        return None, "That file is damaged or isn't a real image."
    return ext, None


def store_private_file(app, file_storage, ext):
    """Save under a random name in the private folder. Returns (stored_name, relative_path)."""
    directory = private_documents_dir(app)
    os.makedirs(directory, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}{ext}"
    absolute = os.path.join(directory, stored_name)
    file_storage.stream.seek(0)
    file_storage.save(absolute)
    if ext != ".pdf":
        compress_image_file(absolute)   # safe no-op on failure
    relative = os.path.relpath(absolute, project_root(app)).replace(os.sep, "/")
    return stored_name, relative


def resolve_document_path(app, doc):
    """
    Absolute path of a document's file, but ONLY if it sits inside one of the
    two folders documents are allowed to live in. A tampered database value
    like ``../../.env`` can therefore never be served.
    """
    stored = (doc or {}).get("file_path")
    if not stored or not isinstance(stored, str):
        return None
    root = project_root(app)
    candidates = [os.path.abspath(os.path.join(root, stored))]
    if not os.path.isabs(stored):
        candidates.append(os.path.abspath(os.path.join(os.getcwd(), stored)))

    allowed_roots = [
        private_documents_dir(app),
        os.path.abspath(os.path.join(root, "static", "uploads", "driver_documents")),
    ]
    for candidate in candidates:
        for allowed in allowed_roots:
            if os.path.commonpath([candidate, allowed]) == allowed and os.path.isfile(candidate):
                return candidate
    return None


def effective_status(doc, today=None):
    today = today or date.today()
    expiry = parse_date(doc.get("expiry_date"))
    if expiry and expiry < today:
        return "Expired"
    status = doc.get("status") or "Pending Review"
    return "Pending Review" if status in ("Pending", "Pending Review") else status


def document_view_model(app, doc, today=None):
    today = today or date.today()
    expiry = parse_date(doc.get("expiry_date"))
    days_left = (expiry - today).days if expiry else None
    doc_id = str(doc["_id"])
    has_file = resolve_document_path(app, doc) is not None
    return {
        "id": doc_id,
        "type": doc.get("document_type"),
        "status": effective_status(doc, today),
        "stored_status": doc.get("status") or "Pending Review",
        "uploaded_at": doc.get("uploaded_at"),
        "expiry_date": expiry,
        "days_left": days_left,
        "expired": bool(expiry and expiry < today),
        "expiring_soon": bool(expiry and 0 <= days_left <= EXPIRY_WARNING_DAYS),
        "rejection_reason": doc.get("rejection_reason"),
        "reviewed_at": doc.get("reviewed_at"),
        "filename": doc.get("original_filename"),
        "size_label": human_size(doc.get("size_bytes")) if doc.get("size_bytes") else "",
        "has_file": has_file,
        "view_url": url_for("driver_document_file", doc_id=doc_id) if has_file else None,
        "download_url": url_for("driver_document_file", doc_id=doc_id, download=1) if has_file else None,
    }


def documents_overview(app, documents_collection, driver_email, today=None):
    """Everything the Documents page / profile / dashboard need, in one pass."""
    today = today or date.today()
    current = {}
    if documents_collection is not None:
        for doc in documents_collection.find({"driver_email": driver_email}):
            current[doc.get("document_type")] = doc

    cards = []
    counts = {"verified": 0, "pending": 0, "rejected": 0, "expired": 0, "missing": 0, "expiring": 0}
    for dtype, spec in DOCUMENT_TYPES.items():
        doc = current.get(dtype)
        view = document_view_model(app, doc, today) if doc else None
        cards.append({"type": dtype, **spec, "doc": view})
        if spec["required"]:
            if not view:
                counts["missing"] += 1
            elif view["status"] == "Verified":
                counts["verified"] += 1
            elif view["status"] == "Pending Review":
                counts["pending"] += 1
            elif view["status"] == "Rejected":
                counts["rejected"] += 1
            elif view["status"] == "Expired":
                counts["expired"] += 1
        if view and view["expiring_soon"]:
            counts["expiring"] += 1
    required_total = sum(1 for s in DOCUMENT_TYPES.values() if s["required"])
    return {
        "cards": cards,
        "counts": counts,
        "required_total": required_total,
        "all_required_uploaded": counts["missing"] == 0,
        "needs_attention": counts["missing"] + counts["rejected"] + counts["expired"] + counts["expiring"],
    }


def sync_expiry_notifications(documents_collection, notifications_collection, driver_email, today=None):
    """
    Raise a notification when a document is about to expire (or has). Each
    document gets at most one "expiring soon" and one "expired" notice per
    expiry date - tracked on the document itself so page loads never spam.
    """
    if documents_collection is None or notifications_collection is None:
        return 0
    today = today or date.today()
    created = 0
    for doc in documents_collection.find({"driver_email": driver_email}):
        expiry = parse_date(doc.get("expiry_date"))
        if not expiry:
            continue
        days_left = (expiry - today).days
        if days_left < 0:
            state = "expired"
        elif days_left <= EXPIRY_WARNING_DAYS:
            state = "soon"
        else:
            continue
        marker = f"{expiry.isoformat()}:{state}"
        notified = doc.get("expiry_notified") or []
        if marker in notified:
            continue
        label = DOCUMENT_TYPES.get(doc.get("document_type"), {}).get("label", "A document")
        if state == "expired":
            title, message, level = f"{label} has expired", (
                f"Your {_lower_first(label)} expired on {expiry.strftime('%d %b %Y')}. "
                "Upload a current copy to keep receiving deliveries."), "danger"
        else:
            title, message, level = f"{label} expires soon", (
                f"Your {_lower_first(label)} expires on {expiry.strftime('%d %b %Y')} "
                f"({days_left} day{'s' if days_left != 1 else ''} left). Upload the renewed copy in Documents."), "warning"
        notifications_collection.insert_one({
            "driver_email": driver_email,
            "type": "document_expiry",
            "title": title,
            "message": message,
            "status": level,
            "read": False,
            "document_id": str(doc["_id"]),
            "created_at": datetime.now(),
        })
        documents_collection.update_one({"_id": doc["_id"]}, {"$addToSet": {"expiry_notified": marker}})
        created += 1
    return created


# =====================================================
# ROUTES
# =====================================================

def init_driver_documents(
    app,
    drivers_collection,
    documents_collection,
    notifications_collection,
    activity_collection,
    driver_login_required,
    render_page,
):
    def _json_error(message, status=400, **extra):
        payload = {"success": False, "message": message}
        payload.update(extra)
        return jsonify(payload), status

    def _admin_only():
        return "admin" in session or "admin_email" in session

    def _wants_json():
        return request.headers.get("X-Requested-With") == "fetch"

    # -------------------------------------------------
    # Block the old public URL. Anything uploaded before this module is
    # still on disk under /static, but it must only be reachable through
    # the authorized routes below.
    # -------------------------------------------------
    @app.before_request
    def _block_public_document_urls():
        if request.path.startswith(LEGACY_PUBLIC_PREFIX):
            abort(404)

    # -------------------------------------------------
    # Documents page
    # -------------------------------------------------
    @app.route("/driver-documents")
    @driver_login_required
    def driver_documents():
        driver_email = session["driver_email"]
        driver = drivers_collection.find_one({"email": driver_email}) or {}

        sync_expiry_notifications(documents_collection, notifications_collection, driver_email)
        overview = documents_overview(app, documents_collection, driver_email)

        return render_page(
            "driver_documents.html", driver, "documents", "Documents",
            "Upload and track the documents that keep your account active.",
            crumbs=[("Dashboard", "driver_dashboard"), ("Documents", None)],
            overview=overview,
            max_mb=MAX_DOCUMENT_BYTES // (1024 * 1024),
            allowed_label="PDF, PNG, JPG or WEBP",
            today_iso=date.today().isoformat(),
        )

    # -------------------------------------------------
    # Upload / replace (XHR with progress - always JSON)
    # -------------------------------------------------
    @app.route("/api/driver/documents/upload", methods=["POST"])
    @driver_login_required
    def api_driver_document_upload():
        driver_email = session["driver_email"]

        # Cheap early rejection before the body is parsed into memory.
        if request.content_length and request.content_length > MAX_DOCUMENT_BYTES + 512 * 1024:
            return _json_error(
                f"That file is too large. The limit is {human_size(MAX_DOCUMENT_BYTES)}.",
                413, errors={"document": "File is too large."})

        document_type = (request.form.get("document_type") or "").strip()
        clean, errors = validate_document_upload(
            request.files.get("document"), document_type, request.form.get("expiry_date"))
        if errors:
            return _json_error("Please fix the highlighted fields.", 400, errors=errors)

        try:
            previous = documents_collection.find_one(
                {"driver_email": driver_email, "document_type": document_type})

            upload = request.files["document"]
            stored_name, relative_path = store_private_file(app, upload, clean["ext"])

            now = datetime.now()
            documents_collection.update_one(
                {"driver_email": driver_email, "document_type": document_type},
                {"$set": {
                    "driver_email": driver_email,
                    "document_type": document_type,
                    "file_path": relative_path,
                    "stored_name": stored_name,
                    "original_filename": secure_filename(upload.filename) or f"document{clean['ext']}",
                    "mime_type": clean["mime"],
                    "size_bytes": clean["size"],
                    "expiry_date": clean["expiry_date"],
                    # A new file always goes back to review - an old approval
                    # never carries over to a different file.
                    "status": "Pending Review",
                    "uploaded_at": now,
                    "reviewed_at": None,
                    "reviewed_by": None,
                    "rejection_reason": None,
                    "expiry_notified": [],
                }},
                upsert=True,
            )

            # The previous file is now orphaned - remove it if we own it.
            old_path = resolve_document_path(app, previous) if previous else None
            if old_path and os.path.abspath(old_path) != os.path.abspath(os.path.join(project_root(app), relative_path)):
                try:
                    os.remove(old_path)
                except OSError:
                    logger.warning("Could not remove replaced document file %s", old_path)

            label = DOCUMENT_TYPES[document_type]["label"]
            if activity_collection is not None:
                activity_collection.insert_one({
                    "driver_email": driver_email, "type": "Document Uploaded",
                    "detail": f"{label} uploaded for review", "at": now})
            if notifications_collection is not None:
                notifications_collection.insert_one({
                    "admin": True, "audience": "admin", "user_email": None,
                    "title": "Driver document awaiting review",
                    "message": f"{driver_email} uploaded a {_lower_first(label)} for review.",
                    "type": "driver_document", "status": "info", "read": False,
                    "driver_email_ref": driver_email, "created_at": now})

            doc = documents_collection.find_one(
                {"driver_email": driver_email, "document_type": document_type})
            view = document_view_model(app, doc)
            return jsonify({
                "success": True,
                "message": f"{label} uploaded. It is now waiting for admin review.",
                "document": {
                    "id": view["id"], "type": view["type"], "status": view["status"],
                    "expiry_date": view["expiry_date"].isoformat() if view["expiry_date"] else None,
                },
            })
        except Exception:
            logger.exception("Driver document upload failed for %s", driver_email)
            return _json_error("We couldn't save that document. Please try again.", 500)

    # -------------------------------------------------
    # View / download a document (owner only)
    # -------------------------------------------------
    @app.route("/driver-documents/<doc_id>/file")
    @driver_login_required
    def driver_document_file(doc_id):
        try:
            oid = ObjectId(doc_id)
        except Exception:
            abort(404)
        doc = documents_collection.find_one({"_id": oid, "driver_email": session["driver_email"]})
        return _send_document(doc)

    def _send_document(doc):
        if not doc:
            abort(404)
        path = resolve_document_path(app, doc)
        if not path:
            abort(404)
        ext = os.path.splitext(path)[1].lower()
        mimetype = sorted(ALLOWED_MIME.get(ext, {"application/octet-stream"}))[0]
        label = DOCUMENT_TYPES.get(doc.get("document_type"), {}).get("label", "document")
        download_name = secure_filename(f"{label}{ext}") or f"document{ext}"
        response = send_file(
            path, mimetype=mimetype,
            as_attachment=request.args.get("download") == "1",
            download_name=download_name, max_age=0)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox"
        return response

    # -------------------------------------------------
    # ADMIN: view any driver's document
    # -------------------------------------------------
    @app.route("/admin/driver-documents/<doc_id>/file")
    def admin_driver_document_file(doc_id):
        if not _admin_only():
            return redirect(url_for("admin_login"))
        try:
            oid = ObjectId(doc_id)
        except Exception:
            abort(404)
        return _send_document(documents_collection.find_one({"_id": oid}))

    # -------------------------------------------------
    # ADMIN: approve / reject
    # -------------------------------------------------
    @app.route("/admin/driver-documents/<doc_id>/<action>", methods=["POST"])
    def admin_review_driver_document(doc_id, action):
        if not _admin_only():
            if _wants_json():
                return jsonify({"ok": False, "message": "Your admin session has expired. Please sign in again."}), 401
            return redirect(url_for("admin_login"))

        def respond(ok, message, status=200):
            if _wants_json():
                return jsonify({"ok": ok, "message": message}), status
            flash(message, "success" if ok else "danger")
            return redirect(url_for("driver_management"))

        if action not in ("approve", "reject"):
            return respond(False, "Unknown action.", 400)
        try:
            oid = ObjectId(doc_id)
        except Exception:
            return respond(False, "Document not found.", 404)

        doc = documents_collection.find_one({"_id": oid})
        if not doc:
            return respond(False, "Document not found.", 404)

        reason = (request.form.get("reason") or (request.get_json(silent=True) or {}).get("reason") or "").strip()
        if action == "reject" and len(reason) < 3:
            return respond(False, "Give the driver a short reason for the rejection.", 400)
        if len(reason) > REJECTION_REASON_MAX:
            return respond(False, f"Keep the reason under {REJECTION_REASON_MAX} characters.", 400)

        new_status = "Verified" if action == "approve" else "Rejected"
        now = datetime.now()
        admin = session.get("admin_email", "admin")

        # Compare-and-set: only a document still waiting for review can be
        # decided, so two admins (or a double click) can't flip each other.
        claimed = documents_collection.find_one_and_update(
            {"_id": oid, "status": {"$in": ["Pending Review", "Pending"]}},
            {"$set": {
                "status": new_status,
                "reviewed_at": now,
                "reviewed_by": admin,
                "rejection_reason": reason if action == "reject" else None,
            }},
        )
        if claimed is None:
            return respond(False, f"This document is already {doc.get('status')}. No changes were made.", 409)

        label = DOCUMENT_TYPES.get(doc.get("document_type"), {}).get("label", "Document")
        if notifications_collection is not None:
            if action == "approve":
                title, message, level = f"{label} verified", f"Your {_lower_first(label)} was verified by the AquaFlow team.", "success"
            else:
                title, message, level = f"{label} rejected", (
                    f"Your {_lower_first(label)} was rejected: {reason} Upload a corrected copy in Documents."), "danger"
            notifications_collection.insert_one({
                "driver_email": doc.get("driver_email"), "type": "document_review",
                "title": title, "message": message, "status": level,
                "read": False, "document_id": str(oid), "created_at": now})

        return respond(True, f"{label} marked {new_status}.")

    return app
