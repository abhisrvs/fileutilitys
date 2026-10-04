import os
import re
import sqlite3
from datetime import datetime, timezone
from flask import g

try:
    import pymysql
    from pymysql.cursors import DictCursor
except ImportError:
    pymysql = None
    DictCursor = None

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
USER_FILES_DIR_NAME = 'user_files'

MAX_FILES_PER_USER = int(os.environ.get('MAX_FILES_PER_USER', '100'))
MAX_TOTAL_BYTES = int(os.environ.get('MAX_TOTAL_MB', '500')) * 1024 * 1024


def _mysql_enabled():
    host = os.environ.get('MYSQL_HOST', '').strip()
    user = os.environ.get('MYSQL_USER', '').strip()
    database = os.environ.get('MYSQL_DATABASE', '').strip()
    return bool(host) and bool(user) and bool(database) and pymysql is not None


def _sqlite_db_path():
    return os.path.join(BASE_DIR, 'users.db')


def _normalize_sqlite_query(query):
    sql = query
    sql = sql.replace('UTC_TIMESTAMP()', 'CURRENT_TIMESTAMP')
    sql = sql.replace('TIMESTAMPDIFF(SECOND, finished_at, UTC_TIMESTAMP())', "strftime('%s', 'now') - strftime('%s', finished_at)")
    sql = sql.replace('TIMESTAMPDIFF(SECOND, finished_at, CURRENT_TIMESTAMP)', "strftime('%s', 'now') - strftime('%s', finished_at)")
    sql = sql.replace('VALUES(setting_value)', 'excluded.setting_value')
    sql = sql.replace('ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)', 'ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value')
    sql = sql.replace('ON DUPLICATE KEY UPDATE setting_value = excluded.setting_value', 'ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value')
    return sql


class _MySQLDatabase:
    def __init__(self):
        if pymysql is None:
            raise RuntimeError('PyMySQL is required when MYSQL_HOST is configured')
        self._connection = pymysql.connect(
            host=os.environ.get('MYSQL_HOST', '127.0.0.1'),
            port=int(os.environ.get('MYSQL_PORT', '3306')),
            user=os.environ.get('MYSQL_USER', 'root'),
            password=os.environ.get('MYSQL_PASSWORD', ''),

            database=os.environ.get('MYSQL_DATABASE', 'utility_system'),
            charset='utf8mb4',
            cursorclass=DictCursor,
            autocommit=False,
            connect_timeout=10,
            read_timeout=15,
            write_timeout=15,
        )

    def execute(self, query, params=()):
        cursor = self._connection.cursor()
        sql = query.replace('?', '%s')
        cursor.execute(sql, params)
        return cursor

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        try:
            self._connection.close()
        except Exception:
            pass


