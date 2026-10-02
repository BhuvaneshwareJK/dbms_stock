# Deployment Guide

Target: a public URL anyone can open, running free of charge.

**Stack:** GitHub (code) → Aiven MySQL (database) → Streamlit Community Cloud (app).

Total time: about 45 minutes. No credit card required at any step.

---

## Why this combination

Streamlit Community Cloud runs the app but has **no database** — its filesystem is wiped on
every restart, so MySQL has to live somewhere else. Aiven's MySQL free tier is permanent
(1 GB storage, 1 GB RAM, no expiry, no card). TiDB Cloud Serverless is an equally good
alternative if you prefer more storage.

---

## STEP 1 — Create the cloud database (Aiven)

1. Sign up at **aiven.io** (GitHub or Google login works).
2. Click **Create service** → choose **MySQL**.
3. Select the **Free** plan, and pick a cloud region close to India
   (`google-asia-south1` / Mumbai, if offered).
4. Name the service, e.g. `portfolio-db`, and click **Create service**.
5. Wait until the status turns from *Rebuilding* to **Running** (3–6 minutes).

On the service **Overview** page, note these five values — you will need them twice:

| Field | Example |
|---|---|
| Host | `portfolio-db-xxxx.aivencloud.com` |
| Port | `12345` (**not** 3306) |
| User | `avnadmin` |
| Password | (click the eye icon) |
| Database | `defaultdb` |

Also click **Download CA certificate** and save `ca.pem`. Aiven refuses unencrypted
connections, so this file matters.

---

## STEP 2 — Load the schema into the cloud database

The seeded demo users and stocks have to exist before the app can start.

**Option A — MySQL Workbench / DBeaver (easiest)**

1. New connection → enter the host, port, user, password from Step 1.
2. In the SSL tab, set **Use SSL: Require**, and point the CA file at your `ca.pem`.
3. Open `schema.sql`, **delete or comment out the first two lines**
   (`CREATE DATABASE ...` and `USE portfolio_db;`) — on Aiven you are already inside
   `defaultdb` and cannot create new databases on the free plan.
4. Run the whole script.

**Option B — command line**

```bash
mysql --host=<HOST> --port=<PORT> --user=avnadmin --password \
      --ssl-mode=VERIFY_CA --ssl-ca=./ca.pem \
      defaultdb < schema.sql
```

Verify it worked:

```sql
SHOW TABLES;              -- Users, Stocks, Transactions, Holdings, v_user_positions
SELECT COUNT(*) FROM Stocks;   -- 10
```

---

## STEP 3 — Test the cloud database from your laptop first

Do **not** skip this. Debugging a connection locally takes seconds; debugging it inside
Streamlit Cloud takes minutes per attempt.

Edit your local `.env`:

```ini
DB_HOST=portfolio-db-xxxx.aivencloud.com
DB_PORT=12345
DB_NAME=defaultdb
DB_USER=avnadmin
DB_PASSWORD=<your password>
DB_SSL_CA=/absolute/path/to/ca.pem
```

Then run `streamlit run app.py`. If the sidebar badge reads **Database connected**, the
cloud database is correct and only hosting remains.

---

## STEP 4 — Push the code to GitHub

```bash
cd stock_portfolio_system
git init
git add .
git commit -m "Real-time stock portfolio management system"
git branch -M main
git remote add origin https://github.com/<your-username>/stock-portfolio-system.git
git push -u origin main
```

Two checks before you push:

- `.gitignore` already excludes `.env.local`, `.venv/` and `secrets.toml`. **Make sure the
  `.env` you commit still holds placeholders, not your real Aiven password.** Anything
  pushed to GitHub is permanent even after deletion.
- The repository must be **public** for the Community Cloud free tier (private repos need
  extra permissions and count against a quota).

---

## STEP 5 — Deploy on Streamlit Community Cloud

