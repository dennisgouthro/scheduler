# IIS Server Setup & Operations Guide

Zero `pip install`, zero external CDNs, zero extra database servers. Everything lives in **one folder**.

## 1. Copy the Folder
Copy this `Scheduler` folder to your IIS server, e.g.:
`C:\AnalyticsScheduler\`

> Keep `C:\AnalyticsScheduler\` **outside** your IIS `wwwroot` folder so nobody can download `scheduler.db`, `config.json`, or run logs directly.

## 2. Edit `config.json`
Open `C:\AnalyticsScheduler\config.json` and update:
- `"dev_mode": false` (enforces the IIS Windows Auth user header in production)
- `"python_exe"`: full path to your server's Python executable (e.g., `"C:\\Python311\\python.exe"` or your shared venv's `python.exe`; if left blank, defaults to the interpreter running `server.py`). Avoid per-user AppData Python installs that the service account cannot access.
- `"powershell_exe"` (optional): defaults to `"powershell.exe"` on Windows (set to `"C:\\Program Files\\PowerShell\\7\\pwsh.exe"` for PowerShell 7).
- `"script_runners"` (optional): map additional extensions to command templates using `{script}` (for example, `".yxmd": ["C:\\Program Files\\Alteryx\\bin\\AlteryxEngineCmd.exe", "{script}"]`). Built-in runners support `.py`, `.ps1`, `.bat`, `.cmd`, `.js`, `.mjs`, `.r`, and `.vbs`.
- `"scripts_roots"`: point to your analytics folder(s) (e.g., `"E:\\Analytics\\projects"` or UNC share `"\\\\fileserver\\analytics\\projects"`).
  > **Warning:** Do **not** use mapped drive letters (`Z:\`) — Windows service/batch accounts cannot see interactive drive mappings. Use local paths or UNC paths (`\\server\share`).
  > Keep script source files, `.sql`, and `.env` files outside IIS `wwwroot`.
- `"data_dir"`: where SQLite (`scheduler.db`) and logs (`logs/service.log` + per-run logs) are stored. In production, placing `data_dir` on a non-OS drive (e.g., `"E:\\AnalyticsSchedulerData"`) protects `C:\` from disk pressure.
- `"misfire_grace_minutes"` (default `60`): after an outage or reboot, scheduled jobs older than this window advance cleanly to their next future slot instead of triggering a catch-up stampede.
- `"max_log_mb"` (default `25`): per-run log size cap to prevent runaway scripts from filling the disk.

Each script runs with `cwd` set to the script's parent directory and receives `ANALYTICS_SCHEDULER_RUN_ID` and `ANALYTICS_SCHEDULER_JOB_NAME` in its environment.

## 3. Start It Under Your NT Service Account
1. Grant your service account **Log on as a batch job** (`SeBatchLogonRight`) in **Local Security Policy** (`secpol.msc` → *Local Policies* → *User Rights Assignment* → *Log on as a batch job*), plus read/execute NTFS permissions on `scripts_roots` and modify permissions on `data_dir`.
2. Run PowerShell as Administrator:

```powershell
cd C:\AnalyticsScheduler\deploy
.\install-service.ps1 `
    -InstallDir "C:\AnalyticsScheduler" `
    -PythonExe "C:\Python311\python.exe" `
    -ServiceAccount "YOURDOMAIN\svc_analytics"
```

- Runs least-privilege (`-RunLevel Limited`) by default; add `-HighestPrivileges` only if your scripts require elevation.
- Registers automatic restart (`-RestartCount 999`, 1-minute interval) or installs via NSSM with 10 MB log rotation if `nssm.exe` is in `PATH`.
- Automatically verifies `http://127.0.0.1:8787/healthz` after starting and surfaces an error if the service fails to come up.
- Service startup, shutdown, and runtime errors are written to `<data_dir>\logs\service.log` (rotating log).
- Note: When your AD service account password rotates, re-run `install-service.ps1` (or update the password in Task Scheduler / `services.msc`) so startup after the next reboot succeeds.

Verify locally on the server (unauthenticated health check):
```powershell
Invoke-RestMethod http://127.0.0.1:8787/healthz
```
(When `dev_mode: false`, `/api/*` returns `401` on direct localhost requests without the IIS user header; `/healthz` always returns `{"status": "ok", "queue": ...}`.)

## 4. Hook Into Your Existing IIS Intranet Site
1. Ensure these IIS components are installed:
   - **Windows Authentication** (`Web-Windows-Auth`)
   - **URL Authorization** (`Web-Url-Auth`) — required for `<authorization>` in `web.config` (otherwise IIS returns HTTP `500.19`)
   - **URL Rewrite 2.1** and **Application Request Routing (ARR 3.0)** with ARR → *Server Proxy Settings* → **Enable proxy** checked.
