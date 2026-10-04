import os
import sys
import threading
from urllib.parse import urlsplit, urlunsplit
from flask import Flask, request, abort, send_from_directory, session, jsonify
import json
import logging
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

# Load environment variables before importing modules that select/configure the database.
# Prefer the app-local backend/.env for runtime config, but also load the repo root .env
# so local dev and deployment settings behave consistently regardless of the working dir.
root_env = os.path.join(os.path.dirname(BACKEND_DIR), '.env')
backend_env = os.path.join(BACKEND_DIR, '.env')
for env_path in (root_env, backend_env):
    if os.path.exists(env_path):
        load_dotenv(env_path, override=False)

if os.path.exists(backend_env):
    load_dotenv(backend_env, override=True)

from auth_context import current_user_id, revoke_current_token

# Local modules (imported lazily inside route handlers in some places,
# eagerly here for ones that expose constants used at module load time).
import worker
import database

app = Flask(__name__, static_folder=None)

# -- User accounts & saved files --
import secrets as _secrets
from database import init_db, close_db, MAX_FILES_PER_USER, MAX_TOTAL_BYTES

# Rate limiter — defined in its own module to break the app <-> auth/admin
# import cycle. See backend/limiter_setup.py.
from limiter_setup import limiter

# auth.py and admin.py import `limiter` from limiter_setup, so this works
# even when app.py is still in the middle of loading.
from auth import auth_bp
from files import files_bp
from admin import admin_bp


def _get_secret_key():
    key = os.environ.get('SECRET_KEY')
    #print(f"Secret key from environment: {key}")
    if key:
        return key
    kf = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.secret_key')
    try:
        if os.path.exists(kf):
            with open(kf) as f:
                k = f.read().strip()
            if k:
                return k
        k = _secrets.token_hex(32)
        with open(kf, 'w') as f:
            f.write(k)
        return k
    except Exception:
        return 'dev-only-insecure-key'


app.secret_key = _get_secret_key()
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB uploads

# Cross-origin session cookie settings (only for HTTPS origins)
_cors = os.environ.get('CORS_ORIGINS', '').strip()
_site_url = os.environ.get('SITE_URL', '').strip()


def _normalize_origin(origin):
    parts = urlsplit(origin.strip())
    if parts.scheme not in ('http', 'https') or not parts.netloc or parts.path not in ('', '/') or parts.query or parts.fragment:
        return ''
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), '', '', ''))


_allowed_cors_origins = {
    normalized
    for normalized in (_normalize_origin(origin) for origin in _cors.split(','))
    if normalized
}

# Session cookie: SameSite=None is REQUIRED for cross-origin fetch().
# Flask defaults to "Lax" which BLOCKS cookies on cross-origin requests.
# Always use None — safe for both same-origin and cross-origin.
app.config['SESSION_COOKIE_SAMESITE'] = 'None'

# Set secure cookie flag based on production indicators
# This is more reliable than checking the request URL at startup time
def _is_production():
    # Check common production indicators
    prod_indicators = [
        os.environ.get('FLASK_ENV') == 'production',
        os.environ.get('PYTHON_ENV') == 'production',
        os.environ.get('ENVIRONMENT') == 'production',
        os.environ.get('DEPLOYMENT') == 'production',
        os.environ.get('SITE_URL', '').startswith('https://'),
        os.environ.get('CORS_ORIGINS', '').startswith('https://'),
        os.environ.get('SECURE_COOKIES', 'false').lower() == 'true',
    ]
    return any(prod_indicators)

app.config['SESSION_COOKIE_SECURE'] = _is_production()

app.register_blueprint(auth_bp)
app.register_blueprint(files_bp)
app.register_blueprint(admin_bp, url_prefix='/admin')


@app.route('/api/content/pages/<slug>')
def public_page(slug):
    from database import get_page_by_slug, get_site_settings
    page = get_page_by_slug(slug)
    if not page:
        return jsonify({'status': 'error', 'error': 'Page not found'}), 404
    settings = get_site_settings()
    return jsonify({'status': 'ok', 'page': dict(page), 'site': settings})


