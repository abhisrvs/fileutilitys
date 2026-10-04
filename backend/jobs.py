"""Background-job runner for conversion/compression tasks.

Architecture: an in-process ThreadPoolExecutor owns the worker threads; a row
in the `jobs` MySQL table is the source of truth for status, progress, and
the result-file path. Routes enqueue with `enqueue_job()` and return 202 +
{job_id, status_url} immediately. Clients poll `GET /api/jobs/<id>` and then
`GET /api/jobs/<id>/download`.

This module is intentionally the only place that knows about the executor
and the on-disk result layout, so we can swap to Celery + Redis later by
rewriting `enqueue_job()` and `_run_job()` without touching routes or workers.
"""
import json
import logging
import os
import secrets
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor

from flask import current_app
from auth_context import current_user_id

from database import (
    get_job_row,
    insert_job,
    mark_job_done,
    mark_job_failed,
    mark_job_running,
    set_job_progress,
    sweep_expired_jobs,
)

log = logging.getLogger(__name__)

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()
_sweep_timer: threading.Timer | None = None


def _periodic_sweep(app, ttl_seconds):
    """Background sweep that runs every 5 minutes to clean up expired jobs."""
    global _sweep_timer
    try:
        with app.app_context():
            expired = sweep_expired_jobs(ttl_seconds)
            if expired:
                _cleanup_result_dirs(expired)
                log.info('Sweeped %d expired jobs', len(expired))
    except Exception:
        log.exception('periodic sweep failed')
    _sweep_timer = threading.Timer(300, _periodic_sweep, args=(app, ttl_seconds))
    _sweep_timer.daemon = True
    _sweep_timer.start()


def init_worker(app):
    """Create the global thread pool. Call once at app startup."""
    global _executor
    max_workers = int(os.environ.get('JOB_WORKERS') or app.config.get('JOB_WORKERS') or 3)
    job_ttl = int(os.environ.get('JOB_TTL_SECONDS') or app.config.get('JOB_TTL_SECONDS') or 3600)
    out_root = os.environ.get('JOB_OUTPUT_DIR') or app.config.get('JOB_OUTPUT_DIR')
    if not out_root:
        out_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'job_outputs')
    app.config['JOB_WORKERS'] = max_workers
    app.config['JOB_TTL_SECONDS'] = job_ttl
    app.config['JOB_OUTPUT_DIR'] = out_root
    os.makedirs(out_root, exist_ok=True)

    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=False, cancel_futures=True)
        _executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='job-worker')
    log.info('Job executor started: max_workers=%d ttl=%ds out=%s', max_workers, job_ttl, out_root)

    # Start periodic sweep timer
    global _sweep_timer
    if _sweep_timer is not None:
        _sweep_timer.cancel()
    _sweep_timer = threading.Timer(300, _periodic_sweep, args=(app, job_ttl))
    _sweep_timer.daemon = True
    _sweep_timer.start()


def _executor_singleton() -> ThreadPoolExecutor:
    if _executor is None:
        raise RuntimeError('Job executor is not initialized; call init_worker(app) at startup.')
    return _executor


def job_root_dir() -> str:
    return current_app.config['JOB_OUTPUT_DIR']


def job_in_dir(job_id: str) -> str:
    p = os.path.join(job_root_dir(), job_id, 'in')
    os.makedirs(p, exist_ok=True)
    return p


def job_out_dir(job_id: str) -> str:
    p = os.path.join(job_root_dir(), job_id, 'out')
    os.makedirs(p, exist_ok=True)
    return p