2. **Disable ARR response buffering for live SSE logs** (run as Administrator):
   ```powershell
   %SystemRoot%\System32\inetsrv\appcmd.exe set config -section:system.webServer/proxy /responseBufferLimit:"0" /commit:apphost
   ```
   (`deploy/web.config` already sets `<urlCompression doDynamicCompression="false" />` so IIS does not buffer `text/event-stream`, and `<httpErrors existingResponse="PassThrough" />` to preserve API JSON error bodies.)
3. Create an empty folder `scheduler` under your intranet site root (e.g., `E:\IntranetSite\scheduler\`) and convert it to an **Application** in IIS Manager.
4. Copy `deploy\web.config` into that folder and replace `YOURDOMAIN\Analytics-Team` with either:
   - an **Active Directory group** (`YOURDOMAIN\Analytics-Team`), or
   - a **local Windows group** on the IIS server (`SERVERNAME\Analytics-Team`).
5. Enable **Windows Authentication** and disable **Anonymous Authentication** on `/scheduler` in IIS Manager or via `appcmd` (do **not** place `<authentication>` in `web.config`, as that section is locked by default in `applicationHost.config` and triggers HTTP `500.19`):
   ```powershell
   %SystemRoot%\System32\inetsrv\appcmd.exe set config "Default Web Site/scheduler" -section:system.webServer/security/authentication/anonymousAuthentication /enabled:"False" /commit:apphost
   %SystemRoot%\System32\inetsrv\appcmd.exe set config "Default Web Site/scheduler" -section:system.webServer/security/authentication/windowsAuthentication /enabled:"True" /commit:apphost
   ```
6. **Forwarding the authenticated user (`X-IIS-User`) after Windows Auth:**
   > **Important:** IIS URL Rewrite inbound rules execute in the `BeginRequest` pipeline stage — *before* Windows Authentication runs and populates `{LOGON_USER}`. Setting `HTTP_X_IIS_USER = {LOGON_USER}` in `web.config` URL Rewrite rules sends an empty header and causes a `401` loop when `dev_mode: false`.
   - Instead, forward the identity in the **`PostAuthenticateRequest`** stage (which runs *after* Windows Auth and *before* ARR dispatches the proxied request in `ExecuteRequestHandler`):
     - Deploy a small managed IIS `IHttpModule` (or `App_Code` / `Global.asax` handler on `/scheduler` in Integrated Pipeline mode) that copies `Context.User.Identity.Name` into `Request.Headers["X-IIS-User"]` (overwriting any client-supplied value):
       ```csharp
       // Example in /scheduler/Global.asax (Integrated Pipeline)
       <%@ Application Language="C#" %>
       <script runat="server">
       void Application_PostAuthenticateRequest(object sender, EventArgs e) {
           var ctx = HttpContext.Current;
           ctx.Request.Headers.Remove("X-IIS-User");
           if (ctx.User != null && ctx.User.Identity != null && ctx.User.Identity.IsAuthenticated) {
               ctx.Request.Headers["X-IIS-User"] = ctx.User.Identity.Name;
           }
       }
       </script>
       ```
     - Confirm `iis_user_header` in `config.json` matches (`"X-IIS-User"`).

## 5. Open It!
Browse to `https://your-intranet/scheduler/` from any analyst's machine:
- Automatically signs in via Windows Auth (no password prompt).
- Click **+ Schedule a Script**, browse your analytics folder, pick any supported script (`.py`, `.ps1`, `.bat`, `.cmd`, `.js`, `.mjs`, `.r`, `.vbs`, or custom `script_runners` extension), and save!

## Day-2 Operations

### Safe Hot Backups (SQLite WAL Mode)
`scheduler.db` runs in **SQLite WAL mode** (`scheduler.db-wal` / `scheduler.db-shm`). Do **not** copy `scheduler.db` alone while the service is running — recent changes in `-wal` will be missed and the copy can be corrupted.

Use Python's built-in online `.backup()` API for safe hot backups without stopping the service:
```powershell
C:\Python311\python.exe -c "import sqlite3; src=sqlite3.connect(r'C:\AnalyticsScheduler\data\scheduler.db'); dst=sqlite3.connect(r'D:\Backups\scheduler-backup.db'); src.backup(dst); dst.close(); src.close()"
```
Also back up `C:\AnalyticsScheduler\config.json`.

### Clean Restart
Check that no scripts are mid-run (`queue.running -eq 0`), then restart and verify `/healthz`:
```powershell
# 1. Check active runs before restarting
(Invoke-RestMethod http://127.0.0.1:8787/healthz).queue

# 2. Restart (Scheduled Task mode)
Stop-ScheduledTask -TaskName "AnalyticsScheduler"
Start-Sleep -Seconds 2
Start-ScheduledTask -TaskName "AnalyticsScheduler"

# Or if installed with NSSM:
# Restart-Service -Name "AnalyticsScheduler"

# 3. Verify healthy
Invoke-RestMethod http://127.0.0.1:8787/healthz
```
Check `<data_dir>\logs\service.log` if the service fails to start.