@app.route('/api/site-settings')
def public_site_settings():
    from database import get_site_settings
    return jsonify({'status': 'ok', 'settings': get_site_settings()})


@app.route('/api/content/posts/<slug>')
def public_post(slug):
    from database import get_post_by_slug, get_site_settings
    post = get_post_by_slug(slug)
    if not post:
        return jsonify({'status': 'error', 'error': 'Post not found'}), 404
    return jsonify({'status': 'ok', 'post': post, 'site': get_site_settings()})

limiter.init_app(app)


def _cors_headers():
    """Return CORS headers for the current request origin, or None if disallowed."""
    origin = _normalize_origin(request.headers.get('Origin', ''))
    if not origin:
        return None
    # Fail CLOSED. This previously read `if _allowed_cors_origins and origin not in ...`,
    # so an empty or unparseable CORS_ORIGINS (e.g. an entry missing its scheme, which
    # `_normalize_origin` silently drops) skipped the check entirely and echoed back
    # whatever origin asked — with Allow-Credentials: true. That let any website make
    # authenticated admin calls from a logged-in browser.
    if origin not in _allowed_cors_origins:
        return None
    return {
        'Access-Control-Allow-Origin': origin,
        'Access-Control-Allow-Credentials': 'true',
        'Access-Control-Allow-Headers': request.headers.get('Access-Control-Request-Headers', 'Content-Type, Accept'),
        'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
        'Access-Control-Max-Age': '86400',
    }


@app.before_request
def handle_cors_preflight():
    if request.method == 'OPTIONS':
        headers = _cors_headers()
        if headers:
            resp = app.make_default_options_response()
            for k, v in headers.items():
                resp.headers[k] = v
            return resp


@app.after_request
def add_cors_headers(response):
    # Always vary on Origin, including on the rejection path. The headers below
    # differ per origin and are omitted entirely for disallowed ones, so without
    # this a shared cache (Cloudflare, LiteSpeed) can store a header-less
    # response and replay it to a browser that needed one.
    response.vary.add('Origin')
    headers = _cors_headers()
    if headers:
        for k, v in headers.items():
            response.headers[k] = v
    return response


@app.teardown_appcontext
def _close_db(exc):
    close_db(exc)

# configure logging
from logging_config import setup_logging
setup_logging(app)

# CORS now fails closed, so a missing or malformed CORS_ORIGINS silently blocks the
# whole browser frontend. Say so at boot, in logs/app.log, rather than leaving it to
# be diagnosed from the browser console.
if not _allowed_cors_origins:
    app.logger.warning(
        'CORS_ORIGINS is empty or contains no valid origins (raw value: %r). Every '
        'cross-origin browser request will be rejected without CORS headers. Set it to a '
        'comma-separated list of FULL origins including the scheme, e.g. '
        'CORS_ORIGINS=https://app.techintricks.in', _cors
    )
else:
    app.logger.info('CORS allow-list: %s', ', '.join(sorted(_allowed_cors_origins)))

init_db()

# Background-job executor (ThreadPoolExecutor) for conversion/compression routes
import jobs
jobs.init_worker(app)

# --- Serve React frontend ---
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_REACT_DIST_CANDIDATES = (
    os.path.join(_BACKEND_DIR, '..', 'frontend', 'dist'),
    os.path.join(_BACKEND_DIR, '..', '..', 'Utility-System-Frontend', 'dist'),
)
REACT_DIST = next((os.path.normpath(path) for path in _REACT_DIST_CANDIDATES if os.path.isdir(path)), os.path.normpath(_REACT_DIST_CANDIDATES[0]))

ADMIN_SPA_PATHS = {
    '/admin/login', '/admin/dashboard', '/admin/pages', '/admin/pages/new',
    '/admin/posts', '/admin/posts/new', '/admin/categories', '/admin/tags', '/admin/seo',
}


