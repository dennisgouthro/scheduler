# Analytics Scheduler

A lightweight, futuristic, zero-dependency Python scheduler with a clean intranet front-end—built for analytics teams running Snowflake & Python pipelines on a Windows/IIS server.

## Why It Stays Light
- **Zero `pip install` needed:** Runs on pure Python 3.9+ standard library (`http.server`, `sqlite3`, `subprocess`, `threading`).
- **Zero external CDNs or internet calls:** All HTML/CSS/JS is bundled locally. Only your Python scripts connect to Snowflake using your existing `.env` and proxy.
- **One folder, one SQLite file:** Back up or move the entire app by copying one folder.
- **Built for analytics teams & AI agents:** Plain-English schedules (no cron syntax), `Scripts` / `Schedules` / `Results` mission-control views, automatic project `README.md` title/description detection, secret-scrubbed live logs, and 1-click **AI Fix** error copying.

## Quick Start (Python)
```powershell
python server.py
```
Then open `http://127.0.0.1:8787/` in your browser.

## Local UI Preview (No Python Installed Yet)
If you want to preview and click through the UI on a machine that only has Node.js 20+:
```powershell
node tools/preview.mjs
```
Then open `http://127.0.0.1:8787/`.

## Running Tests (Python)
```powershell
python -m unittest discover -s tests -v
```

## Deploying to IIS
See [deploy/IIS-SETUP.md](deploy/IIS-SETUP.md) for the 1-page setup guide, ready-to-use [deploy/web.config](deploy/web.config), and [deploy/install-service.ps1](deploy/install-service.ps1).
