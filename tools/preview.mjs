/**
 * Local UI Preview Server (Node 24+, zero npm packages).
 *
 * Mirrors the exact REST + SSE contract of `server.py` so you can preview,
 * test, and click through the live Analytics Scheduler UI on a laptop that
 * does not have Python installed yet.
 *
 * Production IIS server runs `python server.py`.
 */

import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const BASE_DIR = path.resolve(__dirname, "..");
const STATIC_DIR = path.join(BASE_DIR, "static");
const CONFIG = JSON.parse(fs.readFileSync(path.join(BASE_DIR, "config.json"), "utf8"));

const ROOTS = (CONFIG.scripts_roots || []).map((r) => ({
  id: r.id,
  label: r.label,
  path: path.resolve(BASE_DIR, r.path),
}));

const EXCLUDED_DIRS = new Set([".git", ".venv", "venv", "__pycache__", "node_modules"]);
const DAY_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const DAY_FULL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
};

function formatTime12(hhmm = "07:00") {
  const [hStr, mStr] = hhmm.split(":");
  const h = Number(hStr) || 0;
  const m = Number(mStr) || 0;
  const suffix = h < 12 ? "AM" : "PM";
  const h12 = h % 12 || 12;
  return `${h12}:${String(m).padStart(2, "0")} ${suffix}`;
}

function describeSchedule(type, cfg = {}) {
  if (type === "manual") return "Manual only";
  if (type === "daily") return `Daily at ${formatTime12(cfg.time)}`;
  if (type === "weekdays") return `Weekdays at ${formatTime12(cfg.time)}`;
  if (type === "weekly") {
    const days = Array.isArray(cfg.days) && cfg.days.length ? cfg.days : [0];
    if (days.length === 1) return `Every ${DAY_FULL[days[0]]} at ${formatTime12(cfg.time)}`;
    return `${days.map((d) => DAY_SHORT[d]).join(", ")} at ${formatTime12(cfg.time)}`;
  }
  if (type === "monthly") {
    if (cfg.day_mode === "first_business_day") return `1st business day at ${formatTime12(cfg.time)}`;
    if (cfg.day_mode === "last_business_day") return `Last business day at ${formatTime12(cfg.time)}`;
    return `Monthly on day ${cfg.day || 1} at ${formatTime12(cfg.time)}`;
  }
  if (type === "interval") {
    const every = cfg.every_hours || 2;
    const scope = cfg.weekdays_only === false ? "daily" : "weekdays";
    return `Every ${every === 1 ? "hour" : `${every} hrs`}, ${formatTime12(`${cfg.start_hour ?? 7}:00`)}–${formatTime12(`${cfg.end_hour ?? 18}:00`)} (${scope})`;
  }
  return "Manual only";
}

function computeWeeklySlots(type, cfg = {}, enabled = true) {
  if (!enabled || type === "manual") return [];
  const [h, m] = (cfg.time || "07:00").split(":").map((n) => Number(n) || 0);
  if (type === "daily") return [0, 1, 2, 3, 4, 5, 6].map((day) => ({ day, hour: h, minute: m }));
  if (type === "weekdays") return [0, 1, 2, 3, 4].map((day) => ({ day, hour: h, minute: m }));
  if (type === "weekly") return (cfg.days || [0]).map((day) => ({ day, hour: h, minute: m }));
  if (type === "monthly") return [{ day: 0, hour: h, minute: m, monthly: true }];
  if (type === "interval") {
    const slots = [];
    const days = cfg.weekdays_only === false ? [0, 1, 2, 3, 4, 5, 6] : [0, 1, 2, 3, 4];
    const every = Math.max(1, Number(cfg.every_hours) || 2);
    const sh = Number(cfg.start_hour ?? 7);
    const eh = Number(cfg.end_hour ?? 18);
    for (const day of days) {
      for (let hr = sh; hr <= eh; hr += every) slots.push({ day, hour: hr, minute: 0 });
    }
    return slots;
  }
  return [];
}