1. Go to **share.streamlit.io** and sign in with the same GitHub account.
2. Click **Create app** → **"Yup, I have an app."**
3. Fill in:
   - Repository: `<your-username>/stock-portfolio-system`
   - Branch: `main`
   - Main file path: `app.py`
   - App URL: choose a subdomain, e.g. `portfolio-intelligence`
4. Open **Advanced settings** → set Python version **3.12**.
5. In the same dialog, paste this into the **Secrets** box (TOML format):

```toml
DB_HOST = "portfolio-db-xxxx.aivencloud.com"
DB_PORT = "12345"
DB_NAME = "defaultdb"
DB_USER = "avnadmin"
DB_PASSWORD = "your-real-password"
DB_POOL_SIZE = "2"
DB_MAX_OVERFLOW = "3"
PRICE_CACHE_TTL = "120"
CURRENCY_SYMBOL = "₹"

DB_SSL_CA_CONTENT = """
-----BEGIN CERTIFICATE-----
...paste the entire contents of ca.pem here...
-----END CERTIFICATE-----
"""
```

6. Click **Deploy**. First build takes 2–5 minutes.

### Why this works without code changes

Streamlit exposes everything in the Secrets box as **environment variables**, which is
exactly what `db/database.py` reads via `os.getenv`. `DB_SSL_CA_CONTENT` is handled by
`_ssl_connect_args()`: it writes the certificate to a temp file at startup and passes that
path to the MySQL driver, because Community Cloud gives you no way to upload a file.

Note the reduced pool size — the free Aiven plan allows few concurrent connections, and
`DB_POOL_SIZE = 2` keeps the app well inside that ceiling.

---

## STEP 6 — Verify the live app

Open `https://<your-subdomain>.streamlit.app` and check, in order:

1. Sidebar badge says **Database connected** (if not, see troubleshooting below).
2. The account dropdown lists the seeded users.
3. Place a small BUY order — cash should drop and the holding should appear.
4. Reload the page; the trade must still be there (proves the write hit the cloud DB).

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Connected, but missing tables` | `schema.sql` never ran, or ran against the wrong database. Re-run Step 2 against `defaultdb`. |
| `Database unreachable: OperationalError` | Wrong host/port, or the Aiven service is still rebuilding. Remember the port is not 3306. |
| `SSL connection error` / `certificate verify failed` | `DB_SSL_CA_CONTENT` is malformed. Paste the `ca.pem` exactly, including both `-----BEGIN/END-----` lines, inside triple quotes. As a temporary test, replace it with `DB_SSL = "1"` — that encrypts without verifying. |
| `ModuleNotFoundError` in the build log | `requirements.txt` is not in the repository root, or the file path in the deploy form is wrong. |
| Live prices blank, dashboard shows "valued at cost" | Yahoo Finance rate-limited the shared Streamlit IP. Raise `PRICE_CACHE_TTL` to 300. This is expected on free hosting and the app degrades gracefully by design. |
| App is slow on first open after hours idle | Community Cloud sleeps inactive apps. The first request wakes it, taking ~30 seconds. |

---

## Alternative deployment paths

**Render / Railway (if you want the database and app on one platform)**

Add a `Dockerfile`:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8501
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
```

Then create a web service from the repo and add a managed MySQL instance. Render's free
web tier sleeps after 15 minutes of inactivity; its MySQL is not free (use Aiven alongside).

**College lab / offline demo**

No deployment at all — run locally and expose the port only if needed:

```bash
streamlit run app.py --server.address=0.0.0.0
```

Then open `http://<your-laptop-ip>:8501` from any machine on the same Wi-Fi. For a demo in
front of an examiner this is the most reliable option, because it does not depend on the
venue's internet.

---

## Security notice before you make the repo public

This application has **no authentication** — the sidebar account switcher trusts whoever
opens the page. Anyone with the URL can trade on any account. That is acceptable for an
academic demo, but do not put real money data behind it. If the app must be public, either
add Streamlit's `st.login` / OIDC authentication, or set the app to private in Community
Cloud settings and share it only with specific email addresses.
