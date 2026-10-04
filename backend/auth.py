import re
import os
import secrets
import logging
import hashlib
import hmac
from datetime import datetime, timedelta
from flask import Blueprint, request, session, redirect, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

from database import (create_user, find_user_by_email, find_user_by_id, find_user_by_provider,
                      find_user_by_reset_token, set_reset_token, clear_reset_token,
                      update_password, update_user_provider, update_user_profile, admin_count, set_user_active, set_user_admin)
from auth_context import authenticated_user, create_access_token, current_user_id, revoke_current_token

# flask-limiter is set up in backend/limiter_setup.py so it can be
# imported here without creating a circular dependency on app.py.
from limiter_setup import limiter

# Lower the default pbkdf2 cost (Werkzeug 3 default is 600_000) so login
# and registration don't burn a second of CPU on every request. 200_000
# is still well above OWASP guidance, and existing hashes in the DB
# remain valid (login verifies against the stored hash, only new writes
# use the lower cost).
PBKDF2_ITERATIONS = 200_000
PBKDF2_METHOD = f'pbkdf2:sha256:{PBKDF2_ITERATIONS}'

auth_bp = Blueprint('auth', __name__)

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def _wants_json():
    accept = request.headers.get('Accept', '')
    return 'application/json' in accept or request.is_json


def login_user_row(row):
    session.clear()
    session['user_id'] = row['id']
    session['user_name'] = row['username']
    session['user_email'] = row['email']
    session['is_admin'] = bool(row['is_admin'])


def _verify_password(row, password):
    password_hash = row['password_hash'] or ''
    if check_password_hash(password_hash, password):
        return True

    # Older installations stored unsalted MD5 digests. Accept a matching
    # legacy hash once, then replace it with the current PBKDF2 hash.
    if re.fullmatch(r'[0-9a-fA-F]{32}', password_hash):
        legacy_hash = hashlib.md5(
            password.encode('utf-8'), usedforsecurity=False
        ).hexdigest()
        if hmac.compare_digest(password_hash.lower(), legacy_hash):
            update_password(
                row['id'],
                generate_password_hash(
                    password, method=PBKDF2_METHOD, salt_length=16
                ),
            )
            logging.info('Upgraded legacy password hash after successful login')
            return True

    return False


@auth_bp.route('/api/me', methods=['GET'])
def current_user():
    row = authenticated_user()
    if not row:
        return jsonify({'logged_in': False})
    return jsonify({'logged_in': True, 'name': row['username'], 'username': row['username'], 'email': row['email'], 'is_admin': bool(row['is_admin']), 'is_active': bool(row['is_active']), 'provider': row['provider'], 'created_at': row['created_at']})


@auth_bp.route('/api/profile', methods=['PUT'])
def update_profile():
    user_id = current_user_id()
    if not user_id:
        return jsonify({'status': 'error', 'error': 'Unauthorized'}), 401
    data = request.get_json(silent=True) or {}
    username = str(data.get('name') or '').strip()
    if len(username) < 2:
        return jsonify({'status': 'error', 'error': 'Name must be at least 2 characters.'}), 400
    update_user_profile(user_id, username)
    if not request.headers.get('Authorization'):
        session['user_name'] = username
    return jsonify({'status': 'ok', 'name': username})


# ---- Local Register / Login ----

@auth_bp.route('/register', methods=['POST'])
@limiter.limit('5 per minute')
def register():
    username = (request.form.get('username') or '').strip()
    email = (request.form.get('email') or '').strip()
    password = request.form.get('password') or ''
    confirm = request.form.get('confirm') or ''

    def fail(msg, status=400):
        return {'status': 'error', 'error': msg}, status

    if not username or len(username) < 2:
        return fail('Please enter a name (at least 2 characters).')
    if not EMAIL_RE.match(email):
        return fail('Please enter a valid email address.')
    if len(password) < 6:
        return fail('Password must be at least 6 characters.')
    if confirm and password != confirm:
        return fail('Passwords do not match.')
    if find_user_by_email(email):
        return fail('An account with this email already exists. Try logging in.', 409)

    try:
        user_id = create_user(
            username,
            email,
            generate_password_hash(password, method=PBKDF2_METHOD, salt_length=16),
            is_active=0,
        )
    except Exception:
        logging.exception('Registration failed')
        return fail('Could not create account. Please try again.', 500)

    # Auto-approve first user as admin
    if admin_count() == 0:
        set_user_active(user_id, True)
        set_user_admin(user_id, True)
        row = find_user_by_email(email)
        login_user_row(row)
        logging.info(f'First user registered as admin: {email}')
        return {
            'status': 'ok',
            'message': 'Welcome! You are the admin.',
            'user': {'name': username, 'is_admin': True},
            'access_token': create_access_token(user_id),
            'token_type': 'Bearer',
        }

    try:
        from app import send_registration_notification
        send_registration_notification(username, email, user_id)
    except Exception:
        logging.exception('Failed to send registration notification')

    row = find_user_by_email(email)
    logging.info(f'New user registered: {email}')
    return {'status': 'ok', 'message': 'Account created. Your account is being verified by admin. You will be able to login once approved.', 'pending_approval': True}