@app.before_request
def serve_admin_spa_for_html_requests():
    """Keep browser admin routes on React; JSON clients still use Flask APIs."""
    if request.method != 'GET' or not os.path.isdir(REACT_DIST):
        return None
    path = request.path.rstrip('/') or '/'
    is_editor_route = path.startswith('/admin/pages/edit/') or path.startswith('/admin/posts/edit/')
    accepts_html = request.accept_mimetypes.accept_html and not request.accept_mimetypes.accept_json
    if accepts_html and (path in ADMIN_SPA_PATHS or is_editor_route):
        return send_from_directory(REACT_DIST, 'index.html')
    return None

if os.path.isdir(REACT_DIST):
    @app.route('/', defaults={'path': ''})
    @app.route('/<path:path>')
    def serve_react(path):
        full = os.path.join(REACT_DIST, path)
        if path and os.path.isfile(full):
            return send_from_directory(REACT_DIST, path)
        return send_from_directory(REACT_DIST, 'index.html')

ALLOWED_IMG = {"jpg", "jpeg", "png", "webp"}
ALLOWED_PDF = {"pdf"}
ALLOWED_DOC = {"docx"}


# Conversion routes used to call `delete_file_later` for temp files and
# `maybe_save_output` to send+save the file. Both are gone: the worker now
# writes its output into `backend/job_outputs/<job_id>/out/`, and the
# download route serves it. Save-to-account happens inside the worker.

# Per-job input limits. Enforced in the route before enqueue.
MAX_IMAGES_PER_JOB = 20
MAX_PDFS_PER_JOB = 5
MAX_WORD_FILES_PER_JOB = 1


def _enqueue(kind, *, params, result_filename, result_mimetype,
             uploaded_files, allowed_exts, max_files):
    """Save uploaded files to the job's input dir, enqueue the job, return 202.

    `params` is a dict passed to the worker. Anything in `_input_files` is
    consumed here (path, dest_name) and stripped before the row is written.
    """
    if not uploaded_files:
        abort(400, f'No {kind.replace("_", " ")} file(s) provided')
    if len(uploaded_files) > max_files:
        abort(400, f'Too many files (max {max_files} per job)')

    # Filter to allowed extensions
    valid = []
    for f in uploaded_files:
        if not f or not f.filename or '.' not in f.filename:
            continue
        ext = f.filename.rsplit('.', 1)[1].lower()
        if ext not in allowed_exts:
            continue
        valid.append(f)
    if not valid:
        abort(400, f'No valid {kind.replace("_", " ")} file(s) provided')

    # Stage every file to a per-call temp file, then enqueue. We don't know
    # the job_id until enqueue_job runs, so we copy the staged files into
    # backend/job_outputs/<id>/in/ inside enqueue_job itself.
    import tempfile
    staging_paths = []
    for f in valid:
        safe = secure_filename(f.filename) or f'file_{len(staging_paths)}'
        # Unique prefix to avoid name clashes if multiple files share a name.
        unique = f'{len(staging_paths):03d}_{safe}'
        fd, tmp_path = tempfile.mkstemp(prefix='jobin_', suffix=f'_{safe}')
        os.close(fd)
        f.save(tmp_path)
        staging_paths.append((tmp_path, unique))

    # Schedule cleanup of the staging files. The enqueue copies them into
    # the job dir before the worker starts, so a few seconds is plenty.
    def _cleanup_staging(paths):
        for p, _ in paths:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
    threading.Timer(30, _cleanup_staging, args=(staging_paths,)).start()

    params['_input_files'] = staging_paths
    user_id = current_user_id()
    save_flag = (request.form.get('save') == '1')

    job_id = jobs.enqueue_job(
        user_id=user_id,
        kind=kind,
        params=params,
        result_filename=result_filename,
        result_mimetype=result_mimetype,
        save_to_account=save_flag,
    )

    payload = {
        'status': 'accepted',
        'job_id': job_id,
        'status_url': f'/api/jobs/{job_id}',
    }
    return app.response_class(json.dumps(payload), status=202, mimetype='application/json')


# Return JSON error responses when requested by client
from werkzeug.exceptions import HTTPException