def enqueue_job(*, user_id, kind, params, result_filename, result_mimetype,
                save_to_account: bool) -> str:
    """Create a 'pending' job row, copy caller-supplied input files into
    backend/job_outputs/<job_id>/in/, and submit the worker. Returns job_id.

    `params` is a dict; we serialize it as JSON into the row so the worker
    (which may start in another thread) has everything it needs without the
    Flask request context.
    """
    job_id = secrets.token_urlsafe(16)
    in_dir = job_in_dir(job_id)
    inputs = params.pop('_input_files', [])
    for src_path, dest_name in inputs:
        if not src_path or not os.path.exists(src_path):
            continue
        dest = os.path.join(in_dir, dest_name)
        try:
            shutil.copyfile(src_path, dest)
        except Exception:
            log.exception('Failed to stage input file %s -> %s', src_path, dest)

    params['save'] = bool(save_to_account)
    insert_job(
        job_id=job_id,
        user_id=user_id,
        kind=kind,
        params_json=json.dumps(params),
        result_filename=result_filename,
        result_mimetype=result_mimetype,
    )
    app = current_app._get_current_object()
    _executor_singleton().submit(_run_job, app, job_id)
    return job_id


def _run_job(app, job_id: str) -> None:
    """Worker entry point. Dispatch by kind, then write back status."""
    import worker  # imported here so module-level imports stay light

    with app.app_context():
        row = get_job_row(job_id)
        if not row:
            log.error('Job %s vanished before run', job_id)
            return
        try:
            mark_job_running(job_id)
        except Exception:
            log.exception('mark_job_running failed for %s', job_id)
        params = json.loads(row['params_json']) if row['params_json'] else {}
        kind = row['kind']
        user_id = row['user_id']
        try:
            runner = getattr(worker, f'run_{kind}', None)
            if runner is None:
                raise RuntimeError(f'Unknown job kind: {kind}')

            in_dir = os.path.join(job_root_dir(), job_id, 'in')
            out_dir = job_out_dir(job_id)
            result_path, result_filename, mimetype, result_size = runner(
                params=params,
                in_dir=in_dir,
                out_dir=out_dir,
                progress=lambda p: set_job_progress(job_id, p),
            )

            # Optionally copy to the user's account
            if params.get('save') and user_id:
                try:
                    from files import save_output_for_user
                    save_output_for_user(result_path, result_filename, mimetype, user_id=user_id)
                except Exception:
                    log.exception('Save-to-account failed in worker for job %s', job_id)

            mark_job_done(
                job_id,
                result_filename=result_filename,
                result_mimetype=mimetype,
                result_size=result_size,
            )
        except Exception as e:
            log.exception('Job %s failed', job_id)
            try:
                mark_job_failed(job_id, str(e) or e.__class__.__name__)
            except Exception:
                log.exception('mark_job_failed itself failed for %s', job_id)


def get_job_response(job_id: str):
    """Return the public job envelope (dict) or None if the job doesn't exist
    or doesn't belong to the current user (we treat both as 'not found' to
    avoid leaking IDs).
    """
    if not job_id or len(job_id) > 64:
        return None

    row = get_job_row(job_id)
    if not row:
        return None
    # A job tied to a specific user is only readable by that user. Anonymous
    # jobs (user_id IS NULL) are readable by anyone who knows the job_id —
    # the id is a 16-byte secrets token, unguessable in practice.
    if row['user_id'] and row['user_id'] != current_user_id():
        return None

    payload = {
        'job_id': job_id,
        'kind': row['kind'],
        'status': row['status'],
        'progress': row['progress'],
    }
    if row['status'] == 'done':
        payload['result_filename'] = row['result_filename']
        payload['result_mimetype'] = row['result_mimetype']
        payload['result_size'] = row['result_size']
        payload['download_url'] = f'/api/jobs/{job_id}/download'
    elif row['status'] == 'failed':
        payload['error'] = row['error'] or 'Job failed.'
    return payload


def _cleanup_result_dirs(items):
    """Delete result dirs for jobs whose row was swept. Best-effort."""
    out_root = job_root_dir()
    for job_id, _filename in items:
        d = os.path.join(out_root, job_id)
        try:
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


def delete_result_dir_later(job_id: str, delay: int = 8) -> None:
    """Schedule a result dir for deletion. Same pattern as the old
    delete_file_later, but for the whole result dir.
    """
    out_root = job_root_dir()
    d = os.path.join(out_root, job_id)

    def remove():
        try:
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass

    threading.Timer(delay, remove).start()