@auth_bp.route('/login', methods=['POST'])
@limiter.limit('10 per minute')
def login():
    email = (request.form.get('email') or '').strip()
    password = request.form.get('password') or ''

    def fail(msg, status=400):
        return {'status': 'error', 'error': msg}, status

    if not EMAIL_RE.match(email) or not password:
        return fail('Please enter your email and password.')

    row = find_user_by_email(email)
    if not row or not _verify_password(row, password):
        # Brute-force defense is now handled by @limiter.limit() above,
        # not by sleeping the worker. Failed logins no longer block the
        # request thread for 400ms.
        return fail('Invalid email or password.', 401)

    if not row['is_active']:
        return fail('Your account is pending approval. Please wait for admin activation.', 403)

    login_user_row(row)
    logging.info(f'User logged in: {email}')
    return {
        'status': 'ok',
        'message': 'Logged in',
        'user': {'name': row['username'], 'is_admin': bool(row['is_admin'])},
        'access_token': create_access_token(row['id']),
        'token_type': 'Bearer',
    }


@auth_bp.route('/logout', methods=['GET', 'POST'])
def logout():
    revoke_current_token()
    session.clear()
    if _wants_json():
        return {'status': 'ok'}
    return redirect('/')


# ---- Forgot Password ----

@auth_bp.route('/forgot-password', methods=['POST'])
@limiter.limit('5 per minute')
def forgot_password():
    email = (request.form.get('email') or '').strip()

    def success(msg):
        return {'status': 'ok', 'message': msg}

    if not EMAIL_RE.match(email):
        def fail(msg):
            return {'status': 'error', 'error': msg}, 400
        return fail('Please enter a valid email address.')

    row = find_user_by_email(email)
    if row and row['provider'] == 'local':
        token = secrets.token_urlsafe(32)
        expiry = (datetime.utcnow() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
        set_reset_token(row['id'], token, expiry)
        site_url = (os.environ.get('SITE_URL') or f'https://{request.host}').rstrip('/')
        reset_url = f'{site_url}/reset-password/{token}'
        from app import _send_email
        _send_email(email, 'Reset your password',
                    f'Click the link to reset your password (valid for 1 hour):\n\n{reset_url}\n\nIf you did not request this, ignore this email.')
    # Always show success (don't reveal if email exists)
    return success('If that email is registered, a reset link has been sent.')


@auth_bp.route('/reset-password/<token>', methods=['GET', 'POST'])
@limiter.limit('5 per minute')
def reset_password(token):
    row = find_user_by_reset_token(token)
    if not row:
        return jsonify({'status': 'error', 'error': 'Reset link expired or invalid.'}), 400

    if request.method == 'GET':
        return {'status': 'ok', 'token': 'valid'}

    password = request.form.get('password') or ''
    confirm = request.form.get('confirm') or ''

    def fail(msg):
        return {'status': 'error', 'error': msg}, 400

    if len(password) < 6:
        return fail('Password must be at least 6 characters.')
    if password != confirm:
        return fail('Passwords do not match.')

    update_password(
        row['id'],
        generate_password_hash(password, method=PBKDF2_METHOD, salt_length=16),
    )
    clear_reset_token(row['id'])
    logging.info(f'Password reset for user {row["email"]}')

    if _wants_json():
        return {'status': 'ok', 'message': 'Password reset successfully. You can now log in.'}
    return {'status': 'ok', 'message': 'Password reset successfully. You can now log in.'}
