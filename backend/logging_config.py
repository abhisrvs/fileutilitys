import os
import queue
import logging
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from flask import request, jsonify

LOG_DIR = os.path.dirname(os.path.abspath(__file__))

# Module-level listener reference so it doesn't get garbage-collected
# (QueueListener's background thread stops if the listener is collected).
_log_listener: QueueListener | None = None


class _SafeFormatter(logging.Formatter):
    """Formatter that supplies defaults for missing custom fields.

    The server log format expects every record to carry `remote_addr`,
    `request_method`, and `request_path`. The request hook only sets
    those for records that originate in a Flask request context — but
    the queue listener also receives Werkzeug's access log, Flask's
    startup messages, and our own `app.logger` calls without extras.
    Without this wrapper, every such record raises `KeyError: 'remote_addr'`.
    """
    _DEFAULTS = {
        'remote_addr': '-',
        'request_method': '-',
        'request_path': '-',
    }

    def format(self, record):
        for k, v in self._DEFAULTS.items():
            if not hasattr(record, k):
                record.__dict__[k] = v
        return super().format(record)


def setup_logging(app):
    # Server log - all requests and errors
    server_log = os.path.join(LOG_DIR, 'logs', 'server.log')
    os.makedirs(os.path.join(LOG_DIR, 'logs'), exist_ok=True)

    server_handler = RotatingFileHandler(server_log, maxBytes=5*1024*1024, backupCount=5)
    server_handler.setLevel(logging.INFO)
    server_handler.setFormatter(_SafeFormatter(
        '%(asctime)s [%(levelname)s] %(remote_addr)s %(request_method)s %(request_path)s - %(message)s'
    ))

    # Error log - only errors and critical
    error_log = os.path.join(LOG_DIR, 'logs', 'error.log')
    error_handler = RotatingFileHandler(error_log, maxBytes=5*1024*1024, backupCount=5)
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s: %(message)s [in %(pathname)s:%(lineno)d]'
    ))

    # App log - application events
    app_log = os.path.join(LOG_DIR, 'logs', 'app.log')
    app_handler = RotatingFileHandler(app_log, maxBytes=5*1024*1024, backupCount=3)
    app_handler.setLevel(logging.INFO)
    app_handler.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s: %(message)s'
    ))

    # Console output — only in dev, to avoid duplicate writes on production
    # (Passenger/cPanel already captures stdout).
    is_dev = bool(getattr(app, 'debug', False))
    handlers = [server_handler, error_handler, app_handler]
    if is_dev:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
        handlers.append(console_handler)

    # Wrap all disk handlers in a QueueHandler so the request thread only
    # enqueues a record (microseconds). A background daemon thread drains
    # the queue and writes to the rotating files.
    global _log_listener
    log_queue = queue.Queue(-1)
    queue_handler = QueueHandler(log_queue)
    queue_handler.setLevel(logging.INFO)
    app.logger.addHandler(queue_handler)
    logging.root.addHandler(queue_handler)
    app.logger.setLevel(logging.INFO)

    _log_listener = QueueListener(log_queue, *handlers, respect_handler_level=True)
    _log_listener.start()

    @app.after_request
    def log_request(response):
        try:
            extra = {
                'remote_addr': getattr(request, 'remote_addr', None) or '-',
                'request_method': request.method,
                'request_path': request.path,
            }
            app.logger.info(f'{request.method} {request.path} {response.status_code}', extra=extra)
        except Exception:
            pass
        return response

    @app.errorhandler(404)
    def not_found_error(error):
        app.logger.warning(f'404 Not Found: {request.path}')
        return jsonify({'error': 'Not found'}), 404

    @app.errorhandler(500)
    def internal_error(error):
        app.logger.error(f'500 Internal Server Error: {request.path}', exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500

    @app.errorhandler(413)
    def too_large(error):
        app.logger.warning(f'413 Request Entity Too Large: {request.path}')
        return {'error': 'File too large'}, 413

    return app
