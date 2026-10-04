"""Rate limiter singleton, decoupled from app/auth/admin to avoid circular
imports. The limiter is created without an app, then `init_app(app)` is
called once from app.py after the app and blueprints are fully constructed.
Other modules (auth, admin) import `limiter` from here.
"""
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[],
    storage_uri='memory://',
)