@app.errorhandler(HTTPException)
def handle_http_exception(e):
    # If client asked for JSON, return JSON error body
    accept = request.headers.get('Accept', '')
    if 'application/json' in accept:
        payload = {'error': e.description}
        return app.response_class(json.dumps(payload), status=e.code, mimetype='application/json')
    # default: keep Flask's default behavior
    return e


@app.errorhandler(Exception)
def handle_unexpected_error(e):
    # log exception
    logging.exception('Internal server error')
    accept = request.headers.get('Accept', '')
    if 'application/json' in accept:
        payload = {'error': 'Internal server error'}
        return app.response_class(json.dumps(payload), status=500, mimetype='application/json')
    # re-raise for default handler (shows traceback in debug)
    raise


## Page routes removed — React SPA serves all frontend via catch-all below
## POST endpoints for tools remain intact for the React app to call

# ---- Email helpers ----
def _send_email(to, subject, body):
    """Send an email via SMTP. Returns True on success."""
    import smtplib
    from email.message import EmailMessage
    smtp_host = os.environ.get('SMTP_HOST')
    smtp_port = int(os.environ.get('SMTP_PORT') or 0)
    smtp_user = os.environ.get('SMTP_USER')
    smtp_pass = os.environ.get('SMTP_PASS')
    from_addr = os.environ.get('FROM_EMAIL') or smtp_user or f'noreply@{request.host.split(":")[0]}'
    if not smtp_host or not smtp_port:
        logging.warning('SMTP not configured; skipping email')
        return False
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = from_addr
    msg['To'] = to
    msg.set_content(body)
    try:
        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=6)
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=6)
            try:
                server.starttls()
            except Exception:
                pass
        if smtp_user and smtp_pass:
            server.login(smtp_user, smtp_pass)
        server.send_message(msg)
        server.quit()
        logging.info(f'Email sent to {to}: {subject}')
        return True
    except Exception as e:
        logging.error(f'Failed to send email to {to}: {e}')
        return False


def send_registration_notification(username, email, user_id):
    """Notify admin about a new user registration."""
    admin = os.environ.get('ADMIN_EMAIL')
    if not admin:
        return
    site_url = (os.environ.get('SITE_URL') or f'https://{request.host}').rstrip('/')
    body = (
        f'New user registered:\n\n'
        f'Name: {username}\n'
        f'Email: {email}\n\n'
        f'Approve or manage users:\n'
        f'{site_url}/admin/dashboard\n'
    )
    _send_email(admin, f'New registration: {username}', body)


def send_activation_email(username, email, approved):
    """Notify user that their account has been activated or rejected."""
    site_url = (os.environ.get('SITE_URL') or f'https://{request.host}').rstrip('/')
    if approved:
        body = (
            f'Hi {username},\n\n'
            f'Your account has been approved! You can now log in:\n'
            f'{site_url}/login\n'
        )
        subject = 'Your account has been approved'
    else:
        body = (
            f'Hi {username},\n\n'
            f'Your account registration was not approved.\n'
            f'Contact us if you have questions.\n'
        )
        subject = 'Account registration update'
    _send_email(email, subject, body)


BASE_DIR = os.path.abspath(os.path.dirname(__file__))

## test-email removed — tracking disabled


# ---------------- COMPRESSOR MENUS ----------------
## Menu routes removed — React SPA handles these pages


# ================= IMAGE COMPRESSOR =================
## Image compressor GET removed — React SPA handles this page


@app.route("/compressor/image", methods=["POST"])
def image_compressor_post():
    files = request.files.getlist("images")
    try:
        quality = int(request.form.get("quality", 75))
    except Exception:
        quality = 75
    # result_filename is just a hint for the worker; the worker uses
    # per-file names inside a zip when there are multiple inputs.
    return _enqueue(
        'image_compress',
        params={'quality': quality},
        result_filename='image-compressed.jpg',
        result_mimetype='image/jpeg',
        uploaded_files=files,
        allowed_exts=ALLOWED_IMG,
        max_files=MAX_IMAGES_PER_JOB,
    )


# ================= IMAGE CONVERTER =================
# Constants and helpers used to live here; they now live in backend/worker.py
# so the route stays a thin shim.


