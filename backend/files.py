import os
import uuid
import logging
from flask import Blueprint, session, send_from_directory, abort
from auth_context import current_user_id

from database import (USER_FILES_DIR_NAME, get_user_file, list_user_files,
                      delete_user_file, user_usage, MAX_FILES_PER_USER, MAX_TOTAL_BYTES)

files_bp = Blueprint('files', __name__)

DOC_MIMES = {
    '.pdf': 'application/pdf',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png': 'image/png',
    '.webp': 'image/webp',
    '.zip': 'application/zip',
}


def user_storage_dir(user_id):
    base = os.path.join(os.path.abspath(os.path.dirname(__file__)), USER_FILES_DIR_NAME)
    path = os.path.join(base, str(user_id))
    os.makedirs(path, exist_ok=True)
    return path


def save_output_for_user(src_path, download_name, mimetype, user_id=None):
    """Copy a generated output file into the logged-in user's storage.

    Returns True if saved, False otherwise (not logged in / quota exceeded).
    """
    from database import can_save_file, add_user_file
    if user_id is None:
        user_id = current_user_id()
    if not user_id or not src_path or not os.path.exists(src_path):
        return False

    size = os.path.getsize(src_path)
    if not can_save_file(user_id, size):
        logging.warning(f'Save skipped for user {user_id}: quota exceeded')
        return False

    safe_name = download_name.replace('/', '_').replace('\\', '_')
    stored = f'{uuid.uuid4().hex}_{safe_name}'
    dest_dir = user_storage_dir(user_id)
    dest = os.path.join(dest_dir, stored)

    try:
        with open(src_path, 'rb') as fin, open(dest, 'wb') as fout:
            while True:
                chunk = fin.read(1024 * 256)
                if not chunk:
                    break
                fout.write(chunk)
        add_user_file(user_id, safe_name, stored, mimetype, size)
        logging.info(f'Saved file to account: user={user_id} file={safe_name}')
        return True
    except Exception:
        logging.exception('Failed saving output to account')
        try:
            if os.path.exists(dest):
                os.remove(dest)
        except Exception:
            pass
        return False


@files_bp.route('/my-files')
def my_files():
    user_id = current_user_id()
    if not user_id:
        return {'status': 'error', 'error': 'Not logged in'}, 401
    rows = list_user_files(user_id)
    n_files, total_bytes = user_usage(user_id)
    items = []
    for r in rows:
        ext = os.path.splitext(r['orig_name'])[1].lower()
        items.append({
            'id': r['id'],
            'name': r['orig_name'],
            'size': r['size'],
            'created_at': r['created_at'],
            'icon': _icon_for(ext),
        })
    return {'status': 'ok', 'files': items, 'usage': {
        'n_files': n_files, 'total_mb': round(total_bytes / (1024*1024), 2),
        'max_files': MAX_FILES_PER_USER, 'max_mb': round(MAX_TOTAL_BYTES / (1024 * 1024), 2),
    }}


@files_bp.route('/my-files/download/<int:file_id>')
def download(file_id):
    user_id = current_user_id()
    if not user_id:
        abort(403)
    row = get_user_file(file_id, user_id)
    if not row:
        abort(404)
    directory = user_storage_dir(user_id)
    resp = send_from_directory(directory, row['stored_name'],
                               as_attachment=True,
                               download_name=row['orig_name'])
    mime = row['mimetype'] or DOC_MIMES.get(os.path.splitext(row['stored_name'])[1].lower())
    if mime:
        resp.mimetype = mime
    return resp


@files_bp.route('/my-files/delete/<int:file_id>', methods=['POST'])
def delete(file_id):
    user_id = current_user_id()
    if not user_id:
        abort(403)
    row = get_user_file(file_id, user_id)
    if not row:
        abort(404)
    directory = user_storage_dir(user_id)
    try:
        path = os.path.join(directory, row['stored_name'])
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        logging.exception('Failed removing stored file')
    delete_user_file(file_id, user_id)
    return {'status': 'ok'}


def _icon_for(ext):
    if ext == '.pdf':
        return '\U0001F4D5'
    if ext in ('.jpg', '.jpeg', '.png', '.webp'):
        return '\U0001F5BC'
    if ext == '.docx':
        return '\U0001F4C4'
    if ext == '.zip':
        return '\U0001F5DC'
    return '\U0001F4C1'
