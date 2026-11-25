# 1-Page IIS Server Setup Guide

Zero `pip install`, zero external CDNs, zero extra database servers. Everything lives in **one folder**.

## 1. Copy the Folder
Copy this `Scheduler` folder to your IIS server, e.g.:
`C:\AnalyticsScheduler\`

> Keep `C:\AnalyticsScheduler\` **outside** your IIS `wwwroot` folder so nobody can download `scheduler.db` or logs directly.

## 2. Edit `config.json`
Open `C:\AnalyticsScheduler\config.json` and update:
- `"dev_mode": false` (enforces IIS Windows Auth header)
- `"python_exe"`: full path to your server's analytics Python (e.g., `"C:\\Python311\\python.exe"` or your shared venv's `python.exe`)
- `"scripts_roots"`: point to your analytics share folder(s) (e.g., `"E:\\Analytics\\projects"`).
  > Tip: Keep `.py`, `.sql`, and `.env` files in a folder outside IIS `wwwroot` so IIS never serves source code or secrets over HTTP.

## 3. Start It Under Your NT Service Account
Run PowerShell as Administrator:

```powershell
cd C:\AnalyticsScheduler\deploy
.\install-service.ps1 `
    -InstallDir "C:\AnalyticsScheduler" `
    -PythonExe "C:\Python311\python.exe" `
    -ServiceAccount "YOURDOMAIN\svc_analytics"
```

Verify locally on the server: open `http://127.0.0.1:8787/` (or check `C:\AnalyticsScheduler\data\scheduler.db` was created).

## 4. Hook Into Your Existing IIS Intranet Site
1. Confirm **URL Rewrite** and **Application Request Routing (ARR)** are installed in IIS Manager (with ARR → *Server Proxy Settings* → **Enable proxy** checked).
2. In IIS Manager → Server level → **URL Rewrite** → **View Server Variables** → click **Add…** → enter `HTTP_X_IIS_USER`.
3. Create an empty folder `scheduler` under your intranet site root (e.g., `E:\IntranetSite\scheduler\`) and convert it to an **Application** in IIS Manager (or simply place `web.config` inside it).
4. Copy `deploy\web.config` into that folder and replace `YOURDOMAIN\Analytics-Team` with your Analytics AD Group.
5. Make sure **Windows Authentication** is enabled and **Anonymous Authentication** is disabled on `/scheduler`.

## 5. Open It!
Browse to `https://your-intranet/scheduler/` from any analyst's machine:
- Automatically signs in via Windows Auth (no password prompt).
- Click **+ Schedule a Script**, browse your share folder, pick a `.py` file, and save!

## Backups
Back up `C:\AnalyticsScheduler\data\scheduler.db` (and `config.json`). That's it.
