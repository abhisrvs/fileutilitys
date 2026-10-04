# cPanel 2: Frontend (React)

## Steps to deploy React build to separate cPanel

### 1. Build React app
```bash
cd frontend
# Set your backend API domain in .env
echo "VITE_API_BASE=https://YOUR-BACKEND-DOMAIN.COM" > .env
npm run build
```

### 2. Upload to cPanel 2
Upload the contents of `frontend/dist/` to your frontend cPanel's `public_html/` folder:
```
public_html/
├── index.html
└── assets/
    ├── index-xxxxx.js
    └── index-xxxxx.css
```

### 3. Create .htaccess for SPA routing
Create `.htaccess` in `public_html/`:
```apache
RewriteEngine On
RewriteCond %{REQUEST_FILENAME} !-f
RewriteCond %{REQUEST_FILENAME} !-d
RewriteRule ^ index.html [L]
```

### 4. Environment Variables (frontend/.env)
```
VITE_API_BASE=https://YOUR-BACKEND-DOMAIN.COM
```

### 5. Backend CORS Config (backend/.env)
Add the exact frontend origin to CORS_ORIGINS (without a path):
```
CORS_ORIGINS=https://techintricks.online
```

Restart the backend after changing this value. The backend accepts a trailing slash in the setting, but the frontend origin must still be the HTTPS site origin, not a page URL.

### 6. Install dependencies in the cPanel app environment
Run these commands from the backend application directory using the Python environment configured for the cPanel app:
```bash
python -m pip install -r requirements.txt
python -c "import pymysql; print(pymysql.__version__)"
```

The second command must succeed when `MYSQL_HOST` is configured. Restart the cPanel/Passenger application after installing dependencies and changing `.env`; otherwise the old process may continue returning 500 responses without CORS headers.

Browser authentication uses signed bearer tokens. Deploy `backend/auth_context.py`
alongside the other backend modules, install the updated `requirements.txt`
(including PyJWT), and rebuild/redeploy the frontend so it stores the returned
token for the current tab and sends it in the `Authorization: Bearer` header.