## Image converter GET removed — React SPA handles this page


@app.route("/conversion/image-convert", methods=["POST"])
def image_convert_post():
    files = request.files.getlist("images")
    target = (request.form.get("format") or "").lower()
    if target not in worker.IMAGE_CONVERT_TARGETS:
        abort(400, "Unsupported target format")
    _, mime, _ = worker.IMAGE_CONVERT_TARGETS[target]
    try:
        quality = max(1, min(100, int(request.form.get("quality", 90))))
    except Exception:
        quality = 90
    return _enqueue(
        'image_convert',
        params={'format': target, 'quality': quality},
        result_filename=f'image.{target}',
        result_mimetype=mime,
        uploaded_files=files,
        allowed_exts=worker.CONVERT_INPUT_EXT,
        max_files=MAX_IMAGES_PER_JOB,
    )


# ================= PDF COMPRESSOR =================
## PDF compressor GET removed — React SPA handles this page


@app.route("/compressor/pdf", methods=["POST"])
def pdf_compressor_post():
    files = request.files.getlist("pdfs")
    quality_choice = (request.form.get('quality') or 'medium').lower()
    if quality_choice not in {'high', 'medium', 'regular'}:
        quality_choice = 'medium'
    return _enqueue(
        'pdf_compress',
        params={'quality': quality_choice},
        result_filename='document-compressed.pdf',
        result_mimetype='application/pdf',
        uploaded_files=files,
        allowed_exts=ALLOWED_PDF,
        max_files=MAX_PDFS_PER_JOB,
    )


# ================= IMAGE → PDF =================
## Image-to-PDF GET removed — React SPA handles this page


@app.route("/conversion/image-to-pdf", methods=["POST"])
def image_to_pdf_post():
    images = request.files.getlist("images")
    return _enqueue(
        'image_to_pdf',
        params={'outname': 'converted.pdf'},
        result_filename='converted.pdf',
        result_mimetype='application/pdf',
        uploaded_files=images,
        allowed_exts=ALLOWED_IMG,
        max_files=MAX_IMAGES_PER_JOB,
    )


# ================= COMBINE PDFs =================
## Combine PDFs GET removed — React SPA handles this page


@app.route("/compressor/combine", methods=["POST"])
def combine_pdfs_post():
    files = request.files.getlist("pdfs")
    outname = (request.form.get('outname') or '').strip() or 'combined.pdf'
    return _enqueue(
        'combine_pdfs',
        params={'outname': outname},
        result_filename=outname if outname.lower().endswith('.pdf') else outname + '.pdf',
        result_mimetype='application/pdf',
        uploaded_files=files,
        allowed_exts=ALLOWED_PDF,
        max_files=MAX_PDFS_PER_JOB,
    )


# ================= TEXT → PDF =================
## Text-to-PDF GET removed — React SPA handles this page


@app.route("/conversion/text-to-pdf", methods=["POST"])
def text_to_pdf_post():
    text = (request.form.get("text") or "").replace("\r\n", "\n")
    outname = (request.form.get('outname') or '').strip() or 'text.pdf'
    if not outname.lower().endswith('.pdf'):
        outname = outname + '.pdf'
    # Text-to-PDF has no file upload. The worker reads from params['text'].
    job_id = jobs.enqueue_job(
        user_id=current_user_id(),
        kind='text_to_pdf',
        params={'text': text, 'outname': outname, '_input_files': []},
        result_filename=outname,
        result_mimetype='application/pdf',
        save_to_account=(request.form.get('save') == '1'),
    )
    payload = {
        'status': 'accepted',
        'job_id': job_id,
        'status_url': f'/api/jobs/{job_id}',
    }
    return app.response_class(json.dumps(payload), status=202, mimetype='application/json')


# ================= WORD → PDF =================
## Word-to-PDF GET removed — React SPA handles this page


@app.route("/conversion/word-to-pdf", methods=["POST"])
def word_to_pdf_post():
    file = request.files.get("word")
    return _enqueue(
        'word_to_pdf',
        params={},
        result_filename='document.pdf',
        result_mimetype='application/pdf',
        uploaded_files=[file] if file else [],
        allowed_exts=ALLOWED_DOC,
        max_files=MAX_WORD_FILES_PER_JOB,
    )


