import logging
from flask import Blueprint, request, session, jsonify

from database import (set_user_active, delete_user,
                      get_site_settings, set_site_settings, list_pages, create_page,
                      update_page, delete_page, list_posts, create_post, update_post,
                      delete_post, list_taxonomy, create_taxonomy, update_user_admin,
                      get_admin_dashboard_stats)
from auth_context import current_user_id, current_user_is_admin, create_access_token, revoke_current_token

# flask-limiter is set up in backend/limiter_setup.py so it can be
# imported here without creating a circular dependency on app.py.
from limiter_setup import limiter

admin_bp = Blueprint('admin', __name__)


def _is_admin():
    return current_user_is_admin()


def _json_body():
    return request.get_json(silent=True) or {}


def _page_payload(data):
    slug = str(data.get('slug') or '').strip().lower().strip('/')
    return (
        slug,
        str(data.get('title') or '').strip(),
        str(data.get('content_html') or ''),
        str(data.get('seo_title') or '').strip(),
        str(data.get('seo_description') or '').strip(),
        bool(data.get('is_published')),
        str(data.get('seo_keywords') or '').strip(),
        str(data.get('canonical_url') or '').strip(),
        str(data.get('status') or ('published' if data.get('is_published') else 'draft')).strip(),
        data.get('published_at'),
        data.get('modified_at'),
    )


def _post_payload(data):
    payload = dict(data)
    payload['slug'] = str(payload.get('slug') or '').strip().lower().strip('/')
    payload['title'] = str(payload.get('title') or '').strip()
    payload['categories'] = payload.get('categories') or [x.strip() for x in str(payload.get('category') or '').split(',') if x.strip()]
    payload['tags'] = payload.get('tags') or [x.strip() for x in str(payload.get('tag_text') or payload.get('tag') or '').split(',') if x.strip()]
    return payload


@admin_bp.route('/login', methods=['POST'])
@limiter.limit('5 per minute')
def admin_login():
    from database import find_user_by_email
    from auth import _verify_password

    email = (request.form.get('email') or '').strip()
    password = request.form.get('password') or ''

    if not email or not password:
        return jsonify({'status': 'error', 'error': 'Email and password required.'}), 400

    row = find_user_by_email(email)
    if not row or not _verify_password(row, password):
        # Brute-force defense is now handled by @_limiter().limit() above.
        return jsonify({'status': 'error', 'error': 'Invalid credentials.'}), 401
    if not row['is_admin']:
        return jsonify({'status': 'error', 'error': 'Not an admin account.'}), 403

    session.clear()
    session['user_id'] = row['id']
    session['user_name'] = row['username']
    session['user_email'] = row['email']
    session['is_admin'] = True

    return jsonify({
        'status': 'ok',
        'message': 'Admin logged in',
        'access_token': create_access_token(row['id']),
        'token_type': 'Bearer',
    })


@admin_bp.route('/logout', methods=['POST'])
def admin_logout():
    revoke_current_token()
    session.clear()
    return jsonify({'status': 'ok'})


@admin_bp.route('/dashboard')
def dashboard():
    if not _is_admin():
        return jsonify({'status': 'error', 'error': 'Unauthorized'}), 401
    users, stats = get_admin_dashboard_stats()
    return jsonify({
        'status': 'ok',
        'users': [dict(u) for u in users],
        'stats': stats,
    })


@admin_bp.route('/users/<int:uid>/activate', methods=['POST'])
def activate_user(uid):
    if not _is_admin():
        return jsonify({'status': 'error', 'error': 'Unauthorized'}), 403
    set_user_active(uid, True)
    from database import find_user_by_id
    user = find_user_by_id(uid)
    if user:
        try:
            from app import send_activation_email
            send_activation_email(user['username'], user['email'], True)
        except Exception:
            logging.exception('Failed to send activation email')
    return jsonify({'status': 'ok', 'message': 'User activated'})


@admin_bp.route('/users/<int:uid>/deactivate', methods=['POST'])
def deactivate_user(uid):
    if not _is_admin():
        return jsonify({'status': 'error', 'error': 'Unauthorized'}), 403
    set_user_active(uid, False)
    from database import find_user_by_id
    user = find_user_by_id(uid)
    if user:
        try:
            from app import send_activation_email
            send_activation_email(user['username'], user['email'], False)
        except Exception:
            logging.exception('Failed to send deactivation email')
    return jsonify({'status': 'ok', 'message': 'User deactivated'})


@admin_bp.route('/users/<int:uid>/delete', methods=['POST'])
def delete_user_route(uid):
    if not _is_admin():
        return jsonify({'status': 'error', 'error': 'Unauthorized'}), 403
    if uid == current_user_id():
        return jsonify({'status': 'error', 'error': 'Cannot delete yourself'}), 400
    delete_user(uid)
    return jsonify({'status': 'ok', 'message': 'User deleted'})


