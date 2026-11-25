# Analytics Scheduler

A lightweight, futuristic, zero-dependency Python scheduler with a clean intranet front-end—built for analytics teams running Python, PowerShell, Batch, R, Node.js, VBScript, and custom CLI pipelines on a Windows/IIS server.

## Why It Stays Light
- **Zero `pip install` needed:** Runs on pure Python 3.9+ standard library (`http.server`, `sqlite3`, `subprocess`, `threading`).
- **Zero external CDNs or internet calls:** All HTML/CSS/JS is bundled locally. Only your scheduled scripts reach external systems (Snowflake, APIs, databases) using your existing `.env` and proxy.
- **Multi-script ready:** Built-in runners for `.py`, `.ps1`, `.bat`, `.cmd`, `.js`, `.mjs`, `.r`, and `.vbs`, plus customizable `powershell_exe` and `script_runners` in `config.json` (e.g., Alteryx `.yxmd` or custom CLI tools).
- **One folder, one SQLite file:** Back up safely online via SQLite `.backup()` (WAL-safe) and `config.json`.
- **Built for analytics teams & AI agents:** Plain-English schedules (no cron syntax), `Scripts` / `Schedules` / `Results` mission-control views, automatic project `README.md` title/description detection, optional `freshness.json` badges, secret-scrubbed live SSE logs, and 1-click **AI Fix** error copying.

## Supported Script Types & Configuration
Out of the box, the scheduler discovers and runs:
- **Python:** `.py` (`python_exe`, defaults to the running `sys.executable` when blank)
- **PowerShell:** `.ps1` (`powershell_exe`, defaults to `powershell.exe` on Windows / `pwsh` on POSIX)
- **Batch / Shell:** `.bat`, `.cmd`
- **Node.js:** `.js`, `.mjs`
- **R:** `.r`
- **VBScript:** `.vbs`
- **Custom extensions:** Add `"script_runners": { ".ext": ["C:\\path\\to\\tool.exe", "{script}"] }` in `config.json`.

Key operational settings in `config.json`:
- `misfire_grace_minutes` (default `60`): skips stale missed runs after outages/reboots to prevent catch-up stampedes.
- `max_log_mb` (default `25`): caps per-run log files to protect disk space.
- `data_dir` (default `./data`): stores `scheduler.db`, run logs, and rotating service logs at `data/logs/service.log`.
- Health check endpoint: `GET /healthz` (unauthenticated, returns `{"status": "ok", "queue": ...}`).

## Quick Start (Python)
```powershell
python server.py
```
Then open `http://127.0.0.1:8787/` in your browser (health check at `http://127.0.0.1:8787/healthz`).

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
See [deploy/IIS-SETUP.md](deploy/IIS-SETUP.md) for the complete IIS setup & Day-2 operations guide (including Windows Auth `PostAuthenticateRequest` header forwarding, local/AD group authorization, ARR `responseBufferLimit="0"` for live SSE logs, `SeBatchLogonRight`, WAL-safe hot backups, and clean restarts), along with [deploy/web.config](deploy/web.config) and [deploy/install-service.ps1](deploy/install-service.ps1).