# ================= PDF → WORD =================
## PDF-to-Word GET removed — React SPA handles this page


@app.route("/conversion/pdf-to-word", methods=["POST"])
def pdf_to_word_post():
    files = request.files.getlist('pdfs')
    return _enqueue(
        'pdf_to_word',
        params={},
        result_filename='document.docx',
        result_mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        uploaded_files=files,
        allowed_exts=ALLOWED_PDF,
        max_files=MAX_PDFS_PER_JOB,
    )


@app.route("/api/change-password", methods=['POST'])
def api_change_password():
    uid = current_user_id()
    if not uid:
        return jsonify({'status': 'error', 'error': 'Not logged in'}), 401
    data = request.get_json(silent=True) or {}
    current = data.get('current_password', '')
    new = data.get('new_password', '')
    if len(new) < 6:
        return jsonify({'status': 'error', 'error': 'New password must be at least 6 characters.'}), 400
    from database import find_user_by_id, update_password
    from werkzeug.security import check_password_hash, generate_password_hash
    row = find_user_by_id(uid)
    if not row:
        return jsonify({'status': 'error', 'error': 'User not found'}), 404
    if row['password_hash'] and not check_password_hash(row['password_hash'], current):
        return jsonify({'status': 'error', 'error': 'Current password is incorrect.'}), 403
    update_password(uid, generate_password_hash(new))
    return jsonify({'status': 'ok', 'message': 'Password changed successfully.'})


@app.route("/api/delete-account", methods=['POST'])
def api_delete_account():
    uid = current_user_id()
    if not uid:
        return jsonify({'status': 'error', 'error': 'Not logged in'}), 401
    from database import delete_user_account
    info = delete_user_account(uid)
    if not info:
        return jsonify({'status': 'error', 'error': 'User not found'}), 404
    revoke_current_token()
    session.clear()
    if not info['is_admin']:
        admin = os.environ.get('ADMIN_EMAIL')
        if admin:
            site_url = (os.environ.get('SITE_URL') or f'https://{request.host}').rstrip('/')
            body = (
                f'A user has deleted their account.\n\n'
                f'Name: {info["username"]}\n'
                f'Email: {info["email"]}\n\n'
                f'You may want to reach out for feedback.\n'
            )
            _send_email(admin, f'Account deleted: {info["username"]}', body)
    return jsonify({'status': 'ok', 'message': 'Account deleted.'})


# ================= JOB STATUS / DOWNLOAD =================

@app.route("/api/jobs/<job_id>", methods=["GET"])
def api_job_status(job_id):
    payload = jobs.get_job_response(job_id)
    if payload is None:
        return jsonify({'status': 'error', 'error': 'Job not found'}), 404
    return jsonify(payload)


@app.route("/api/jobs/<job_id>/download", methods=["GET"])
def api_job_download(job_id):
    row = database.get_job_row(job_id)
    if not row or not row['result_filename']:
        abort(404)
    # Auth: a job tied to a specific user is only downloadable by that user.
    # Anonymous jobs (user_id IS NULL) are downloadable by anyone who knows
    # the job_id — same as the old behavior where the response was the
    # binary file itself. The job_id is a 16-byte secrets token so it's
    # unguessable in practice.
    if row['user_id'] and row['user_id'] != current_user_id():
        abort(404)
    out_dir = os.path.join(app.config['JOB_OUTPUT_DIR'], job_id, 'out')
    full = os.path.join(out_dir, row['result_filename'])
    if not os.path.isfile(full):
        abort(404)
    resp = send_from_directory(
        out_dir,
        row['result_filename'],
        as_attachment=True,
        download_name=row['result_filename'],
    )
    if row['result_mimetype']:
        resp.mimetype = row['result_mimetype']
    # Clean up the result dir a few seconds after the response goes out.
    jobs.delete_result_dir_later(job_id, delay=8)
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