function nextRunPreview(type, cfg = {}, enabled = true) {
  if (!enabled || type === "manual") return null;
  const now = new Date();
  const [h, m] = (cfg.time || "07:00").split(":").map((n) => Number(n) || 0);
  const d = new Date(now);
  d.setSeconds(0, 0);
  d.setHours(type === "interval" ? Number(cfg.start_hour ?? 7) : h, m, 0, 0);
  if (d <= now) d.setDate(d.getDate() + 1);
  if (type === "weekdays") {
    while (d.getDay() === 0 || d.getDay() === 6) d.setDate(d.getDate() + 1);
  }
  return d.toISOString();
}

function scrubText(text) {
  return String(text || "")
    .replace(/\b(password|token|secret|api_key)\s*([=:])\s*([^\s'"]+)/gi, "$1$2[REDACTED]")
    .replace(/\b\d{3}-\d{2}-\d{4}\b/g, "[SSN REDACTED]");
}

function resolveSafe(rootId, relPath = "") {
  const root = ROOTS.find((r) => r.id === rootId) || ROOTS[0];
  const cleaned = String(relPath || "").replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
  if (cleaned.split("/").includes("..")) throw new Error("Path traversal is not allowed.");
  const target = cleaned ? path.resolve(root.path, cleaned) : root.path;
  if (!target.startsWith(root.path)) throw new Error("Path escapes the allowed analytics folder.");
  return { root, target, rel: path.relative(root.path, target).replace(/\\/g, "/") };
}

function parseReadme(filePath) {
  try {
    const text = fs.readFileSync(filePath, "utf8").slice(0, 8192);
    let title = "";
    const lines = [];
    for (const raw of text.split(/\r?\n/)) {
      const line = raw.trim();
      if (!line) {
        if (lines.length) break;
        continue;
      }
      if (line.startsWith("#")) {
        if (!title) title = line.replace(/^#+\s*/, "").trim();
        else if (lines.length) break;
        continue;
      }
      if (line.startsWith("|") || line.startsWith("```") || line.startsWith("-")) continue;
      lines.push(line);
    }
    return { title, summary: lines.join(" ").replace(/[`*_]/g, "").slice(0, 220) };
  } catch {
    return { title: "", summary: "" };
  }
}

// Seed realistic preview data
const nowMs = Date.now();
const agoIso = (mins) => new Date(nowMs - mins * 60000).toISOString();

const jobs = new Map();
const runs = new Map(); // runId -> run
const runLogs = new Map(); // runId -> string
const sseClients = new Map(); // runId -> Set<res>
const activeTimers = new Map(); // runId -> timer

function seedDemoData() {
  const j1 = {
    id: "job_exec_sales",
    name: "Executive Sales Dashboard",
    description: "Refreshes morning executive sales KPIs and regional rollup JSON from Snowflake for the leadership dashboard.",
    script_ref: "projects:executive_sales/python/refresh_sales.py",
    args: "",
    python_exe: "",
    dashboard_url: "/dashboards/executive-sales/",
    freshness_path: "projects:executive_sales/reports/freshness.json",
    schedule_type: "weekdays",
    schedule_config: { time: "07:00" },
    timeout_minutes: 45,
    enabled: true,
    owner: "Analyst",
    created_by: "Analyst",
    created_at: agoIso(14400),
    updated_by: "Analyst",
    updated_at: agoIso(420),
  };

  const j2 = {
    id: "job_revenue_fcst",
    name: "Revenue Forecast Pipeline",
    description: "Builds the rolling 4-month regional revenue forecast dataset and JSON payload for the intranet dashboard.",
    script_ref: "projects:revenue_forecast/python/build_forecast.py",
    args: "",
    python_exe: "",
    dashboard_url: "/dashboards/revenue-forecast/",
    freshness_path: "",
    schedule_type: "weekdays",
    schedule_config: { time: "07:15" },
    timeout_minutes: 45,
    enabled: true,
    owner: "Sarah",
    created_by: "Sarah",
    created_at: agoIso(12000),
    updated_by: "Sarah",
    updated_at: agoIso(180),
  };

  const j3 = {
    id: "job_schema_refresh",
    name: "Nightly Schema Refresh",
    description: "Refreshes warehouse table metadata exports and runs nightly data quality total checks.",
    script_ref: "projects:schema_refresh/python/refresh_metadata.py",
    args: "",
    python_exe: "",
    dashboard_url: "",
    freshness_path: "",
    schedule_type: "daily",
    schedule_config: { time: "05:30" },
    timeout_minutes: 30,
    enabled: true,
    owner: "Analyst",
    created_by: "Analyst",
    created_at: agoIso(20000),
    updated_by: "Analyst",
    updated_at: agoIso(1440),
  };

  const j4 = {
    id: "job_plan_rollup",
    name: "Monthly Plan Sponsor Rollup",
    description: "Generates month-end plan sponsor metrics and Excel/JSON exports on the first business day.",
    script_ref: "projects:executive_sales/python/refresh_sales.py",
    args: "--mode monthly",
    python_exe: "",
    dashboard_url: "/dashboards/plan-sponsor/",
    freshness_path: "",
    schedule_type: "monthly",
    schedule_config: { time: "07:00", day_mode: "first_business_day" },
    timeout_minutes: 60,
    enabled: true,
    owner: "Maya",
    created_by: "Maya",
    created_at: agoIso(30000),
    updated_by: "Maya",
    updated_at: agoIso(2880),
  };

  [j1, j2, j3, j4].forEach((j) => jobs.set(j.id, j));

  // Add realistic run history
  for (let i = 10; i >= 1; i--) {
    const rid = `run_sales_${i}`;
    runs.set(rid, {
      id: rid,
      job_id: j1.id,
      status: "succeeded",
      trigger: "scheduled",
      triggered_by: "Schedule",
      queued_at: agoIso(i * 1440 - 120),
      started_at: agoIso(i * 1440 - 120),
      finished_at: agoIso(i * 1440 - 115),
      duration_sec: 245 + (i % 4) * 18,
      exit_code: 0,
      error_summary: "",
    });
    runLogs.set(
      rid,
      `=== Executive Sales Dashboard ===\nStarted : ${agoIso(i * 1440 - 120)}\nTrigger : scheduled (Schedule)\nScript  : ${j1.script_ref}\n================================================\nConnecting to Snowflake using configured keypair (.env)...\nRunning query: PROD.ANALYTICS.V_EXEC_SALES_DAILY (last 90 days)...\nRetrieved 42,180 rows across 6 regions.\nWrote dashboard payload -> reports/sales_summary.json\nDone.\n\n=== Finished: SUCCEEDED in ${245 + (i % 4) * 18}.0s (exit code 0) ===\n`
    );
  }

  // Revenue forecast: mostly green, latest failed
  for (let i = 6; i >= 2; i--) {
    const rid = `run_fcst_${i}`;
    runs.set(rid, {
      id: rid,
      job_id: j2.id,
      status: "succeeded",
      trigger: "scheduled",
      triggered_by: "Schedule",
      queued_at: agoIso(i * 1440),
      started_at: agoIso(i * 1440),
      finished_at: agoIso(i * 1440 - 6),
      duration_sec: 312,
      exit_code: 0,
      error_summary: "",
    });
    runLogs.set(rid, `=== Revenue Forecast Pipeline ===\nRetrieved 18,410 forecast records.\nWrote reports/revenue_forecast.json.\n=== Finished: SUCCEEDED in 312.0s (exit code 0) ===\n`);
  }
  runs.set("run_fcst_latest", {
    id: "run_fcst_latest",
    job_id: j2.id,
    status: "failed",
    trigger: "scheduled",
    triggered_by: "Schedule",
    queued_at: agoIso(95),
    started_at: agoIso(95),
    finished_at: agoIso(94),
    duration_sec: 44.2,
    exit_code: 1,
    error_summary: "SnowflakeProgrammingError: 000904 (42000): invalid identifier 'FORECAST_BUCKET_DT' in DEMO.ANALYTICS.REVENUE_FORECAST",
  });
  runLogs.set(
    "run_fcst_latest",
    scrubText(
      `=== Revenue Forecast Pipeline ===\nStarted : ${agoIso(95)}\nTrigger : scheduled (Schedule)\nScript  : ${j2.script_ref}\n================================================\nLoading Snowflake connection profile...\nQuerying DEMO.ANALYTICS.REVENUE_FORECAST for next 4 months...\nDebug hint: token=demo_secret_12345 (automatically redacted in UI logs)\nTraceback (most recent call last):\n  File "revenue_forecast/python/build_forecast.py", line 16, in main\n    raise RuntimeError("SnowflakeProgrammingError: 000904 (42000): invalid identifier 'FORECAST_BUCKET_DT' in DEMO.ANALYTICS.REVENUE_FORECAST")\nRuntimeError: SnowflakeProgrammingError: 000904 (42000): invalid identifier 'FORECAST_BUCKET_DT' in DEMO.ANALYTICS.REVENUE_FORECAST\n\n=== Finished: FAILED in 44.2s (exit code 1) ===\n`
    )
  );

  // Nightly schema refresh
  for (let i = 8; i >= 1; i--) {
    const rid = `run_schema_${i}`;
    runs.set(rid, {
      id: rid,
      job_id: j3.id,
      status: "succeeded",
      trigger: "scheduled",
      triggered_by: "Schedule",
      queued_at: agoIso(i * 1440 - 200),
      started_at: agoIso(i * 1440 - 200),
      finished_at: agoIso(i * 1440 - 197),
      duration_sec: 162,
      exit_code: 0,
      error_summary: "",
    });
    runLogs.set(
      rid,
      `=== Nightly Schema Refresh ===\nExported 84 table metadata summaries.\nRunning 6 data quality total checks...\nAll 6 checks passed within tolerance.\n=== Finished: SUCCEEDED in 162.0s (exit code 0) ===\n`
    );
  }
}

seedDemoData();

function runsForJob(jobId) {
  return Array.from(runs.values())
    .filter((r) => r.job_id === jobId)
    .sort((a, b) => (a.queued_at < b.queued_at ? 1 : -1));
}

function serializeJob(j) {
  const jRuns = runsForJob(j.id).slice(0, 20);
  const active = jRuns.find((r) => r.status === "running" || r.status === "queued") || null;
  return {
    ...j,
    schedule_label: describeSchedule(j.schedule_type, j.schedule_config),
    weekly_slots: computeWeeklySlots(j.schedule_type, j.schedule_config, j.enabled),
    next_run_at: nextRunPreview(j.schedule_type, j.schedule_config, j.enabled),
    last_run: jRuns[0] || null,
    active_run: active,
    recent_runs: [...jRuns].reverse(),
  };
}

function queueStats() {
  let running = 0;
  let queued = 0;
  for (const r of runs.values()) {
    if (r.status === "running") running++;
    if (r.status === "queued") queued++;
  }
  return { running, queued, max_concurrent: CONFIG.max_concurrent_runs || 4 };
}

function broadcast(runId, event) {
  const clients = sseClients.get(runId);
  if (!clients) return;
  const payload = `data: ${JSON.stringify(event)}\n\n`;
  for (const res of clients) {
    try {
      res.write(payload);
    } catch {
      clients.delete(res);
    }
  }
}

function simulateRun(job, run) {
  const isErrorScript = job.script_ref.includes("build_forecast");
  const lines = isErrorScript
    ? [
        `=== ${job.name} (Preview Simulation) ===\n`,
        `Started : ${run.started_at}\n`,
        `Trigger : manual (${run.triggered_by})\n`,
        `Script  : ${job.script_ref}\n`,
        `================================================\n`,
        `Loading Snowflake connection profile...\n`,
        `Querying DEMO.ANALYTICS.REVENUE_FORECAST for next 4 months...\n`,
        scrubText(`Debug hint: token=demo_secret_12345 (automatically redacted in UI logs)\n`),
        `Traceback (most recent call last):\n`,
        `  File "${job.script_ref}", line 16, in main\n`,
        `RuntimeError: SnowflakeProgrammingError: 000904 (42000): invalid identifier 'FORECAST_BUCKET_DT'\n`,
      ]
    : [
        `=== ${job.name} (Preview Simulation) ===\n`,
        `Started : ${run.started_at}\n`,
        `Trigger : manual (${run.triggered_by})\n`,
        `Script  : ${job.script_ref}\n`,
        `================================================\n`,
        `Connecting to Snowflake using configured keypair (.env)...\n`,
        `Executing SQL extract and transforming dataset...\n`,
        `Processed 42,180 rows cleanly.\n`,
        `Wrote refreshed JSON payload for intranet dashboard.\n`,
        `Done.\n`,
      ];

  let idx = 0;
  const start = Date.now();
  const timer = setInterval(() => {
    if (idx < lines.length) {
      const chunk = lines[idx++];
      runLogs.set(run.id, (runLogs.get(run.id) || "") + chunk);
      broadcast(run.id, { type: "line", text: chunk });
    } else {
      clearInterval(timer);
      activeTimers.delete(run.id);
      const dur = Number(((Date.now() - start) / 1000).toFixed(1));
      run.status = isErrorScript ? "failed" : "succeeded";
      run.exit_code = isErrorScript ? 1 : 0;
      run.duration_sec = dur;
      run.finished_at = new Date().toISOString();
      run.error_summary = isErrorScript
        ? "SnowflakeProgrammingError: 000904 (42000): invalid identifier 'FORECAST_BUCKET_DT'"
        : "";
      const footer = `\n=== Finished: ${run.status.toUpperCase()} in ${dur}s (exit code ${run.exit_code}) ===\n`;
      runLogs.set(run.id, (runLogs.get(run.id) || "") + footer);
      broadcast(run.id, { type: "line", text: footer });
      broadcast(run.id, { type: "done", status: run.status, duration_sec: dur });
    }
  }, 450);
  activeTimers.set(run.id, timer);
}

function sendJson(res, status, data) {
  const body = Buffer.from(JSON.stringify(data), "utf8");
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
    "Content-Length": body.length,
  });
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let raw = "";
    req.on("data", (c) => (raw += c));
    req.on("end", () => {
      try {
        resolve(raw ? JSON.parse(raw) : {});
      } catch (err) {
        reject(err);
      }
    });
  });
}

const server = http.createServer(async (req, res) => {
  const parsed = new URL(req.url, `http://${req.headers.host}`);
  const pathname = decodeURIComponent(parsed.pathname);
  const user = CONFIG.dev_user || "Analyst";

  if (!pathname.startsWith("/api/")) {
    const rel = pathname === "/" ? "index.html" : pathname.replace(/^\/+/, "");
    const target = path.resolve(STATIC_DIR, rel);
    if (!target.startsWith(STATIC_DIR) || !fs.existsSync(target) || !fs.statSync(target).isFile()) {
      sendJson(res, 404, { error: "Not found" });
      return;
    }
    const ext = path.extname(target).toLowerCase();
    const content = fs.readFileSync(target);
    res.writeHead(200, { "Content-Type": MIME[ext] || "application/octet-stream", "Content-Length": content.length });
    res.end(content);
    return;
  }

  try {
    if (req.method === "GET" && pathname === "/api/me") {
      return sendJson(res, 200, {
        user,
        app_name: CONFIG.app_name || "Analytics Scheduler",
        dev_mode: true,
        default_timeout_minutes: CONFIG.default_timeout_minutes || 45,
        roots: ROOTS.map((r) => ({ id: r.id, label: r.label })),
        queue: queueStats(),
      });
    }

    if (req.method === "GET" && pathname === "/api/browse") {
      const rootId = parsed.searchParams.get("root") || ROOTS[0].id;
      const relPath = parsed.searchParams.get("path") || "";
      const { root, target, rel } = resolveSafe(rootId, relPath);
      const entries = fs.readdirSync(target, { withFileTypes: true }).sort((a, b) => {
        if (a.isDirectory() !== b.isDirectory()) return a.isDirectory() ? -1 : 1;
        return a.name.localeCompare(b.name);
      });
      const folders = [];
      const scripts = [];
      for (const e of entries) {
        if (e.name.startsWith(".") || EXCLUDED_DIRS.has(e.name)) continue;
        const childRel = rel ? `${rel}/${e.name}` : e.name;
        if (e.isDirectory()) {
          const full = path.join(target, e.name);
          folders.push({
            name: e.name,
            path: childRel,
            is_project: fs.existsSync(path.join(full, "README.md")) || fs.existsSync(path.join(full, "python")),
          });
        } else if (e.isFile() && e.name.toLowerCase().endsWith(".py")) {
          scripts.push({ name: e.name, path: childRel, script_ref: `${root.id}:${childRel}` });
        }
      }
      const breadcrumbs = [{ label: root.label, path: "" }];
      if (rel) {
        const parts = rel.split("/");
        parts.forEach((p, idx) => breadcrumbs.push({ label: p, path: parts.slice(0, idx + 1).join("/") }));
      }
      return sendJson(res, 200, { root_id: root.id, root_label: root.label, path: rel, breadcrumbs, folders, scripts });
    }

    if (req.method === "GET" && pathname === "/api/inspect") {
      const scriptRef = parsed.searchParams.get("script_ref") || "";
      const [rootId, relPath] = scriptRef.split(":", 2);
      const { root, target, rel } = resolveSafe(rootId, relPath);
      const dir = path.dirname(target);
      const projDir = ["python", "scripts"].includes(path.basename(dir).toLowerCase()) ? path.dirname(dir) : dir;
      let readme = { title: "", summary: "" };
      let found = false;
      for (const cand of [projDir, dir]) {
        const rp = path.join(cand, "README.md");
        if (fs.existsSync(rp)) {
          readme = parseReadme(rp);
          found = true;
          break;
        }
      }
      const fallbackName = path
        .basename(target, ".py")
        .replace(/[_\-]+/g, " ")
        .replace(/\b\w/g, (c) => c.toUpperCase());
      return sendJson(res, 200, {
        script_ref: `${root.id}:${rel}`,
        script_name: path.basename(target),
        suggested_name: readme.title || fallbackName,
        suggested_description: readme.summary,
        readme_found: found,
        suggested_freshness_path: "",
      });
    }

    if (req.method === "GET" && pathname === "/api/jobs") {
      const list = Array.from(jobs.values())
        .sort((a, b) => a.name.localeCompare(b.name))
        .map(serializeJob);
      return sendJson(res, 200, { jobs: list, queue: queueStats() });
    }

    if (req.method === "GET" && pathname === "/api/runs") {
      const limit = Math.max(1, Math.min(300, Number(parsed.searchParams.get("limit") || 100)));
      const list = Array.from(runs.values())
        .filter((r) => jobs.has(r.job_id))
        .sort((a, b) => (a.queued_at < b.queued_at ? 1 : -1))
        .slice(0, limit)
        .map((r) => {
          const j = jobs.get(r.job_id);
          return {
            ...r,
            job_name: j.name,
            job_owner: j.owner,
            script_ref: j.script_ref,
            dashboard_url: j.dashboard_url,
          };
        });
      return sendJson(res, 200, { runs: list, queue: queueStats() });
    }

    if (req.method === "POST" && pathname === "/api/jobs") {
      const body = await readBody(req);
      if (!body.name || !body.script_ref) return sendJson(res, 400, { error: "Name and script are required." });
      const id = `job_${crypto.randomBytes(5).toString("hex")}`;
      const now = new Date().toISOString();
      const job = {
        id,
        name: String(body.name).trim(),
        description: String(body.description || "").trim(),
        script_ref: String(body.script_ref).trim(),
        args: String(body.args || "").trim(),
        python_exe: "",
        dashboard_url: String(body.dashboard_url || "").trim(),
        freshness_path: String(body.freshness_path || "").trim(),
        schedule_type: body.schedule_type || "weekdays",
        schedule_config: body.schedule_config || { time: "07:00" },
        timeout_minutes: Number(body.timeout_minutes) || 45,
        enabled: body.enabled !== false,
        owner: String(body.owner || user).trim(),
        created_by: user,
        created_at: now,
        updated_by: user,
        updated_at: now,
      };
      jobs.set(id, job);
      return sendJson(res, 201, serializeJob(job));
    }

    const parts = pathname.split("/").filter(Boolean);
    if (parts[1] === "jobs" && parts.length === 3) {
      const job = jobs.get(parts[2]);
      if (!job) return sendJson(res, 404, { error: "Schedule not found." });
      if (req.method === "GET") return sendJson(res, 200, serializeJob(job));
      if (req.method === "PUT") {
        const body = await readBody(req);
        Object.assign(job, {
          name: body.name ?? job.name,
          description: body.description ?? job.description,
          script_ref: body.script_ref ?? job.script_ref,
          args: body.args ?? job.args,
          dashboard_url: body.dashboard_url ?? job.dashboard_url,
          freshness_path: body.freshness_path ?? job.freshness_path,
          schedule_type: body.schedule_type ?? job.schedule_type,
          schedule_config: body.schedule_config ?? job.schedule_config,
          timeout_minutes: Number(body.timeout_minutes ?? job.timeout_minutes),
          enabled: body.enabled ?? job.enabled,
          owner: body.owner ?? job.owner,
          updated_by: user,
          updated_at: new Date().toISOString(),
        });
        return sendJson(res, 200, serializeJob(job));
      }
      if (req.method === "DELETE") {
        jobs.delete(parts[2]);
        return sendJson(res, 200, { deleted: true });
      }
    }

    if (parts[1] === "jobs" && parts.length === 4 && parts[3] === "runs" && req.method === "GET") {
      return sendJson(res, 200, { runs: runsForJob(parts[2]) });
    }

    if (parts[1] === "jobs" && parts.length === 4 && parts[3] === "run" && req.method === "POST") {
      const job = jobs.get(parts[2]);
      if (!job) return sendJson(res, 404, { error: "Schedule not found." });
      const active = runsForJob(job.id).find((r) => r.status === "running" || r.status === "queued");
      if (active) return sendJson(res, 409, { error: "This script is already running." });
      const rid = `run_${Date.now()}_${crypto.randomBytes(3).toString("hex")}`;
      const now = new Date().toISOString();
      const run = {
        id: rid,
        job_id: job.id,
        status: "running",
        trigger: "manual",
        triggered_by: user,
        queued_at: now,
        started_at: now,
        finished_at: null,
        duration_sec: null,
        exit_code: null,
        error_summary: "",
      };
      runs.set(rid, run);
      runLogs.set(rid, "");
      simulateRun(job, run);
      return sendJson(res, 202, { run });
    }

    if (parts[1] === "runs" && parts.length === 3 && req.method === "GET") {
      const run = runs.get(parts[2]);
      if (!run) return sendJson(res, 404, { error: "Run not found." });
      const job = jobs.get(run.job_id) || null;
      const log = runLogs.get(run.id) || "";
      const ai_prompt = job
        ? `A scheduled analytics Python script failed on the server. Please help diagnose and fix it.\n\n- **Pipeline:** ${job.name}\n- **Script:** \`${job.script_ref}\`\n- **Status:** ${run.status} (exit code: ${run.exit_code})\n- **Error summary:** ${run.error_summary}\n\n### Recent Log Output (secrets scrubbed)\n\`\`\`text\n${log}\n\`\`\`\n`
        : "";
      return sendJson(res, 200, { run, job: job ? serializeJob(job) : null, log, ai_prompt });
    }

    if (parts[1] === "runs" && parts.length === 4 && parts[3] === "stop" && req.method === "POST") {
      const run = runs.get(parts[2]);
      if (!run) return sendJson(res, 404, { error: "Run not found." });
      const t = activeTimers.get(run.id);
      if (t) {
        clearInterval(t);
        activeTimers.delete(run.id);
      }
      run.status = "stopped";
      run.finished_at = new Date().toISOString();
      run.error_summary = `Stopped by ${user}.`;
      broadcast(run.id, { type: "done", status: "stopped" });
      return sendJson(res, 200, { stopped: true });
    }

    if (parts[1] === "runs" && parts.length === 4 && parts[3] === "stream" && req.method === "GET") {
      const run = runs.get(parts[2]);
      if (!run) return sendJson(res, 404, { error: "Run not found." });
      res.writeHead(200, {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache",
        Connection: "keep-alive",
      });
      res.write(`data: ${JSON.stringify({ type: "snapshot", status: run.status, log: runLogs.get(run.id) || "" })}\n\n`);
      if (run.status !== "running" && run.status !== "queued") {
        res.write(`data: ${JSON.stringify({ type: "done", status: run.status })}\n\n`);
        res.end();
        return;
      }
      if (!sseClients.has(run.id)) sseClients.set(run.id, new Set());
      sseClients.get(run.id).add(res);
      req.on("close", () => sseClients.get(run.id)?.delete(res));
      return;
    }

    sendJson(res, 404, { error: "Not found" });
  } catch (err) {
    sendJson(res, 400, { error: err.message });
  }
});

const PORT = CONFIG.port || 8787;
const HOST = CONFIG.host || "127.0.0.1";
server.listen(PORT, HOST, () => {
  console.log(`Analytics Scheduler [LOCAL UI PREVIEW] listening on http://${HOST}:${PORT}/`);
  console.log(`Production IIS server runs: python server.py`);
});
