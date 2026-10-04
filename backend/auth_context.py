import secrets
from datetime import datetime, timedelta, timezone

import jwt
from flask import current_app, g, request, session

from database import find_user_by_id, is_token_revoked, revoke_token

ACCESS_TOKEN_LIFETIME = timedelta(hours=8)


def create_access_token(user_id):
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            'sub': str(user_id),
            'iat': now,
            'exp': now + ACCESS_TOKEN_LIFETIME,
            'jti': secrets.token_urlsafe(24),
        },
        current_app.secret_key,
        algorithm='HS256',
    )
    return token.decode('utf-8') if isinstance(token, bytes) else token


def _decode_token(token):
    return jwt.decode(
        token,
        current_app.secret_key,
        algorithms=['HS256'],
        options={'require': ['exp', 'iat', 'sub', 'jti']},
    )


def revoke_current_token():
    scheme, separator, token = request.headers.get('Authorization', '').partition(' ')
    if scheme.lower() != 'bearer' or not separator or not token:
        return
    try:
        claims = _decode_token(token)
    except jwt.InvalidTokenError:
        return
    revoke_token(claims['jti'], claims['exp'])


def authenticated_user():
    if hasattr(g, '_authenticated_user'):
        return g._authenticated_user

    scheme, separator, token = request.headers.get('Authorization', '').partition(' ')
    if request.headers.get('Authorization'):
        if scheme.lower() != 'bearer' or not separator or not token:
            g._authenticated_user = None
            return None
        try:
            claims = _decode_token(token)
            user_id = int(claims['sub'])
            if is_token_revoked(claims['jti']):
                g._authenticated_user = None
                return None
            user = find_user_by_id(user_id)
        except (jwt.InvalidTokenError, TypeError, ValueError):
            user = None
    else:
        user_id = session.get('user_id')
        user = find_user_by_id(user_id) if user_id else None

    if user and not user['is_active']:
        user = None
    g._authenticated_user = user
    return user


def current_user_id():
    user = authenticated_user()
    return user['id'] if user else None


def current_user_is_admin():
    user = authenticated_user()
    return bool(user and user['is_admin'])