@admin_bp.route('/users/<int:uid>', methods=['PUT'])
def update_user_route(uid):
    if not _is_admin():
        return jsonify({'status': 'error', 'error': 'Unauthorized'}), 403
    data = _json_body()
    username = str(data.get('username') or '').strip()
    email = str(data.get('email') or '').strip()
    if len(username) < 2 or '@' not in email:
        return jsonify({'status': 'error', 'error': 'Valid username and email are required'}), 400
    try:
        update_user_admin(uid, username, email, data.get('is_active', True), data.get('is_admin', False))
    except Exception as exc:
        return jsonify({'status': 'error', 'error': str(exc)}), 400
    return jsonify({'status': 'ok', 'message': 'User updated'})


@admin_bp.route('/seo', methods=['GET', 'POST'])
def seo_settings():
    if not _is_admin():
        return jsonify({'status': 'error', 'error': 'Unauthorized'}), 403
    if request.method == 'POST':
        data = _json_body()
        allowed = {'site_title', 'site_description', 'site_keywords', 'canonical_url', 'default_og_image', 'og_title', 'og_description', 'twitter_card', 'twitter_title', 'twitter_description', 'twitter_image', 'robots_index', 'robots_follow', 'schema_type'}
        set_site_settings({key: str(data.get(key) or '') for key in allowed})
    return jsonify({'status': 'ok', 'settings': get_site_settings()})


@admin_bp.route('/pages', methods=['GET', 'POST'])
def pages():
    if not _is_admin():
        return jsonify({'status': 'error', 'error': 'Unauthorized'}), 403
    if request.method == 'POST':
        try:
            page_id = create_page(*_page_payload(_json_body()))
        except Exception as exc:
            logging.exception('Failed creating page')
            return jsonify({'status': 'error', 'error': str(exc)}), 400
        return jsonify({'status': 'ok', 'id': page_id}), 201
    return jsonify({'status': 'ok', 'pages': [dict(row) for row in list_pages()]})


@admin_bp.route('/pages/<int:page_id>', methods=['PUT', 'DELETE'])
def page_detail(page_id):
    if not _is_admin():
        return jsonify({'status': 'error', 'error': 'Unauthorized'}), 403
    if request.method == 'DELETE':
        delete_page(page_id)
        return jsonify({'status': 'ok'})
    try:
        update_page(page_id, *_page_payload(_json_body()))
    except Exception as exc:
        logging.exception('Failed updating page')
        return jsonify({'status': 'error', 'error': str(exc)}), 400
    return jsonify({'status': 'ok'})


@admin_bp.route('/posts', methods=['GET', 'POST'])
def posts():
    if not _is_admin():
        return jsonify({'status': 'error', 'error': 'Unauthorized'}), 403
    if request.method == 'POST':
        try:
            post_id = create_post(_post_payload(_json_body()))
        except Exception as exc:
            logging.exception('Failed creating post')
            return jsonify({'status': 'error', 'error': str(exc)}), 400
        return jsonify({'status': 'ok', 'id': post_id}), 201
    return jsonify({'status': 'ok', 'posts': list_posts()})


@admin_bp.route('/posts/<int:post_id>', methods=['PUT', 'DELETE'])
def post_detail(post_id):
    if not _is_admin():
        return jsonify({'status': 'error', 'error': 'Unauthorized'}), 403
    if request.method == 'DELETE':
        delete_post(post_id)
        return jsonify({'status': 'ok'})
    try:
        update_post(post_id, _post_payload(_json_body()))
    except Exception as exc:
        logging.exception('Failed updating post')
        return jsonify({'status': 'error', 'error': str(exc)}), 400
    return jsonify({'status': 'ok'})


@admin_bp.route('/<kind>', methods=['GET', 'POST'])
def taxonomy(kind):
    if kind not in ('categories', 'tags'):
        return jsonify({'status': 'error', 'error': 'Not found'}), 404
    if not _is_admin():
        return jsonify({'status': 'error', 'error': 'Unauthorized'}), 403
    if request.method == 'POST':
        data = _json_body()
        name = str(data.get('name') or '').strip()
        if not name:
            return jsonify({'status': 'error', 'error': 'Name is required'}), 400
        try:
            item_id = create_taxonomy(kind, name)
        except Exception as exc:
            return jsonify({'status': 'error', 'error': str(exc)}), 400
        return jsonify({'status': 'ok', 'id': item_id}), 201
    return jsonify({'status': 'ok', kind: [dict(row) for row in list_taxonomy(kind)]})
