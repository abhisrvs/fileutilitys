# File Compressor & Converter

A web-based file compression and conversion toolkit built with Flask.

## Features

- **Image Compression** - Compress JPG, PNG, WEBP images
- **PDF Compression** - Reduce PDF file size with 3 quality levels
- **PDF Merge** - Combine multiple PDFs into one
- **Image to PDF** - Convert images to PDF
- **Image Converter** - Convert between JPG, PNG, WEBP, BMP, TIFF (accepts JPG, PNG, WEBP, BMP, GIF, TIFF, ICO)
- **Text to PDF** - Convert text to PDF
- **Word to PDF** - Convert .docx to PDF
- **PDF to Word** - Convert PDF to .docx

## Accounts

- Users can register/login (email + password, hashed with Werkzeug).
- Browser logins use signed 8-hour bearer tokens kept in tab-scoped `sessionStorage`; logout revokes the token.
- While logged in, every conversion/compression result is automatically saved to **My Files** (`/my-files`).
- Logged-out visitors are prompted to create an account after running a task; they can skip and still download.
- Storage quota: 20 files / 50 MB per user. Data lives in `users.db` (SQLite) and `user_files/`.

## Installation

```bash
pip install -r requirements.txt
python app.py
```

After changing backend dependencies, install the updated `requirements.txt` in the
same Python environment used by the deployed application and restart it.

## Deployment (cPanel)

1. Upload all files to your cPanel Python app directory
2. Set Python version to 3.12 in cPanel
3. Set application root to your project folder
4. The app will use `passenger_wsgi.py` as the entry point

## Environment Variables

| Variable | Description |
|----------|-------------|
| `ADMIN_EMAIL` | Email for visitor notifications |
| `SMTP_HOST` | SMTP server host |
| `SMTP_PORT` | SMTP server port |
| `SMTP_USER` | SMTP username |
| `SMTP_PASS` | SMTP password |
| `FROM_EMAIL` | Sender email address |
| `SECRET_KEY` | Session signing key for logins (auto-generated `.secret_key` file if unset) |
| `SITE_URL` | Public site URL for sitemap/robots.txt (default `https://util.techintricks.in`) |

### MySQL instead of SQLite

The app uses SQLite when `MYSQL_HOST` is empty. To use MySQL:

1. Create the database and application user in MySQL:

```sql
CREATE DATABASE utility_system CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'utility_user'@'%' IDENTIFIED BY 'change-this-password';
GRANT ALL PRIVILEGES ON utility_system.* TO 'utility_user'@'%';
FLUSH PRIVILEGES;
```

2. Add these values to the backend `.env` file:

```env
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=utility_user
MYSQL_PASSWORD=change-this-password
MYSQL_DATABASE=utility_system
```

3. Install dependencies and restart the backend:

```bash
pip install -r requirements.txt
python app.py
```

The application creates the `users`, `user_files`, and `jobs` tables automatically on startup. The existing SQLite `users.db` is not automatically copied to MySQL; export/import existing users separately if that data must be preserved.

## Tech Stack

- Python 3.12
- Flask 3.0.3
- Pillow (Image processing)
- PyPDF2 / pdf2docx (PDF operations)
- reportlab (PDF generation)
- Bootstrap 5.3