class _SQLiteDatabase:
    def __init__(self):
        self._connection = sqlite3.connect(_sqlite_db_path(), timeout=30, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute('PRAGMA journal_mode=WAL;')

    def execute(self, query, params=()):
        cursor = self._connection.cursor()
        cursor.execute(_normalize_sqlite_query(query), params)
        return cursor

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        try:
            self._connection.close()
        except Exception:
            pass


def _new_db():
    if _mysql_enabled():
        try:
            db = _MySQLDatabase()
            db.execute('SELECT 1').fetchone()
            return db
        except Exception as exc:
            print(f'[DB] MySQL connection unavailable; falling back to SQLite ({exc})')
    return _SQLiteDatabase()


def get_db():
    if 'db' not in g:
        g.db = _new_db()
    return g.db


def close_db(exc=None):
    db = g.pop('db', None)
    if db is not None:
        try:
            if exc is not None:
                db.rollback()
        except Exception:
            pass
        db.close()


def init_db():
    db = _new_db()
    try:
        if isinstance(db, _SQLiteDatabase):
            db.execute('''CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT,
                is_admin INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                provider TEXT NOT NULL DEFAULT 'local',
                provider_id TEXT,
                reset_token TEXT,
                reset_token_expiry DATETIME,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )''')
            db.execute('''CREATE TABLE IF NOT EXISTS user_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                orig_name TEXT NOT NULL,
                stored_name TEXT NOT NULL UNIQUE,
                mimetype TEXT,
                size INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )''')
            db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                user_id INTEGER,
                kind TEXT NOT NULL,
                params_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                progress REAL NOT NULL DEFAULT 0,
                result_filename TEXT,
                result_mimetype TEXT,
                result_size INTEGER,
                error TEXT,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at DATETIME,
                finished_at DATETIME
            )''')
            db.execute('CREATE INDEX IF NOT EXISTS idx_jobs_user_created ON jobs (user_id, created_at)')
            db.execute('CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status)')
            db.execute('''CREATE TABLE IF NOT EXISTS site_settings (
                setting_key TEXT NOT NULL PRIMARY KEY,
                setting_value TEXT NOT NULL
            )''')
            db.execute('''CREATE TABLE IF NOT EXISTS revoked_tokens (
                jti TEXT NOT NULL PRIMARY KEY,
                expires_at DATETIME NOT NULL
            )''')
            db.execute('''CREATE TABLE IF NOT EXISTS pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                content_html TEXT NOT NULL,
                seo_title TEXT NOT NULL DEFAULT '',
                seo_description TEXT NOT NULL,
                is_published INTEGER NOT NULL DEFAULT 0,
                seo_keywords TEXT NOT NULL DEFAULT '',
                canonical_url TEXT NOT NULL DEFAULT '',
                robots_index TEXT NOT NULL DEFAULT '1',
                robots_follow TEXT NOT NULL DEFAULT '1',
                status TEXT NOT NULL DEFAULT 'draft',
                published_at DATETIME NULL,
                modified_at DATETIME NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )''')
            db.execute('''CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                content_html TEXT NOT NULL,
                seo_title TEXT NOT NULL DEFAULT '',
                seo_description TEXT NOT NULL,
                seo_keywords TEXT NOT NULL DEFAULT '',
                canonical_url TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'draft',
                is_published INTEGER NOT NULL DEFAULT 0,
                published_at DATETIME NULL,
                modified_at DATETIME NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )''')
            db.execute('''CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                slug TEXT NOT NULL UNIQUE
            )''')
            db.execute('''CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                slug TEXT NOT NULL UNIQUE
            )''')
            db.execute('''CREATE TABLE IF NOT EXISTS post_categories (
                post_id INTEGER NOT NULL,
                category_id INTEGER NOT NULL,
                PRIMARY KEY (post_id, category_id),
                FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE,
                FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE
            )''')
            db.execute('''CREATE TABLE IF NOT EXISTS post_tags (
                post_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL,
                PRIMARY KEY (post_id, tag_id),
                FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE,
                FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
            )''')
        else:
            db.execute('''CREATE TABLE IF NOT EXISTS users (
                id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(255) NOT NULL,
                email VARCHAR(255) NOT NULL UNIQUE,
                password_hash VARCHAR(255),
                is_admin TINYINT NOT NULL DEFAULT 0,
                is_active TINYINT NOT NULL DEFAULT 1,
                provider VARCHAR(32) NOT NULL DEFAULT 'local',
                provider_id VARCHAR(255),
                reset_token VARCHAR(255),
                reset_token_expiry DATETIME,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')
            db.execute('''CREATE TABLE IF NOT EXISTS user_files (
                id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                user_id INT UNSIGNED NOT NULL,
                orig_name VARCHAR(255) NOT NULL,
                stored_name VARCHAR(255) NOT NULL UNIQUE,
                mimetype VARCHAR(255),
                size BIGINT NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT fk_user_files_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')
            db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id VARCHAR(64) NOT NULL PRIMARY KEY,
                user_id INT UNSIGNED,
                kind VARCHAR(64) NOT NULL,
                params_json LONGTEXT NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'pending',
                progress DOUBLE NOT NULL DEFAULT 0,
                result_filename VARCHAR(255),
                result_mimetype VARCHAR(255),
                result_size BIGINT,
                error TEXT,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at DATETIME,
                finished_at DATETIME,
                INDEX idx_jobs_user_created (user_id, created_at),
                INDEX idx_jobs_status (status)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')
            db.execute('''CREATE TABLE IF NOT EXISTS site_settings (
                setting_key VARCHAR(100) NOT NULL PRIMARY KEY,
                setting_value TEXT NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')
            db.execute('''CREATE TABLE IF NOT EXISTS revoked_tokens (
                jti VARCHAR(64) NOT NULL PRIMARY KEY,
                expires_at DATETIME NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')
            db.execute('''CREATE TABLE IF NOT EXISTS pages (
                id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                slug VARCHAR(191) NOT NULL UNIQUE,
                title VARCHAR(255) NOT NULL,
                content_html LONGTEXT NOT NULL,
                seo_title VARCHAR(255) NOT NULL DEFAULT '',
                seo_description TEXT NOT NULL,
                is_published TINYINT NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')
            db.execute('''CREATE TABLE IF NOT EXISTS posts (
                id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                slug VARCHAR(191) NOT NULL UNIQUE,
                title VARCHAR(255) NOT NULL,
                content_html LONGTEXT NOT NULL,
                seo_title VARCHAR(255) NOT NULL DEFAULT '',
                seo_description TEXT NOT NULL,
                seo_keywords VARCHAR(255) NOT NULL DEFAULT '',
                canonical_url VARCHAR(500) NOT NULL DEFAULT '',
                status VARCHAR(32) NOT NULL DEFAULT 'draft',
                is_published TINYINT NOT NULL DEFAULT 0,
                published_at DATETIME NULL,
                modified_at DATETIME NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')
            db.execute('CREATE TABLE IF NOT EXISTS categories (id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY, name VARCHAR(191) NOT NULL UNIQUE, slug VARCHAR(191) NOT NULL UNIQUE) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4')
            db.execute('CREATE TABLE IF NOT EXISTS tags (id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY, name VARCHAR(191) NOT NULL UNIQUE, slug VARCHAR(191) NOT NULL UNIQUE) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4')
            db.execute('CREATE TABLE IF NOT EXISTS post_categories (post_id INT UNSIGNED NOT NULL, category_id INT UNSIGNED NOT NULL, PRIMARY KEY(post_id, category_id), FOREIGN KEY(post_id) REFERENCES posts(id) ON DELETE CASCADE, FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE CASCADE) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4')
            db.execute('CREATE TABLE IF NOT EXISTS post_tags (post_id INT UNSIGNED NOT NULL, tag_id INT UNSIGNED NOT NULL, PRIMARY KEY(post_id, tag_id), FOREIGN KEY(post_id) REFERENCES posts(id) ON DELETE CASCADE, FOREIGN KEY(tag_id) REFERENCES tags(id) ON DELETE CASCADE) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4')

        admin_email = (os.environ.get('ADMIN_EMAIL') or '').strip()
        admin_pass = os.environ.get('ADMIN_PASSWORD') or ''
        if admin_email and admin_pass:
            from werkzeug.security import generate_password_hash
            existing = db.execute('SELECT id FROM users WHERE email = ?', (admin_email,)).fetchone()
            if not existing:
                db.execute('INSERT INTO users (username, email, password_hash, is_admin, is_active) VALUES (?, ?, ?, 1, 1)', ('Admin', admin_email, generate_password_hash(admin_pass)))
                print(f'[DB] Seeded admin account: {admin_email}')
            else:
                db.execute('UPDATE users SET is_admin = 1, is_active = 1 WHERE email = ?', (admin_email,))

        admin_exists = db.execute('SELECT COUNT(*) AS count FROM users WHERE is_admin = 1').fetchone()['count']
        if not admin_exists:
            first_user = db.execute('SELECT id FROM users ORDER BY id ASC LIMIT 1').fetchone()
            if first_user:
                db.execute('UPDATE users SET is_admin = 1, is_active = 1 WHERE id = ?', (first_user['id'],))
                print(f'[DB] Auto-promoted user id {first_user["id"]} to admin')

        db.commit()
    finally:
        db.close()


def _normalize_by_db(db, query):
    if isinstance(db, _SQLiteDatabase):
        return _normalize_sqlite_query(query)
    return query.replace('?', '%s')


def find_user_by_email(email):
    return get_db().execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()


def find_user_by_provider(provider, provider_id):
    return get_db().execute('SELECT * FROM users WHERE provider = ? AND provider_id = ?', (provider, provider_id)).fetchone()


def find_user_by_reset_token(token):
    return get_db().execute('SELECT * FROM users WHERE reset_token = ?', (token,)).fetchone()


def set_reset_token(user_id, token, expiry):
    db = get_db()
    db.execute('UPDATE users SET reset_token = ?, reset_token_expiry = ? WHERE id = ?', (token, expiry, user_id))
    db.commit()


def clear_reset_token(user_id):
    db = get_db()
    db.execute('UPDATE users SET reset_token = NULL, reset_token_expiry = NULL WHERE id = ?', (user_id,))
    db.commit()


def create_user(username, email, password_hash, *, is_active=True, provider='local', provider_id=None):
    db = get_db()
    cur = db.execute(
        'INSERT INTO users (username, email, password_hash, is_admin, is_active, provider, provider_id) VALUES (?, ?, ?, 0, ?, ?, ?)',
        (username.strip(), email.strip(), password_hash, int(bool(is_active)), provider, provider_id),
    )
    db.commit()
    return cur.lastrowid


def update_password(user_id, password_hash):
    db = get_db()
    db.execute('UPDATE users SET password_hash = ? WHERE id = ?', (password_hash, user_id))
    db.commit()


def update_user_provider(user_id, provider, provider_id):
    db = get_db()
    db.execute('UPDATE users SET provider = ?, provider_id = ? WHERE id = ?', (provider, provider_id, user_id))
    db.commit()


def update_user_profile(user_id, username):
    db = get_db()
    db.execute('UPDATE users SET username = ? WHERE id = ?', (username.strip(), user_id))
    db.commit()


def find_user_by_id(user_id):
    return get_db().execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()


def is_token_revoked(jti):
    return get_db().execute(
        'SELECT 1 FROM revoked_tokens WHERE jti = ?', (jti,)
    ).fetchone() is not None


def revoke_token(jti, expires_at):
    db = get_db()
    db.execute('DELETE FROM revoked_tokens WHERE expires_at < UTC_TIMESTAMP()')
    db.execute(
        'INSERT INTO revoked_tokens (jti, expires_at) VALUES (?, ?)',
        (jti, datetime.fromtimestamp(int(expires_at), timezone.utc).strftime('%Y-%m-%d %H:%M:%S')),
    )
    db.commit()


def set_user_active(user_id, active):
    db = get_db()
    db.execute('UPDATE users SET is_active = ? WHERE id = ?', (1 if active else 0, user_id))
    db.commit()


def delete_user(user_id):
    db = get_db()
    db.execute('DELETE FROM user_files WHERE user_id = ?', (user_id,))
    db.execute('DELETE FROM users WHERE id = ?', (user_id,))
    db.commit()


def delete_user_account(user_id):
    db = get_db()
    row = db.execute('SELECT username, email, is_admin FROM users WHERE id = ?', (user_id,)).fetchone()
    if not row:
        return None
    info = dict(row)
    db.execute('DELETE FROM user_files WHERE user_id = ?', (user_id,))
    db.execute('DELETE FROM users WHERE id = ?', (user_id,))
    db.commit()
    storage = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'user_files', str(user_id))
    if os.path.isdir(storage):
        import shutil
        shutil.rmtree(storage, ignore_errors=True)
    return info


def list_all_users():
    return get_db().execute('SELECT id, username, email, is_admin, is_active, created_at FROM users ORDER BY created_at DESC').fetchall()


def user_count():
    return get_db().execute('SELECT COUNT(*) FROM users').fetchone()[0]


def active_user_count():
    return get_db().execute('SELECT COUNT(*) FROM users WHERE is_active = 1').fetchone()[0]


def admin_count():
    return get_db().execute('SELECT COUNT(*) FROM users WHERE is_admin = 1').fetchone()[0]


def set_user_admin(user_id, is_admin=True):
    db = get_db()
    db.execute('UPDATE users SET is_admin = ? WHERE id = ?', (1 if is_admin else 0, user_id))
    db.commit()


def update_user_admin(user_id, username, email, is_active, is_admin):
    db = get_db()
    db.execute('UPDATE users SET username = ?, email = ?, is_active = ?, is_admin = ? WHERE id = ?', (username.strip(), email.strip(), int(bool(is_active)), int(bool(is_admin)), user_id))
    db.commit()


def get_admin_dashboard_stats():
    db = get_db()
    stats_row = db.execute(
        'SELECT '
        '(SELECT COUNT(*) FROM users) AS total_users, '
        '(SELECT COUNT(*) FROM users WHERE is_active = 1) AS active_users, '
        '(SELECT COUNT(*) FROM user_files) AS total_files, '
        '(SELECT COALESCE(SUM(size), 0) FROM user_files) AS total_size'
    ).fetchone()
    users = db.execute(
        'SELECT id, username, email, is_admin, is_active, created_at FROM users ORDER BY created_at DESC'
    ).fetchall()
    return users, {
        'total_users': stats_row['total_users'],
        'active_users': stats_row['active_users'],
        'total_files': stats_row['total_files'],
        'total_size_mb': round(stats_row['total_size'] / (1024 * 1024), 2),
    }


def user_usage(user_id):
    row = get_db().execute(
        'SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS total FROM user_files WHERE user_id = ?', (user_id,)).fetchone()
    return row['n'], row['total']


def can_save_file(user_id, incoming_size):
    n, total = user_usage(user_id)
    return n < MAX_FILES_PER_USER and (total + max(incoming_size, 0)) <= MAX_TOTAL_BYTES


def add_user_file(user_id, orig_name, stored_name, mimetype, size):
    db = get_db()
    cur = db.execute('INSERT INTO user_files (user_id, orig_name, stored_name, mimetype, size) VALUES (?, ?, ?, ?, ?)', (user_id, orig_name, stored_name, mimetype, size))
    db.commit()
    return cur.lastrowid


def list_user_files(user_id):
    return get_db().execute('SELECT * FROM user_files WHERE user_id = ? ORDER BY created_at DESC, id DESC', (user_id,)).fetchall()


def get_user_file(file_id, user_id):
    return get_db().execute('SELECT * FROM user_files WHERE id = ? AND user_id = ?', (file_id, user_id)).fetchone()


def delete_user_file(file_id, user_id):
    db = get_db()
    cur = db.execute('DELETE FROM user_files WHERE id = ? AND user_id = ?', (file_id, user_id))
    db.commit()
    return cur.rowcount > 0


def total_files_count():
    return get_db().execute('SELECT COUNT(*) FROM user_files').fetchone()[0]


def total_files_size():
    return get_db().execute('SELECT COALESCE(SUM(size), 0) FROM user_files').fetchone()[0]


def get_site_settings():
    rows = get_db().execute('SELECT setting_key, setting_value FROM site_settings').fetchall()
    return {row['setting_key']: row['setting_value'] for row in rows}


def set_site_settings(settings):
    db = get_db()
    for key, value in settings.items():
        db.execute('INSERT INTO site_settings (setting_key, setting_value) VALUES (?, ?) ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)', (key, value))
    db.commit()


def list_pages():
    return get_db().execute('SELECT * FROM pages ORDER BY updated_at DESC, id DESC').fetchall()


def get_page_by_slug(slug, published_only=True):
    query = 'SELECT * FROM pages WHERE slug = ?'
    params = [slug]
    if published_only:
        query += ' AND is_published = 1'
    return get_db().execute(query, params).fetchone()


def create_page(slug, title, content_html, seo_title, seo_description, is_published, seo_keywords='', canonical_url='', status='draft', published_at=None, modified_at=None):
    db = get_db()
    cur = db.execute(
        'INSERT INTO pages (slug, title, content_html, seo_title, seo_description, is_published, seo_keywords, canonical_url, status, published_at, modified_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (slug, title, content_html, seo_title, seo_description, int(bool(is_published)), seo_keywords, canonical_url, status, published_at, modified_at),
    )
    db.commit()
    return cur.lastrowid


def update_page(page_id, slug, title, content_html, seo_title, seo_description, is_published, seo_keywords='', canonical_url='', status='draft', published_at=None, modified_at=None):
    db = get_db()
    db.execute(
        'UPDATE pages SET slug = ?, title = ?, content_html = ?, seo_title = ?, seo_description = ?, is_published = ?, seo_keywords = ?, canonical_url = ?, status = ?, published_at = ?, modified_at = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
        (slug, title, content_html, seo_title, seo_description, int(bool(is_published)), seo_keywords, canonical_url, status, published_at, modified_at, page_id),
    )
    db.commit()


def delete_page(page_id):
    db = get_db()
    db.execute('DELETE FROM pages WHERE id = ?', (page_id,))
    db.commit()


def list_taxonomy(kind):
    if kind not in ('categories', 'tags'):
        raise ValueError('Invalid taxonomy')
    return get_db().execute(f'SELECT * FROM {kind} ORDER BY name').fetchall()


def create_taxonomy(kind, name):
    if kind not in ('categories', 'tags'):
        raise ValueError('Invalid taxonomy')
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    db = get_db()
    cur = db.execute(f'INSERT INTO {kind} (name, slug) VALUES (?, ?)', (name.strip(), slug))
    db.commit()
    return cur.lastrowid


def list_posts():
    db = get_db()
    rows = db.execute(
        'SELECT p.*, '
        'GROUP_CONCAT(DISTINCT c.name ORDER BY c.name SEPARATOR ",") AS category_names, '
        'GROUP_CONCAT(DISTINCT t.name ORDER BY t.name SEPARATOR ",") AS tag_names '
        'FROM posts p '
        'LEFT JOIN post_categories pc ON pc.post_id = p.id '
        'LEFT JOIN categories c ON c.id = pc.category_id '
        'LEFT JOIN post_tags pt ON pt.post_id = p.id '
        'LEFT JOIN tags t ON t.id = pt.tag_id '
        'GROUP BY p.id '
        'ORDER BY p.updated_at DESC, p.id DESC'
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item['categories'] = [x.strip() for x in (item.pop('category_names', '') or '').split(',') if x.strip()]
        item['tags'] = [x.strip() for x in (item.pop('tag_names', '') or '').split(',') if x.strip()]
        result.append(item)
    return result


def get_post_by_slug(slug, published_only=True):
    query = (
        'SELECT p.*, '
        'GROUP_CONCAT(DISTINCT c.name ORDER BY c.name SEPARATOR ",") AS category_names, '
        'GROUP_CONCAT(DISTINCT t.name ORDER BY t.name SEPARATOR ",") AS tag_names '
        'FROM posts p '
        'LEFT JOIN post_categories pc ON pc.post_id = p.id '
        'LEFT JOIN categories c ON c.id = pc.category_id '
        'LEFT JOIN post_tags pt ON pt.post_id = p.id '
        'LEFT JOIN tags t ON t.id = pt.tag_id '
        'WHERE p.slug = ?'
    )
    params = [slug]
    if published_only:
        query += " AND p.is_published = 1 AND (p.published_at IS NULL OR p.published_at <= CURRENT_TIMESTAMP)"
    query += ' GROUP BY p.id'
    row = get_db().execute(query, params).fetchone()
    if not row:
        return None
    item = dict(row)
    item['categories'] = [x.strip() for x in (item.pop('category_names', '') or '').split(',') if x.strip()]
    item['tags'] = [x.strip() for x in (item.pop('tag_names', '') or '').split(',') if x.strip()]
    return item


def create_post(data):
    db = get_db()
    cur = db.execute('INSERT INTO posts (slug, title, content_html, seo_title, seo_description, seo_keywords, canonical_url, status, is_published, published_at, modified_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (data['slug'], data['title'], data.get('content_html', ''), data.get('seo_title', ''), data.get('seo_description', ''), data.get('seo_keywords', ''), data.get('canonical_url', ''), data.get('status', 'draft'), int(data.get('is_published', False)), data.get('published_at'), data.get('modified_at')))
    post_id = cur.lastrowid
    _set_post_terms(db, post_id, 'categories', data.get('categories', []))
    _set_post_terms(db, post_id, 'tags', data.get('tags', []))
    db.commit()
    return post_id


def update_post(post_id, data):
    db = get_db()
    db.execute('UPDATE posts SET slug = ?, title = ?, content_html = ?, seo_title = ?, seo_description = ?, seo_keywords = ?, canonical_url = ?, status = ?, is_published = ?, published_at = ?, modified_at = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?', (data['slug'], data['title'], data.get('content_html', ''), data.get('seo_title', ''), data.get('seo_description', ''), data.get('seo_keywords', ''), data.get('canonical_url', ''), data.get('status', 'draft'), int(data.get('is_published', False)), data.get('published_at'), data.get('modified_at'), post_id))
    _set_post_terms(db, post_id, 'categories', data.get('categories', []))
    _set_post_terms(db, post_id, 'tags', data.get('tags', []))
    db.commit()


def _set_post_terms(db, post_id, kind, names):
    join_table = 'post_categories' if kind == 'categories' else 'post_tags'
    term_table = kind
    term_key = 'category_id' if kind == 'categories' else 'tag_id'
    db.execute(f'DELETE FROM {join_table} WHERE post_id = ?', (post_id,))
    for name in names or []:
        row = db.execute(f'SELECT id FROM {term_table} WHERE name = ?', (str(name).strip(),)).fetchone()
        if row:
            db.execute(f'INSERT INTO {join_table} (post_id, {term_key}) VALUES (?, ?)', (post_id, row['id']))


def delete_post(post_id):
    db = get_db()
    db.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    db.commit()


def insert_job(job_id, user_id, kind, params_json, result_filename, result_mimetype):
    db = get_db()
    db.execute(
        'INSERT INTO jobs (id, user_id, kind, params_json, status, result_filename, result_mimetype) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (job_id, user_id, kind, params_json, 'pending', result_filename, result_mimetype),
    )
    db.commit()


def get_job_row(job_id):
    return get_db().execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()


def mark_job_running(job_id):
    db = get_db()
    db.execute('UPDATE jobs SET status = ?, started_at = CURRENT_TIMESTAMP WHERE id = ?', ('running', job_id))
    db.commit()


def mark_job_done(job_id, *, result_filename, result_mimetype, result_size):
    db = get_db()
    db.execute(
        'UPDATE jobs SET status = ?, finished_at = CURRENT_TIMESTAMP, result_size = ?, result_filename = ?, result_mimetype = ?, progress = 1, error = NULL WHERE id = ?',
        ('done', result_size, result_filename, result_mimetype, job_id),
    )
    db.commit()


def mark_job_failed(job_id, error_message):
    db = get_db()
    db.execute('UPDATE jobs SET status = ?, finished_at = CURRENT_TIMESTAMP, error = ? WHERE id = ?', ('failed', error_message[:1000] if error_message else None, job_id))
    db.commit()


def set_job_progress(job_id, progress):
    try:
        db = get_db()
        db.execute('UPDATE jobs SET progress = ? WHERE id = ?', (max(0.0, min(1.0, progress)), job_id))
        db.commit()
    except Exception:
        pass


def sweep_expired_jobs(ttl_seconds):
    db = get_db()
    rows = db.execute(
        "SELECT id, result_filename FROM jobs WHERE status IN ('done', 'failed') AND finished_at IS NOT NULL AND (strftime('%s', 'now') - strftime('%s', finished_at)) > ?",
        (ttl_seconds,),
    ).fetchall()
    if not rows:
        return []
    ids = [r['id'] for r in rows]
    placeholders = ','.join('?' for _ in ids)
    db.execute(f'DELETE FROM jobs WHERE id IN ({placeholders})', ids)
    db.commit()
    return [(r['id'], r['result_filename']) for r in rows]
