(() => {
  const DEFAULT_EXTENSIONS = [".py", ".ps1", ".bat", ".cmd", ".js", ".mjs", ".r", ".vbs"];

  const state = {
    me: null,
    jobs: [],
    allRuns: [],
    queue: { running: 0, queued: 0, max_concurrent: 4 },
    view: "scripts", // "scripts" | "schedules" | "results"
    filter: "all",
    search: "",
    mineOnly: false,
    showAllHours: false,

    // Results view filters
    resultsFilter: "all",
    resultsSearch: "",

    // Polling & resilience
    refreshing: false,
    pollFailures: 0,
    pollTimer: null,

    // Job drawer state
    editingJobId: null,
    deleteArmed: false,
    cadenceType: "weekdays",
    weeklyDays: new Set([0, 2, 4]),
    intervalMinute: 0,
    browserRootId: null,
    browserPath: "",
    browserReqId: 0,
    inspectReqId: 0,
    browserEntries: { folders: [], scripts: [] },
    browserFilter: "",
    lastAutoFilled: { name: "", description: "", freshness: "" },

    // Logs drawer state
    logsJobId: null,
    logsRuns: [],
    selectedRunId: null,
    selectedAiPrompt: "",
    logsReqId: 0,
    runReqId: 0,
    eventSource: null,
    sseRunId: null,
    sseReconnectAttempts: 0,
    sseFallbackPollTimer: null,
    logLineCount: 0,
    logNewlines: 0,
    logTotalChars: 0,
    logEndsWithNewline: false,
    logHasPlaceholder: true,
    programmaticLogScroll: false,
    lastLogScrollTop: 0,

    // Accessibility focus restoration
    lastFocusedEl: null,
    lastCopyFocusEl: null,
  };

  const DAY_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const $ = (id) => document.getElementById(id);

  function parseIntOr(val, fallback) {
    if (val === "" || val === null || val === undefined) return fallback;
    const n = Number(val);
    return Number.isNaN(n) ? fallback : n;
  }

  function friendlyHttpError(status) {
    if (status === 401) return "Authentication required (401). Please reload the page to sign in.";
    if (status === 403) return "Access denied (403). You may not have permission for this action.";
    if (status === 404) return "Requested endpoint or resource was not found (404).";
    if (status === 409) return "Action conflicts with current state (409).";
    if (status === 502) return "Scheduler service is temporarily unreachable (502).";
    if (status === 503) return "Scheduler service is restarting or unavailable (503).";
    if (status === 504) return "Timed out waiting for the scheduler service (504).";
    return `Server returned an unexpected response (${status}).`;
  }

  async function api(path, options = {}) {
    const res = await fetch(path.replace(/^\//, ""), {
      ...options,
      headers: {
        "Content-Type": "application/json",
        "X-Scheduler-Request": "1",
        ...(options.headers || {}),
      },
    });
    const contentType = (res.headers.get("content-type") || "").toLowerCase();
    const text = await res.text();
    const looksHtml =
      contentType.includes("text/html") || /^\s*(<!doctype\s+html|<html\b|<head\b|<body\b)/i.test(text);

    let data = {};
    if (!looksHtml && text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = {};
      }
    }

    if (!res.ok) {
      if (data && typeof data.error === "string" && data.error.trim()) {
        throw new Error(data.error.trim());
      }
      throw new Error(friendlyHttpError(res.status));
    }

    if (looksHtml) {
      throw new Error("Received an HTML page instead of API data. Please reload to re-authenticate.");
    }

    return data;
  }

  let toastTimer = null;
  function showToast(msg) {
    const el = $("toast");
    el.textContent = msg;
    el.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), 3000);
  }

  function escapeHtml(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function isValidDashboardUrl(url) {
    const u = String(url || "").trim();
    if (!u) return true;
    if (/[\r\n\t\x00-\x1f\x7f]/.test(u)) return false;
    if (u.startsWith("/")) {
      // Reject protocol-relative //evil.com and /\evil.com
      return !u.startsWith("//") && !u.startsWith("/\\");
    }
    try {
      const parsed = new URL(u);
      return (parsed.protocol === "http:" || parsed.protocol === "https:") && Boolean(parsed.hostname);
    } catch {
      return false;
    }
  }

  function supportedExtensionsList() {
    const exts = state.me?.supported_extensions;
    if (Array.isArray(exts) && exts.length) return exts;
    return DEFAULT_EXTENSIONS;
  }

  function supportedExtSummary() {
    return supportedExtensionsList().join(", ");
  }

  function updateDynamicCopy() {
    const extsText = supportedExtSummary();
    if (!state.editingJobId) {
      $("job-drawer-sub").textContent = `Pick a script (${extsText}) and set its schedule.`;
    }
    if (state.jobs.length === 0) {
      $("empty-body").textContent = `Pick any script (${extsText}) from your analytics share and configure when it runs in seconds.`;
    }
    const tz = state.me?.timezone;
    $("schedule-tz-hint").textContent = tz ? `(Server time: ${tz})` : "";
  }

  function formatDuration(sec) {
    if (sec === null || sec === undefined) return "—";
    const s = Math.round(Number(sec));
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    const r = s % 60;
    return r ? `${m}m ${r}s` : `${m}m`;
  }

  function formatRelative(iso) {
    if (!iso) return "";
    const dt = new Date(iso);
    if (Number.isNaN(dt.getTime())) return iso;
    const diffMs = Date.now() - dt.getTime();
    const absMin = Math.round(Math.abs(diffMs) / 60000);
    if (diffMs >= 0) {
      if (absMin < 1) return "just now";
      if (absMin < 60) return `${absMin}m ago`;
      const hrs = Math.round(absMin / 60);
      if (hrs < 24) return `${hrs}h ago`;
      const days = Math.round(hrs / 24);
      if (days < 7) return `${days}d ago`;
      return dt.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    } else {
      const today = new Date();
      const tomorrow = new Date(today);
      tomorrow.setDate(today.getDate() + 1);
      const timeStr = dt.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
      if (dt.toDateString() === today.toDateString()) return `Today ${timeStr}`;
      if (dt.toDateString() === tomorrow.toDateString()) return `Tomorrow ${timeStr}`;
      return `${dt.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })} ${timeStr}`;
    }
  }

  function formatNextRun(iso) {
    if (!iso) return "";
    const dt = new Date(iso);
    if (Number.isNaN(dt.getTime())) return iso;
    const aheadMs = dt.getTime() - Date.now();
    if (aheadMs <= 60000) return "Due now";
    return formatRelative(iso);
  }

  function formatTimestamp(iso) {
    if (!iso) return "—";
    const dt = new Date(iso);
    if (Number.isNaN(dt.getTime())) return iso;
    return dt.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  }

  function formatHour12(h) {
    const suffix = h < 12 ? "AM" : "PM";
    const h12 = h % 12 || 12;
    return `${h12}:00 ${suffix}`;
  }

  function formatTime12(hhmm) {
    const [hStr, mStr] = (hhmm || "07:00").split(":");
    const h = parseIntOr(hStr, 0);
    const m = parseIntOr(mStr, 0);
    const suffix = h < 12 ? "AM" : "PM";
    const h12 = h % 12 || 12;
    return `${h12}:${String(m).padStart(2, "0")} ${suffix}`;
  }

  function scriptIcon(name, ext) {
    const str = String(name || "");
    const e = (ext || (str.includes(".") ? "." + str.split(".").pop() : "")).toLowerCase();
    if (e === ".py") return "🐍";
    if (e === ".ps1") return "⚡";
    if (e === ".bat" || e === ".cmd") return "⌨️";
    if (e === ".js" || e === ".mjs") return "🟨";
    if (e === ".r") return "📊";
    if (e === ".vbs") return "📜";
    return "📄";
  }

  function jobHealthState(job) {
    if (job.active_run) return "running";
    if (!job.enabled) return "paused";
    if (!job.last_run) return "never";
    if (job.last_run.status === "succeeded") return "healthy";
    if (job.last_run.status === "failed" || job.last_run.status === "timed_out") return "failed";
    if (job.last_run.status === "stopped") return "stopped";
    return "never";
  }

  function hasSelectionInside(el) {
    const sel = window.getSelection?.();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return false;
    const node = sel.anchorNode;
    return Boolean(node && el && el.contains(node));
  }

  function isAnyOverlayOpen() {
    return (
      !$("job-drawer").classList.contains("hidden") ||
      !$("logs-drawer").classList.contains("hidden") ||
      !$("copy-modal-backdrop").classList.contains("hidden")
    );
  }

  function showConnBanner(message) {
    $("conn-banner-text").textContent = message;
    $("conn-banner").classList.remove("hidden");
  }

  function hideConnBanner() {
    $("conn-banner").classList.add("hidden");
  }

  function updateGreeting() {
    const hr = new Date().getHours();
    const salutation = hr < 12 ? "Morning Deck" : hr < 17 ? "Afternoon Deck" : "Evening Deck";
    $("greeting-chip").textContent = salutation;
  }

  function renderQueuePill() {
    const q = state.queue || { running: 0, queued: 0, max_concurrent: 4 };
    const pill = $("queue-pill");
    const txt = $("queue-text");
    const busy = q.running > 0 || q.queued > 0;
    pill.classList.toggle("is-busy", busy);
    if (busy) {
      txt.textContent = q.queued > 0
        ? `${q.running}/${q.max_concurrent} active · ${q.queued} queued`
        : `${q.running}/${q.max_concurrent} active`;
    } else {
      txt.textContent = `Ready · 0/${q.max_concurrent}`;
    }
  }

  function renderNavCounts() {
    $("nav-count-scripts").textContent = state.jobs.length;
    const scheduledCount = state.jobs.filter((j) => j.enabled && j.schedule_type !== "manual").length;
    $("nav-count-schedules").textContent = scheduledCount;

    const failedRuns = (state.allRuns || []).filter(
      (r) => r.status === "failed" || r.status === "timed_out"
    ).length;
    const badge = $("nav-badge-failed");
    badge.textContent = failedRuns;
    badge.title = `${failedRuns} failed run${failedRuns === 1 ? "" : "s"} in recent history`;
    badge.classList.toggle("hidden", failedRuns === 0);
  }

  function renderCounts() {
    const counts = { all: state.jobs.length, healthy: 0, failed: 0, running: 0, paused: 0 };
    for (const job of state.jobs) {
      const st = jobHealthState(job);
      if (st === "healthy") counts.healthy++;
      else if (st === "failed") counts.failed++;
      else if (st === "running") counts.running++;
      else if (st === "paused") counts.paused++;
    }
    $("count-all").textContent = counts.all;
    $("count-healthy").textContent = counts.healthy;
    $("count-failed").textContent = counts.failed;
    $("count-running").textContent = counts.running;
    $("count-paused").textContent = counts.paused;
  }

  function getFilteredJobs() {
    const q = state.search.trim().toLowerCase();
    const myName = (state.me?.user || "").toLowerCase();
    return state.jobs.filter((job) => {
      if (state.mineOnly && myName) {
        const ownerMatch = (job.owner || "").toLowerCase() === myName;
        const createdMatch = (job.created_by || "").toLowerCase() === myName;
        if (!ownerMatch && !createdMatch) return false;
      }
      const st = jobHealthState(job);
      if (state.filter !== "all" && st !== state.filter) return false;
      if (q) {
        const hay = `${job.name} ${job.description} ${job.owner} ${job.script_ref} ${job.schedule_label}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }

  function renderStatusBadge(job) {
    const st = jobHealthState(job);
    if (st === "running") {
      const label = job.active_run.status === "queued" ? "Queued" : "Running";
      return `<span class="status-pill status-running"><span class="pulse-dot"></span>${label}</span>`;
    }
    if (st === "healthy") {
      const ago = formatRelative(job.last_run.finished_at || job.last_run.started_at);
      return `<span class="status-pill status-healthy">Fresh · ${escapeHtml(ago)}</span>`;
    }
    if (st === "failed") {
      const label = job.last_run.status === "timed_out" ? "Timed out" : "Failed";
      return `<span class="status-pill status-failed">${label}</span>`;
    }
    if (st === "stopped") {
      return `<span class="status-pill status-paused">Stopped</span>`;
    }
    if (st === "paused") {
      return `<span class="status-pill status-paused">Paused</span>`;
    }
    return `<span class="status-pill status-never">Ready</span>`;
  }

  function renderRunStatusPill(status) {
    if (status === "running" || status === "queued") {
      return `<span class="status-pill status-running"><span class="pulse-dot"></span>${status === "queued" ? "Queued" : "Running"}</span>`;
    }
    if (status === "succeeded") return `<span class="status-pill status-healthy">Succeeded</span>`;
    if (status === "failed" || status === "timed_out") {
      return `<span class="status-pill status-failed">${status === "timed_out" ? "Timed out" : "Failed"}</span>`;
    }
    return `<span class="status-pill status-paused">Stopped</span>`;
  }

  // ==================== 1. SCRIPTS VIEW ====================
  function renderScriptsView({ skipListIfSelected = false } = {}) {
    renderCounts();
    renderNavCounts();
    renderQueuePill();

    const list = $("jobs-list");
    if (skipListIfSelected && hasSelectionInside(list)) return;

    const panel = $("deck-panel");
    const empty = $("empty-state");
    const filtered = getFilteredJobs();

    if (!filtered.length) {
      list.innerHTML = "";
      panel.classList.add("hidden");
      empty.classList.remove("hidden");
      if (state.jobs.length === 0) {
        $("empty-title").textContent = "No scripts registered";
        $("empty-body").textContent = `Pick any script (${supportedExtSummary()}) from your analytics share and configure when it runs in seconds.`;
        $("btn-empty-new").classList.remove("hidden");
      } else {
        $("empty-title").textContent = "No matching scripts";
        $("empty-body").textContent = "Try clearing your filter or search query.";
        $("btn-empty-new").classList.add("hidden");
      }
      return;
    }

    panel.classList.remove("hidden");
    empty.classList.add("hidden");

    list.innerHTML = filtered
      .map((job) => {
        const st = jobHealthState(job);
        const nextLabel = job.next_run_at ? formatNextRun(job.next_run_at) : "";
        const nextTxt = job.next_run_at
          ? nextLabel === "Due now"
            ? "Due now"
            : `Next: ${nextLabel}`
          : job.enabled
            ? "Manual trigger"
            : "Paused";
        const durTxt = job.last_run?.duration_sec != null ? formatDuration(job.last_run.duration_sec) : "";
        const runs = job.recent_runs || [];
        const maxDur = Math.max(1, ...runs.map((r) => Number(r.duration_sec) || 1));

        const barsHtml = runs.length
          ? runs
              .map((r) => {
                const dur = Number(r.duration_sec) || 1;
                const h = Math.max(7, Math.min(20, Math.round(7 + (dur / maxDur) * 13)));
                const tip = `${r.status} · ${formatTimestamp(r.started_at || r.queued_at)}${r.duration_sec != null ? " · " + formatDuration(r.duration_sec) : ""}`;
                return `<button type="button" class="mini-bar bar-${escapeHtml(r.status)}" style="height:${h}px" data-open-run="${escapeHtml(r.id)}" data-job-id="${escapeHtml(job.id)}" title="${escapeHtml(tip)}" aria-label="${escapeHtml(tip)}"></button>`;
              })
              .join("")
          : `<span class="muted">—</span>`;

        const safeDashUrl = job.dashboard_url && isValidDashboardUrl(job.dashboard_url) ? job.dashboard_url.trim() : "";
        const dashLink = safeDashUrl
          ? `<a class="dash-link" href="${escapeHtml(safeDashUrl)}" target="_blank" rel="noopener noreferrer" title="Open live dashboard">↗</a>`
          : "";

        const actionPrimary = job.active_run
          ? `<button type="button" class="btn btn-danger btn-xs" data-stop-run="${escapeHtml(job.active_run.id)}">■ Stop</button>`
          : `<button type="button" class="btn btn-run btn-xs" data-run-job="${escapeHtml(job.id)}">▶ Run</button>`;

        const rowClasses = [
          "job-row",
          !job.enabled ? "is-paused" : "",
          st === "failed" ? "is-failed" : "",
          st === "running" ? "is-running" : "",
        ]
          .filter(Boolean)
          .join(" ");

        const errFallback =
          job.last_run?.status === "timed_out"
            ? "Script timed out"
            : `Exited with code ${job.last_run?.exit_code ?? "unknown"}`;
        const errText = job.last_run?.error_summary || errFallback;
        const errorBar =
          st === "failed" && job.last_run
            ? `<div class="row-error-bar">
                 <button type="button" class="row-error-text" data-open-logs="${escapeHtml(job.id)}" data-run-id="${escapeHtml(job.last_run.id)}" title="Click to inspect full log: ${escapeHtml(errText)}">${escapeHtml(errText)}</button>
                 <div class="row-error-actions">
                   <button type="button" class="btn btn-ai btn-xs" data-quick-ai="${escapeHtml(job.last_run.id)}">✦ AI Fix</button>
                 </div>
               </div>`
            : "";

        const icon = scriptIcon(job.script_ref);
        const descLine = job.description
          ? `<div class="job-desc" title="${escapeHtml(job.description)}">${escapeHtml(job.description)}</div>`
          : "";
        const orbClass = st === "stopped" ? "paused" : st;

        return `
          <div class="${rowClasses}">
            <div class="job-row-main">
              <div class="col-pipeline">
                <span class="orb orb-${orbClass}"></span>
                <div class="pipeline-text">
                  <div class="job-title-line">
                    <button type="button" class="job-title-btn" data-open-logs="${escapeHtml(job.id)}" title="${escapeHtml(job.description || job.name)}">${escapeHtml(job.name)}</button>
                    ${dashLink}
                  </div>
                  <div class="job-subline" title="${escapeHtml(job.script_ref)}"><span class="script-type-icon" aria-hidden="true">${icon}</span>${escapeHtml(job.script_ref)}</div>
                  ${descLine}
                </div>
              </div>

              <div class="col-cadence">
                <span class="cadence-main">${escapeHtml(job.schedule_label)}</span>
                <span class="cadence-next">${escapeHtml(nextTxt)}</span>
              </div>

              <div class="col-owner">${escapeHtml(job.owner || "Team")}</div>

              <div class="col-telemetry">
                <div class="mini-bars">${barsHtml}</div>
              </div>

              <div class="col-status">
                ${renderStatusBadge(job)}
                ${durTxt ? `<span class="status-dur">${escapeHtml(durTxt)}</span>` : ""}
              </div>

              <div class="col-actions">
                ${actionPrimary}
                <button type="button" class="btn btn-ghost btn-xs" data-open-logs="${escapeHtml(job.id)}">Logs</button>
                <button type="button" class="btn btn-ghost btn-xs" data-edit-job="${escapeHtml(job.id)}">Edit</button>
              </div>
            </div>
            ${errorBar}
          </div>
        `;
      })
      .join("");
  }

  // ==================== 2. SCHEDULES VIEW (Upcoming + Weekly Matrix + Roster) ====================
  function renderSchedulesView() {
    // 1. Upcoming strip
    const upcoming = state.jobs
      .filter((j) => j.enabled && j.next_run_at)
      .sort((a, b) => new Date(a.next_run_at) - new Date(b.next_run_at))
      .slice(0, 6);

    const upBox = $("upcoming-items");
    if (!upcoming.length) {
      upBox.innerHTML = `<span class="muted">No automatic schedules active.</span>`;
    } else {
      upBox.innerHTML = upcoming
        .map(
          (j) =>
            `<button type="button" class="upcoming-chip" data-edit-job="${escapeHtml(j.id)}">
               <span class="upcoming-time">${escapeHtml(formatNextRun(j.next_run_at))}</span>
               <span>${escapeHtml(j.name)}</span>
             </button>`
        )
        .join("");
    }

    // 2. Weekly matrix
    const grid = $("timeline-grid");
    const cellMap = new Map();
    const activeHours = new Set();

    for (const job of state.jobs) {
      if (!job.enabled || !job.weekly_slots?.length) continue;
      for (const slot of job.weekly_slots) {
        const key = `${slot.hour}:${slot.day}`;
        if (!cellMap.has(key)) cellMap.set(key, []);
        cellMap.get(key).push({ job, minute: slot.minute || 0, monthly: !!slot.monthly });
        activeHours.add(slot.hour);
      }
    }

    const hours =
      state.showAllHours || activeHours.size === 0
        ? Array.from({ length: 24 }, (_, i) => i)
        : Array.from(activeHours).sort((a, b) => a - b);

    const todayIdx = (new Date().getDay() + 6) % 7; // Mon=0..Sun=6
    const peakThreshold = Math.max(2, Number(state.queue?.max_concurrent) || 4);

    const headCells = DAY_SHORT.map(
      (d, idx) => `<th class="${idx === todayIdx ? "tl-today-col" : ""}">${d}</th>`
    ).join("");

    const bodyRows = hours
      .map((hour) => {
        let maxInCell = 0;
        const cells = DAY_SHORT.map((_, day) => {
          const items = (cellMap.get(`${hour}:${day}`) || []).sort((a, b) => a.minute - b.minute);
          if (items.length > maxInCell) maxInCell = items.length;
          const chips = items
            .map(({ job, minute, monthly }) => {
              const mm = `:${String(minute).padStart(2, "0")}`;
              const mTag = monthly ? " · mo" : "";
              return `<button type="button" class="tl-chip" data-edit-job="${escapeHtml(job.id)}" title="${escapeHtml(job.name)} — ${escapeHtml(job.schedule_label)}${monthly ? " (next monthly run)" : ""}"><span class="tl-chip-time">${mm}</span>${escapeHtml(job.name)}${mTag}</button>`;
            })
            .join("");
          return `<td>${chips}</td>`;
        }).join("");

        const busy = maxInCell >= peakThreshold;
        return `
          <tr class="${busy ? "tl-row-busy" : ""}">
            <td class="tl-hour-cell">
              <div>${formatHour12(hour)}</div>
              ${busy ? `<span class="tl-busy-tag">Peak · ${maxInCell}</span>` : ""}
            </td>
            ${cells}
          </tr>
        `;
      })
      .join("");

    grid.innerHTML = `
      <table class="tl-table">
        <thead>
          <tr><th>Hour</th>${headCells}</tr>
        </thead>
        <tbody>${bodyRows}</tbody>
      </table>
    `;

    // 3. Compact schedule roster (quick pause/resume & reschedule)
    const roster = $("schedule-roster-list");
    if (!state.jobs.length) {
      roster.innerHTML = `<div class="roster-row muted">No scripts configured yet.</div>`;
    } else {
      roster.innerHTML = state.jobs
        .map((j) => {
          const isManual = j.schedule_type === "manual";
          const nextTxt = j.next_run_at ? formatNextRun(j.next_run_at) : "—";
          const stateBadge = isManual
            ? `<span class="status-pill status-never">Manual</span>`
            : j.enabled
              ? `<span class="status-pill status-healthy">Active</span>`
              : `<span class="status-pill status-paused">Paused</span>`;
          const toggleBtn = isManual
            ? ""
            : `<button type="button" class="btn btn-subtle btn-xs" data-toggle-enabled="${escapeHtml(j.id)}">${j.enabled ? "Pause" : "Resume"}</button>`;
          return `
            <div class="roster-row ${!j.enabled && !isManual ? "is-paused" : ""}">
              <div class="pipeline-text">
                <span class="cadence-main" style="font-weight:600">${escapeHtml(j.name)}</span>
                <span class="job-subline">${escapeHtml(j.owner || "Team")}</span>
              </div>
              <span class="cadence-main">${escapeHtml(j.schedule_label)}</span>
              <span class="result-mono">${escapeHtml(nextTxt)}</span>
              <div>${stateBadge}</div>
              <div class="col-actions">
                ${toggleBtn}
                <button type="button" class="btn btn-ghost btn-xs" data-edit-job="${escapeHtml(j.id)}">Reschedule</button>
              </div>
            </div>
          `;
        })
        .join("");
    }
  }

  // ==================== 3. RESULTS VIEW (Unified Team Run Feed) ====================
  function renderResultsView({ skipListIfSelected = false } = {}) {
    const all = state.allRuns || [];
    let ok = 0, err = 0, active = 0;
    for (const r of all) {
      if (r.status === "succeeded") ok++;
      else if (r.status === "failed" || r.status === "timed_out") err++;
      else if (r.status === "running" || r.status === "queued") active++;
    }
    $("res-count-all").textContent = all.length;
    $("res-count-ok").textContent = ok;
    $("res-count-err").textContent = err;
    $("res-count-active").textContent = active;

    const finished = ok + err;
    const rate = finished > 0 ? Math.round((ok / finished) * 100) : 0;
    const rateEl = $("res-pass-rate");
    rateEl.classList.remove("is-warn", "is-err", "is-neutral");
    if (finished === 0) {
      rateEl.textContent = "No completed runs";
      rateEl.classList.add("is-neutral");
    } else {
      rateEl.textContent = `${rate}% pass rate (${finished} runs)`;
      if (rate < 70) rateEl.classList.add("is-err");
      else if (rate < 90) rateEl.classList.add("is-warn");
    }

    const list = $("results-list");
    if (skipListIfSelected && hasSelectionInside(list)) return;

    const q = state.resultsSearch.trim().toLowerCase();
    const filtered = all.filter((r) => {
      if (state.resultsFilter === "succeeded" && r.status !== "succeeded") return false;
      if (state.resultsFilter === "failed" && r.status !== "failed" && r.status !== "timed_out") return false;
      if (state.resultsFilter === "active" && r.status !== "running" && r.status !== "queued") return false;
      if (q) {
        const hay = `${r.job_name} ${r.triggered_by} ${r.error_summary} ${r.script_ref}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });

    const panel = $("results-panel");
    const empty = $("results-empty");

    if (!filtered.length) {
      list.innerHTML = "";
      panel.classList.add("hidden");
      empty.classList.remove("hidden");
      if (all.length === 0) {
        $("results-empty-title").textContent = "No execution results yet";
        $("results-empty-body").textContent =
          "Run any script from the Scripts tab to see live telemetry, durations, and logs here.";
      } else {
        $("results-empty-title").textContent = "No matching runs";
        $("results-empty-body").textContent = "Try clearing your status filter or search query.";
      }
      return;
    }

    panel.classList.remove("hidden");
    empty.classList.add("hidden");

    list.innerHTML = filtered
      .map((r) => {
        const isFailed = r.status === "failed" || r.status === "timed_out";
        const isActive = r.status === "running" || r.status === "queued";
        const trig = r.trigger === "scheduled" ? "Scheduled" : `Manual · ${r.triggered_by || "User"}`;
        const outcomeText = isFailed
          ? r.error_summary || (r.status === "timed_out" ? "Script timed out" : `Exited with code ${r.exit_code ?? "unknown"}`)
          : isActive
            ? "Streaming console output..."
            : r.status === "stopped"
              ? r.error_summary || "Stopped"
              : "Completed cleanly (exit 0)";
        const icon = scriptIcon(r.script_ref || "");

        return `
          <div class="result-row ${isFailed ? "is-failed" : ""} ${isActive ? "is-running" : ""}" tabindex="0" role="button" data-open-run="${escapeHtml(r.id)}" data-job-id="${escapeHtml(r.job_id)}">
            <div>${renderRunStatusPill(r.status)}</div>
            <div class="pipeline-text">
              <span class="cadence-main" style="font-weight:600">${escapeHtml(r.job_name)}</span>
              <span class="job-subline" title="${escapeHtml(r.script_ref || "")}"><span class="script-type-icon" aria-hidden="true">${icon}</span>${escapeHtml(r.script_ref || "")}</span>
            </div>
            <span class="res-trigger">${escapeHtml(trig)}</span>
            <span class="result-mono" title="${escapeHtml(formatTimestamp(r.started_at || r.queued_at))}">${escapeHtml(formatRelative(r.started_at || r.queued_at))}</span>
            <span class="result-mono">${escapeHtml(formatDuration(r.duration_sec))}</span>
            <span class="result-outcome ${isFailed ? "is-err" : ""}" title="${escapeHtml(outcomeText)}">${escapeHtml(outcomeText)}</span>
            <div class="col-actions">
              ${isFailed ? `<button type="button" class="btn btn-ai btn-xs" data-quick-ai="${escapeHtml(r.id)}">✦ AI Fix</button>` : ""}
              ${isActive ? `<button type="button" class="btn btn-danger btn-xs" data-stop-run="${escapeHtml(r.id)}">■ Stop</button>` : `<button type="button" class="btn btn-ghost btn-xs" data-open-run="${escapeHtml(r.id)}" data-job-id="${escapeHtml(r.job_id)}">Logs</button>`}
            </div>
          </div>
        `;
      })
      .join("");
  }

  async function refreshAll({ background = false } = {}) {
    if (state.refreshing) return;
    state.refreshing = true;
    try {
      const [jobsData, runsData] = await Promise.all([api("api/jobs"), api("api/runs?limit=120")]);
      state.jobs = jobsData.jobs || [];
      state.queue = jobsData.queue || state.queue;
      state.allRuns = runsData.runs || [];
      state.pollFailures = 0;
      hideConnBanner();

      renderNavCounts();
      renderQueuePill();
      renderCounts();

      if (state.view === "scripts") {
        renderScriptsView({ skipListIfSelected: background });
      } else if (state.view === "schedules") {
        renderSchedulesView();
      } else if (state.view === "results") {
        renderResultsView({ skipListIfSelected: background });
      }

      if (state.logsJobId && !$("logs-drawer").classList.contains("hidden")) {
        updateLogsActionButtons(state.logsJobId);
      }
    } catch (err) {
      state.pollFailures++;
      showConnBanner(err.message || "Unable to reach the scheduler service.");
      if (!background || state.pollFailures === 1) {
        showToast(err.message);
      }
    } finally {
      state.refreshing = false;
    }
  }

  function scheduleNextPoll() {
    clearTimeout(state.pollTimer);
    const delay =
      state.pollFailures > 0
        ? Math.min(60000, 12000 * Math.pow(2, Math.min(state.pollFailures, 3)))
        : 12000;
    state.pollTimer = setTimeout(async () => {
      if (!document.hidden) {
        await refreshAll({ background: true });
      }
      scheduleNextPoll();
    }, delay);
  }

  // ==================== SCHEDULE BUILDER ====================
  function populateHourSelects() {
    const startSel = $("field-start-hour");
    const endSel = $("field-end-hour");
    const opts = Array.from({ length: 24 }, (_, h) => `<option value="${h}">${formatHour12(h)}</option>`).join("");
    startSel.innerHTML = opts;
    endSel.innerHTML = opts;
    startSel.value = "7";
    endSel.value = "18";
  }

  function updateCadenceUI() {
    const type = state.cadenceType;
    document.querySelectorAll(".cadence-pill").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.type === type);
    });

    const hideTime = type === "manual" || type === "interval";
    const monthlyMode = $("field-monthly-mode").value;
    const showMonthlyDay = type === "monthly" && monthlyMode === "day_of_month";

    $("row-time").classList.toggle("hidden", hideTime);
    $("row-weekly-days").classList.toggle("hidden", type !== "weekly");
    $("row-monthly").classList.toggle("hidden", type !== "monthly");
    $("row-interval").classList.toggle("hidden", type !== "interval");
    $("field-monthly-day").classList.toggle("hidden", !showMonthlyDay);

    // Disable hidden cadence inputs so native validation never blocks Save
    $("field-time").disabled = hideTime;
    $("field-monthly-mode").disabled = type !== "monthly";
    $("field-monthly-day").disabled = !showMonthlyDay;
    $("field-every-hours").disabled = type !== "interval";
    $("field-start-hour").disabled = type !== "interval";
    $("field-end-hour").disabled = type !== "interval";
    $("field-interval-weekdays").disabled = type !== "interval";

    document.querySelectorAll(".day-chip").forEach((chip) => {
      chip.classList.toggle("active", state.weeklyDays.has(Number(chip.dataset.day)));
    });

    const previewEl = $("schedule-preview");
    previewEl.classList.remove("is-invalid");

    const timeStr = formatTime12($("field-time").value || "07:00");
    let preview = "Runs only when triggered manually";
    if (type === "weekdays") preview = `Runs every weekday (Mon–Fri) at ${timeStr}`;
    else if (type === "daily") preview = `Runs every day at ${timeStr}`;
    else if (type === "weekly") {
      const days = Array.from(state.weeklyDays).sort((a, b) => a - b);
      const label = days.length ? days.map((d) => DAY_SHORT[d]).join(", ") : "Mon";
      preview = `Runs every ${label} at ${timeStr}`;
    } else if (type === "monthly") {
      if (monthlyMode === "first_business_day") preview = `Runs on the 1st business day of each month at ${timeStr}`;
      else if (monthlyMode === "last_business_day") preview = `Runs on the last business day of each month at ${timeStr}`;
      else preview = `Runs monthly on day ${parseIntOr($("field-monthly-day").value, 1)} at ${timeStr}`;
    } else if (type === "interval") {
      const every = parseIntOr($("field-every-hours").value, 2);
      const sh = parseIntOr($("field-start-hour").value, 7);
      const eh = parseIntOr($("field-end-hour").value, 18);
      const wd = $("field-interval-weekdays").checked ? "weekdays" : "every day";
      if (eh < sh) {
        preview = "End hour must be at or after start hour (overnight windows are not supported).";
        previewEl.classList.add("is-invalid");
      } else {
        const minSuffix =
          state.intervalMinute > 0 ? ` at :${String(state.intervalMinute).padStart(2, "0")}` : "";
        preview = `Runs every ${every === 1 ? "hour" : `${every} hours`}${minSuffix}, ${formatHour12(sh)}–${formatHour12(eh)} (${wd})`;
      }
    }
    previewEl.textContent = preview;
  }

  function collectScheduleConfig() {
    const type = state.cadenceType;
    if (type === "manual") return {};
    const time = $("field-time").value || "07:00";
    if (type === "daily" || type === "weekdays") return { time };
    if (type === "weekly") {
      const days = Array.from(state.weeklyDays).sort((a, b) => a - b);
      return { time, days: days.length ? days : [0] };
    }
    if (type === "monthly") {
      const day_mode = $("field-monthly-mode").value;
      const out = { time, day_mode };
      if (day_mode === "day_of_month") out.day = parseIntOr($("field-monthly-day").value, 1);
      return out;
    }
    return {
      every_hours: parseIntOr($("field-every-hours").value, 2),
      start_hour: parseIntOr($("field-start-hour").value, 7),
      end_hour: parseIntOr($("field-end-hour").value, 18),
      minute: parseIntOr(state.intervalMinute, 0),
      weekdays_only: $("field-interval-weekdays").checked,
    };
  }

  // ==================== SHARE FILE BROWSER ====================
  function renderBrowserList() {
    const listEl = $("browser-list");
    const q = state.browserFilter.trim().toLowerCase();
    const folders = (state.browserEntries.folders || []).filter(
      (f) => !q || f.name.toLowerCase().includes(q)
    );
    const scripts = (state.browserEntries.scripts || []).filter(
      (s) => !q || s.name.toLowerCase().includes(q)
    );

    const rows = [];
    for (const folder of folders) {
      const badge = folder.is_project ? `<span class="project-tag">✦ Project</span>` : "";
      rows.push(`
        <button type="button" class="browser-item" data-browse-path="${escapeHtml(folder.path)}">
          <span class="browser-item-left">📁 <span>${escapeHtml(folder.name)}</span></span>
          ${badge}
        </button>
      `);
    }
    for (const script of scripts) {
      const icon = scriptIcon(script.name, script.ext);
      rows.push(`
        <button type="button" class="browser-item" data-pick-script="${escapeHtml(script.script_ref)}">
          <span class="browser-item-left">${icon} <span>${escapeHtml(script.name)}</span></span>
          <span class="muted">Select</span>
        </button>
      `);
    }

    if (rows.length) {
      listEl.innerHTML = rows.join("");
    } else if (q) {
      listEl.innerHTML = `<div class="browser-item muted">No folders or scripts match "${escapeHtml(state.browserFilter)}".</div>`;
    } else {
      listEl.innerHTML = `<div class="browser-item muted">No folders or supported scripts here.</div>`;
    }
  }

  async function loadBrowser(rootId, relPath = "") {
    const reqId = ++state.browserReqId;
    state.browserRootId = rootId || state.me?.roots?.[0]?.id || "projects";
    state.browserPath = relPath;
    state.browserFilter = "";
    $("browser-filter-input").value = "";

    const rootSel = $("browser-root-select");
    if (Array.from(rootSel.options).some((opt) => opt.value === state.browserRootId)) {
      rootSel.value = state.browserRootId;
    }

    const listEl = $("browser-list");
    listEl.innerHTML = `<div class="browser-item muted">Scanning folder...</div>`;

    try {
      const data = await api(
        `api/browse?root=${encodeURIComponent(state.browserRootId)}&path=${encodeURIComponent(relPath)}`
      );
      if (reqId !== state.browserReqId) return;

      $("browser-breadcrumbs").innerHTML = (data.breadcrumbs || [])
        .map((crumb, idx) => {
          const sep = idx > 0 ? `<span class="crumb-sep">/</span>` : "";
          return `${sep}<button type="button" class="crumb-btn" data-browse-path="${escapeHtml(crumb.path)}">${escapeHtml(crumb.label)}</button>`;
        })
        .join("");

      state.browserEntries = {
        folders: data.folders || [],
        scripts: data.scripts || [],
      };
      renderBrowserList();
    } catch (err) {
      if (reqId !== state.browserReqId) return;
      listEl.innerHTML = `<div class="browser-item" style="color: var(--failed)">${escapeHtml(err.message)}</div>`;
    }
  }

  function updateAutoFillField(fieldId, key, suggested) {
    const el = $(fieldId);
    const cur = el.value.trim();
    const prevAuto = state.lastAutoFilled[key];
    if (!cur || (prevAuto && cur === prevAuto)) {
      el.value = suggested || "";
      state.lastAutoFilled[key] = suggested || "";
      return Boolean(suggested);
    }
    return false;
  }

  async function pickScript(scriptRef) {
    const reqId = ++state.inspectReqId;
    $("field-script-ref").value = scriptRef;
    const txt = $("selected-script-text");
    txt.textContent = scriptRef;
    txt.classList.add("has-value");
    $("file-browser").classList.add("hidden");
    $("btn-browse-inline").textContent = "Browse";

    try {
      const info = await api(`api/inspect?script_ref=${encodeURIComponent(scriptRef)}`);
      if (reqId !== state.inspectReqId || $("field-script-ref").value !== scriptRef) return;

      const appliedName = updateAutoFillField("field-name", "name", info.suggested_name);
      const appliedDesc = updateAutoFillField("field-description", "description", info.suggested_description);
      updateAutoFillField("field-freshness", "freshness", info.suggested_freshness_path);

      if (info.readme_found && (appliedName || appliedDesc)) {
        $("readme-hint-text").textContent = "Auto-filled title & note from README.md";
        $("readme-hint").classList.remove("hidden");
      } else {
        $("readme-hint").classList.add("hidden");
      }
    } catch {
      if (reqId !== state.inspectReqId) return;
      $("readme-hint").classList.add("hidden");
    }
  }

  function parseScriptRefLocation(scriptRef) {
    const ref = String(scriptRef || "").trim();
    const defaultRoot = $("browser-root-select").value || state.me?.roots?.[0]?.id || "projects";
    if (!ref || !ref.includes(":")) return { rootId: defaultRoot, parentPath: "" };
    const idx = ref.indexOf(":");
    const rootId = ref.slice(0, idx) || defaultRoot;
    const rel = ref.slice(idx + 1).replace(/\\/g, "/");
    const lastSlash = rel.lastIndexOf("/");
    const parentPath = lastSlash > 0 ? rel.slice(0, lastSlash) : "";
    const validRoot = (state.me?.roots || []).some((r) => r.id === rootId) ? rootId : defaultRoot;
    return { rootId: validRoot, parentPath };
  }

  // ==================== JOB DRAWER ====================
  function closeDrawers() {
    $("drawer-backdrop").classList.add("hidden");
    $("job-drawer").classList.add("hidden");
    $("logs-drawer").classList.add("hidden");
    state.logsJobId = null;
    state.selectedRunId = null;
    state.logsRuns = [];
    state.selectedAiPrompt = "";
    state.deleteArmed = false;
    state.logsReqId++;
    state.runReqId++;
    state.browserReqId++;
    state.inspectReqId++;
    closeEventStream();

    const prev = state.lastFocusedEl;
    state.lastFocusedEl = null;
    if (prev && typeof prev.focus === "function" && document.contains(prev)) {
      prev.focus();
    }
  }

  function openJobDrawer(job = null) {
    if (!isAnyOverlayOpen()) {
      state.lastFocusedEl = document.activeElement;
    }
    closeEventStream();
    state.logsJobId = null;
    state.selectedRunId = null;
    state.logsReqId++;
    state.runReqId++;

    $("logs-drawer").classList.add("hidden");
    $("drawer-backdrop").classList.remove("hidden");
    $("job-drawer").classList.remove("hidden");
    $("job-form-error").classList.add("hidden");
    $("readme-hint").classList.add("hidden");
    state.deleteArmed = false;
    state.lastAutoFilled = { name: "", description: "", freshness: "" };

    const delBtn = $("btn-delete-job");
    delBtn.textContent = "Delete";
    delBtn.disabled = false;

    const roots = state.me?.roots || [];
    const rootSel = $("browser-root-select");
    rootSel.innerHTML = roots.map((r) => `<option value="${escapeHtml(r.id)}">${escapeHtml(r.label)}</option>`).join("");
    rootSel.classList.toggle("hidden", roots.length <= 1);

    if (job) {
      state.editingJobId = job.id;
      $("job-drawer-title").textContent = "Edit Script & Schedule";
      $("job-drawer-sub").textContent = `Updated by ${job.updated_by || job.owner} · ${formatRelative(job.updated_at)}`;
      $("field-job-id").value = job.id;
      $("field-script-ref").value = job.script_ref;
      $("selected-script-text").textContent = job.script_ref;
      $("selected-script-text").classList.add("has-value");
      $("field-name").value = job.name;
      $("field-description").value = job.description || "";
      $("field-dashboard-url").value = job.dashboard_url || "";
      $("field-owner").value = job.owner || state.me?.user || "";
      $("field-timeout").value = job.timeout_minutes || state.me?.default_timeout_minutes || 45;
      $("field-args").value = job.args || "";
      $("field-freshness").value = job.freshness_path || "";
      $("field-enabled").checked = !!job.enabled;
      $("file-browser").classList.add("hidden");
      $("btn-browse-inline").textContent = "Browse";
      delBtn.classList.remove("hidden");

      state.cadenceType = job.schedule_type || "manual";
      const cfg = job.schedule_config || {};
      $("field-time").value = cfg.time || "07:00";
      state.weeklyDays = new Set(Array.isArray(cfg.days) && cfg.days.length ? cfg.days : [0, 2, 4]);
      $("field-monthly-mode").value = cfg.day_mode || "first_business_day";
      $("field-monthly-day").value = parseIntOr(cfg.day, 1);
      $("field-every-hours").value = String(parseIntOr(cfg.every_hours, 2));
      $("field-start-hour").value = String(parseIntOr(cfg.start_hour, 7));
      $("field-end-hour").value = String(parseIntOr(cfg.end_hour, 18));
      state.intervalMinute = parseIntOr(cfg.minute, 0);
      $("field-interval-weekdays").checked = cfg.weekdays_only !== false;
      $("more-options").open = Boolean(job.dashboard_url || job.args || job.freshness_path || !job.enabled);
    } else {
      state.editingJobId = null;
      $("job-drawer-title").textContent = "Add Script";
      $("job-drawer-sub").textContent = `Pick a script (${supportedExtSummary()}) and set its schedule.`;
      $("field-job-id").value = "";
      $("field-script-ref").value = "";
      $("selected-script-text").textContent = "No script selected";
      $("selected-script-text").classList.remove("has-value");
      $("field-name").value = "";
      $("field-description").value = "";
      $("field-dashboard-url").value = "";
      $("field-owner").value = state.me?.user || "";
      $("field-timeout").value = state.me?.default_timeout_minutes || 45;
      $("field-args").value = "";
      $("field-freshness").value = "";
      $("field-enabled").checked = true;
      delBtn.classList.add("hidden");

      state.cadenceType = "weekdays";
      $("field-time").value = "07:00";
      state.weeklyDays = new Set([0, 2, 4]);
      $("field-monthly-mode").value = "first_business_day";
      $("field-monthly-day").value = 1;
      $("field-every-hours").value = "2";
      $("field-start-hour").value = "7";
      $("field-end-hour").value = "18";
      state.intervalMinute = 0;
      $("field-interval-weekdays").checked = true;
      $("more-options").open = false;

      $("file-browser").classList.remove("hidden");
      $("btn-browse-inline").textContent = "Hide";
      loadBrowser(roots[0]?.id, "");
    }

    updateCadenceUI();
    $("close-job-drawer").focus();
  }

  async function saveJobForm(e) {
    e.preventDefault();
    const errEl = $("job-form-error");
    errEl.classList.add("hidden");

    const form = $("job-form");
    const firstInvalid = form.querySelector(":invalid");
    if (firstInvalid) {
      const moreOpts = $("more-options");
      if (moreOpts.contains(firstInvalid)) {
        moreOpts.open = true;
      }
      firstInvalid.focus();
      form.reportValidity();
      return;
    }

    const payload = {
      name: $("field-name").value.trim(),
      description: $("field-description").value.trim(),
      script_ref: $("field-script-ref").value.trim(),
      schedule_type: state.cadenceType,
      schedule_config: collectScheduleConfig(),
      dashboard_url: $("field-dashboard-url").value.trim(),
      owner: $("field-owner").value.trim(),
      timeout_minutes: parseIntOr($("field-timeout").value, 45),
      args: $("field-args").value.trim(),
      freshness_path: $("field-freshness").value.trim(),
      enabled: $("field-enabled").checked,
    };

    if (!payload.script_ref) {
      errEl.textContent = "Please choose a script from the folder browser above.";
      errEl.classList.remove("hidden");
      return;
    }

    if (
      payload.schedule_type === "interval" &&
      payload.schedule_config.end_hour < payload.schedule_config.start_hour
    ) {
      errEl.textContent = "Interval end hour must be at or after the start hour.";
      errEl.classList.remove("hidden");
      $("field-end-hour").focus();
      return;
    }

    if (payload.dashboard_url && !isValidDashboardUrl(payload.dashboard_url)) {
      $("more-options").open = true;
      errEl.textContent = "Dashboard URL must be a relative path starting with '/' (not '//') or an http(s):// URL.";
      errEl.classList.remove("hidden");
      $("field-dashboard-url").focus();
      return;
    }

    const saveBtn = $("btn-save-job");
    saveBtn.disabled = true;
    try {
      if (state.editingJobId) {
        await api(`api/jobs/${encodeURIComponent(state.editingJobId)}`, {
          method: "PUT",
          body: JSON.stringify(payload),
        });
        showToast("Saved changes");
      } else {
        await api("api/jobs", {
          method: "POST",
          body: JSON.stringify(payload),
        });
        showToast("Script added");
      }
      closeDrawers();
      await refreshAll();
    } catch (err) {
      errEl.textContent = err.message;
      errEl.classList.remove("hidden");
    } finally {
      saveBtn.disabled = false;
    }
  }

  async function toggleJobEnabled(jobId, triggerBtn = null) {
    const job = state.jobs.find((j) => j.id === jobId);
    if (!job) return;
    if (triggerBtn) triggerBtn.disabled = true;
    try {
      await api(`api/jobs/${encodeURIComponent(jobId)}`, {
        method: "PUT",
        body: JSON.stringify({ enabled: !job.enabled }),
      });
      showToast(job.enabled ? `Paused ${job.name}` : `Resumed ${job.name}`);
      await refreshAll();
    } catch (err) {
      showToast(err.message);
    } finally {
      if (triggerBtn && document.contains(triggerBtn)) triggerBtn.disabled = false;
    }
  }

  // ==================== LOGS & TELEMETRY DRAWER ====================
  function countNewlines(str) {
    let n = 0;
    for (let i = 0; i < str.length; i++) {
      if (str.charCodeAt(i) === 10) n++;
    }
    return n;
  }

  function renderLogLineCount() {
    const count =
      state.logTotalChars === 0
        ? 0
        : state.logNewlines + (state.logEndsWithNewline ? 0 : 1);
    state.logLineCount = count;
    $("log-line-count").textContent = `${count} line${count === 1 ? "" : "s"}`;
  }

  function scrollLogToBottomIfEnabled() {
    if (!$("log-autoscroll").checked) return;
    const logEl = $("log-output");
    state.programmaticLogScroll = true;
    logEl.scrollTop = logEl.scrollHeight;
    state.lastLogScrollTop = logEl.scrollTop;
    requestAnimationFrame(() => {
      state.programmaticLogScroll = false;
    });
  }

  function setLogContent(text, isPlaceholder = false) {
    const logEl = $("log-output");
    const content = String(text || "");
    logEl.textContent = content;
    state.logHasPlaceholder = isPlaceholder;
    if (isPlaceholder || !content) {
      state.logNewlines = 0;
      state.logTotalChars = 0;
      state.logEndsWithNewline = false;
    } else {
      state.logNewlines = countNewlines(content);
      state.logTotalChars = content.length;
      state.logEndsWithNewline = content.endsWith("\n");
    }
    renderLogLineCount();
    scrollLogToBottomIfEnabled();
  }

  function appendLogChunk(chunk) {
    if (!chunk) return;
    const logEl = $("log-output");
    if (state.logHasPlaceholder) {
      logEl.textContent = "";
      state.logHasPlaceholder = false;
      state.logNewlines = 0;
      state.logTotalChars = 0;
      state.logEndsWithNewline = false;
    }
    logEl.appendChild(document.createTextNode(chunk));
    state.logNewlines += countNewlines(chunk);
    state.logTotalChars += chunk.length;
    state.logEndsWithNewline = chunk.endsWith("\n");
    renderLogLineCount();
    scrollLogToBottomIfEnabled();
  }

  function setRunStatusPill(status) {
    const pill = $("run-status-pill");
    if (status === "running" || status === "queued") {
      pill.className = "status-pill status-running";
      pill.innerHTML = `<span class="pulse-dot"></span>${status === "queued" ? "Queued" : "Running"}`;
    } else if (status === "succeeded") {
      pill.className = "status-pill status-healthy";
      pill.textContent = "Succeeded";
    } else if (status === "failed" || status === "timed_out") {
      pill.className = "status-pill status-failed";
      pill.textContent = status === "timed_out" ? "Timed out" : "Failed";
    } else if (status === "stopped") {
      pill.className = "status-pill status-paused";
      pill.textContent = "Stopped";
    } else {
      pill.className = "status-pill status-never";
      pill.textContent = "No runs yet";
    }
  }

  function updateLogsActionButtons(jobId) {
    const job = state.jobs.find((j) => j.id === jobId);
    const activeRun =
      job?.active_run ||
      state.logsRuns.find((r) => r.status === "running" || r.status === "queued") ||
      null;
    const stopBtn = $("btn-logs-stop");
    const runBtn = $("btn-logs-run-now");
    stopBtn.dataset.activeRunId = activeRun ? activeRun.id : "";
    stopBtn.classList.toggle("hidden", !activeRun);
    runBtn.classList.toggle("hidden", Boolean(activeRun));
  }

  function closeEventStream() {
    if (state.eventSource) {
      state.eventSource.close();
      state.eventSource = null;
    }
    if (state.sseFallbackPollTimer) {
      clearInterval(state.sseFallbackPollTimer);
      state.sseFallbackPollTimer = null;
    }
    state.sseRunId = null;
    state.sseReconnectAttempts = 0;
    const badge = $("log-live-badge");
    badge.classList.add("hidden");
    badge.classList.remove("is-reconnecting");
    $("btn-log-reconnect").classList.add("hidden");
  }

  async function openLogsDrawer(jobId, preferredRunId = null) {
    if (!isAnyOverlayOpen()) {
      state.lastFocusedEl = document.activeElement;
    }
    closeEventStream();
    const reqId = ++state.logsReqId;
    state.runReqId++;

    $("job-drawer").classList.add("hidden");
    $("drawer-backdrop").classList.remove("hidden");
    $("logs-drawer").classList.remove("hidden");
    state.logsJobId = jobId;
    state.selectedRunId = null;

    const job = state.jobs.find((j) => j.id === jobId);
    $("logs-drawer-title").textContent = job ? job.name : "Run Telemetry";
    $("logs-drawer-script").textContent = job ? job.script_ref : "";
    const descEl = $("logs-drawer-desc");
    if (job?.description) {
      descEl.textContent = job.description;
      descEl.classList.remove("hidden");
    } else {
      descEl.textContent = "";
      descEl.classList.add("hidden");
    }

    updateLogsActionButtons(jobId);
    $("close-logs-drawer").focus();

    try {
      const data = await api(`api/jobs/${encodeURIComponent(jobId)}/runs`);
      if (reqId !== state.logsReqId || state.logsJobId !== jobId) return;

      state.logsRuns = data.runs || [];
      renderHistoryStrip();
      updateLogsActionButtons(jobId);

      const targetId = preferredRunId || state.logsRuns[0]?.id || null;
      if (targetId) {
        await selectRun(targetId);
      } else {
        state.selectedRunId = null;
        state.selectedAiPrompt = "";
        setRunStatusPill(null);
        $("run-meta-text").textContent = 'Click "Run now" to test this script.';
        $("run-error-callout").classList.add("hidden");
        $("btn-copy-ai").classList.add("hidden");
        updateLogsActionButtons(jobId);
        setLogContent("No run logs yet for this script.", true);
      }
    } catch (err) {
      if (reqId !== state.logsReqId) return;
      showToast(err.message);
    }
  }

  function renderHistoryStrip() {
    const strip = $("logs-history-strip");
    if (!state.logsRuns.length) {
      strip.innerHTML = `<span class="muted">No runs recorded yet</span>`;
      return;
    }
    const ordered = [...state.logsRuns].slice(0, 20).reverse();
    const maxDur = Math.max(1, ...ordered.map((r) => Number(r.duration_sec) || 1));

    strip.innerHTML = ordered
      .map((r) => {
        const sel = r.id === state.selectedRunId ? "selected" : "";
        const dur = Number(r.duration_sec) || 1;
        const pct = Math.max(30, Math.min(100, Math.round(30 + (dur / maxDur) * 70)));
        const tip = `${r.status} · ${formatTimestamp(r.started_at || r.queued_at)}${r.duration_sec != null ? " · " + formatDuration(r.duration_sec) : ""}`;
        return `<button type="button" class="history-bar bar-${escapeHtml(r.status)} ${sel}" style="height:${pct}%" data-select-run="${escapeHtml(r.id)}" title="${escapeHtml(tip)}" aria-label="${escapeHtml(tip)}"></button>`;
      })
      .join("");
  }

  async function selectRun(runId) {
    const reqId = ++state.runReqId;
    const expectedJobId = state.logsJobId;
    closeEventStream();
    state.selectedRunId = runId;
    renderHistoryStrip();

    try {
      const data = await api(`api/runs/${encodeURIComponent(runId)}`);
      if (
        reqId !== state.runReqId ||
        state.selectedRunId !== runId ||
        state.logsJobId !== expectedJobId
      ) {
        return;
      }

      const run = data.run;
      state.selectedAiPrompt = data.ai_prompt || "";

      const idx = state.logsRuns.findIndex((r) => r.id === run.id);
      if (idx >= 0) {
        state.logsRuns[idx] = { ...state.logsRuns[idx], ...run };
        renderHistoryStrip();
      }

      const isActive = run.status === "running" || run.status === "queued";
      const isFailed = run.status === "failed" || run.status === "timed_out";

      setRunStatusPill(run.status);

      const bits = [];
      if (run.triggered_by) bits.push(`by ${run.triggered_by}`);
      bits.push(formatTimestamp(run.started_at || run.queued_at));
      if (run.duration_sec != null) bits.push(`took ${formatDuration(run.duration_sec)}`);
      if (run.exit_code !== null && run.exit_code !== undefined) bits.push(`exit ${run.exit_code}`);
      $("run-meta-text").textContent = bits.join(" · ");

      updateLogsActionButtons(expectedJobId);
      $("btn-copy-ai").classList.toggle("hidden", !isFailed);

      const errBox = $("run-error-callout");
      if (run.error_summary) {
        errBox.textContent = run.error_summary;
        errBox.classList.remove("hidden");
      } else if (isFailed) {
        errBox.textContent =
          run.status === "timed_out"
            ? "Script timed out."
            : `Script failed with exit code ${run.exit_code ?? "unknown"}.`;
        errBox.classList.remove("hidden");
      } else {
        errBox.classList.add("hidden");
      }

      const hasLog = Boolean(data.log);
      setLogContent(
        hasLog ? data.log : isActive ? "Waiting for output..." : "(No console output)",
        !hasLog
      );

      if (isActive) {
        startLiveStream(runId, reqId);
      }
    } catch (err) {
      if (reqId !== state.runReqId) return;
      showToast(err.message);
    }
  }

  function startFallbackRunPoll(runId, runReqId) {
    if (state.sseFallbackPollTimer) clearInterval(state.sseFallbackPollTimer);
    state.sseFallbackPollTimer = setInterval(async () => {
      if (
        state.selectedRunId !== runId ||
        state.runReqId !== runReqId ||
        $("logs-drawer").classList.contains("hidden")
      ) {
        clearInterval(state.sseFallbackPollTimer);
        state.sseFallbackPollTimer = null;
        return;
      }
      if (document.hidden) return;
      try {
        const data = await api(`api/runs/${encodeURIComponent(runId)}`);
        if (state.selectedRunId !== runId || state.runReqId !== runReqId) return;
        const run = data.run;
        setRunStatusPill(run.status);
        if (data.log) {
          setLogContent(data.log, false);
        }
        if (run.status !== "running" && run.status !== "queued") {
          closeEventStream();
          await refreshAll();
          if (state.logsJobId && state.selectedRunId === runId && state.runReqId === runReqId) {
            const runsData = await api(`api/jobs/${encodeURIComponent(state.logsJobId)}/runs`);
            if (state.selectedRunId !== runId || state.runReqId !== runReqId) return;
            state.logsRuns = runsData.runs || [];
            await selectRun(runId);
          }
        }
      } catch {
        // Keep trying gently until drawer closes or user clicks Reconnect
      }
    }, 4000);
  }

  function startLiveStream(runId, runReqId) {
    closeEventStream();
    const badge = $("log-live-badge");
    badge.classList.remove("hidden", "is-reconnecting");
    badge.innerHTML = `<span class="pulse-dot"></span>Streaming live`;
    $("btn-log-reconnect").classList.add("hidden");

    const es = new EventSource(`api/runs/${encodeURIComponent(runId)}/stream`);
    state.eventSource = es;
    state.sseRunId = runId;
    state.sseReconnectAttempts = 0;

    es.onopen = () => {
      if (state.eventSource !== es) return;
      state.sseReconnectAttempts = 0;
      badge.classList.remove("is-reconnecting");
      badge.innerHTML = `<span class="pulse-dot"></span>Streaming live`;
      $("btn-log-reconnect").classList.add("hidden");
    };

    es.onmessage = async (ev) => {
      if (state.eventSource !== es || state.selectedRunId !== runId || state.runReqId !== runReqId) {
        return;
      }
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "snapshot") {
          if (msg.status) setRunStatusPill(msg.status);
          setLogContent(msg.log || "Waiting for output...", !msg.log);
        } else if (msg.type === "status") {
          if (msg.status) {
            setRunStatusPill(msg.status);
            const idx = state.logsRuns.findIndex((r) => r.id === runId);
            if (idx >= 0) {
              state.logsRuns[idx].status = msg.status;
              renderHistoryStrip();
            }
          }
        } else if (msg.type === "line") {
          appendLogChunk(msg.text || "");
        } else if (msg.type === "done") {
          closeEventStream();
          await refreshAll();
          if (state.logsJobId && state.selectedRunId === runId && state.runReqId === runReqId) {
            const runsData = await api(`api/jobs/${encodeURIComponent(state.logsJobId)}/runs`);
            if (state.selectedRunId !== runId || state.runReqId !== runReqId) return;
            state.logsRuns = runsData.runs || [];
            await selectRun(runId);
          }
        }
      } catch {
        // Ignore malformed SSE chunks
      }
    };

    es.onerror = () => {
      if (state.eventSource !== es || state.selectedRunId !== runId || state.runReqId !== runReqId) {
        return;
      }
      state.sseReconnectAttempts++;
      if (state.sseReconnectAttempts <= 3) {
        badge.classList.remove("hidden");
        badge.classList.add("is-reconnecting");
        badge.innerHTML = `<span class="pulse-dot"></span>Reconnecting… (${state.sseReconnectAttempts}/3)`;
      } else {
        es.close();
        state.eventSource = null;
        badge.classList.remove("hidden");
        badge.classList.add("is-reconnecting");
        badge.textContent = "Stream paused · polling";
        $("btn-log-reconnect").classList.remove("hidden");
        startFallbackRunPoll(runId, runReqId);
      }
    };
  }

  async function triggerRunNow(jobId, openDrawerAfter = true, triggerBtn = null) {
    if (triggerBtn) triggerBtn.disabled = true;
    try {
      const data = await api(`api/jobs/${encodeURIComponent(jobId)}/run`, { method: "POST" });
      showToast("Script started");
      await refreshAll();
      if (openDrawerAfter) {
        await openLogsDrawer(jobId, data.run?.id);
      }
    } catch (err) {
      showToast(err.message);
    } finally {
      if (triggerBtn && document.contains(triggerBtn)) triggerBtn.disabled = false;
    }
  }

  async function stopRun(runId, triggerBtn = null) {
    if (triggerBtn) triggerBtn.disabled = true;
    try {
      await api(`api/runs/${encodeURIComponent(runId)}/stop`, { method: "POST" });
      showToast("Stopping run...");
      await refreshAll();
      if (
        state.logsJobId &&
        state.selectedRunId === runId &&
        !$("logs-drawer").classList.contains("hidden")
      ) {
        updateLogsActionButtons(state.logsJobId);
      }
    } catch (err) {
      showToast(err.message);
    } finally {
      if (triggerBtn && document.contains(triggerBtn)) triggerBtn.disabled = false;
    }
  }

  async function quickCopyAiError(runId, triggerBtn = null) {
    if (triggerBtn) triggerBtn.disabled = true;
    try {
      const data = await api(`api/runs/${encodeURIComponent(runId)}`);
      if (data.ai_prompt) {
        await copyText(data.ai_prompt, "Copied scrubbed error context for AI agent");
      } else {
        showToast("No error context available for this run.");
      }
    } catch (err) {
      showToast(err.message);
    } finally {
      if (triggerBtn && document.contains(triggerBtn)) triggerBtn.disabled = false;
    }
  }

  function openCopyModal(text, title = "Copy text manually") {
    state.lastCopyFocusEl = document.activeElement;
    $("copy-modal-title").textContent = title;
    const ta = $("copy-modal-text");
    ta.value = text;
    $("copy-modal-backdrop").classList.remove("hidden");
    ta.focus();
    ta.select();
  }

  function closeCopyModal() {
    $("copy-modal-backdrop").classList.add("hidden");
    const prev = state.lastCopyFocusEl;
    state.lastCopyFocusEl = null;
    if (prev && typeof prev.focus === "function" && document.contains(prev)) {
      prev.focus();
    }
  }

  async function copyText(text, successMsg) {
    if (window.isSecureContext && navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      try {
        await navigator.clipboard.writeText(text);
        showToast(successMsg);
        return true;
      } catch {
        // Fall through to off-screen execCommand fallback
      }
    }

    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "0";
    ta.style.left = "0";
    ta.style.width = "1px";
    ta.style.height = "1px";
    ta.style.opacity = "0";
    ta.style.pointerEvents = "none";
    document.body.appendChild(ta);

    let copied = false;
    try {
      ta.focus({ preventScroll: true });
      ta.select();
      ta.setSelectionRange(0, ta.value.length);
      copied = document.execCommand("copy");
    } catch {
      copied = false;
    } finally {
      ta.remove();
    }

    if (copied) {
      showToast(successMsg);
      return true;
    }

    showToast("Clipboard copy was blocked — press Ctrl+C in the dialog.");
    openCopyModal(text);
    return false;
  }

  function switchView(viewName) {
    state.view = viewName;
    document.querySelectorAll(".nav-tab").forEach((t) => t.classList.toggle("active", t.dataset.view === viewName));
    $("view-scripts").classList.toggle("hidden", viewName !== "scripts");
    $("view-schedules").classList.toggle("hidden", viewName !== "schedules");
    $("view-results").classList.toggle("hidden", viewName !== "results");
    if (viewName === "scripts") renderScriptsView();
    if (viewName === "schedules") renderSchedulesView();
    if (viewName === "results") renderResultsView();
  }

  // ==================== EVENTS ====================
  function bindEvents() {
    document.querySelectorAll(".nav-tab").forEach((tab) => {
      tab.addEventListener("click", () => switchView(tab.dataset.view));
    });

    $("btn-conn-retry").addEventListener("click", async () => {
      await loadMe();
      await refreshAll();
    });

    // Scripts filters
    document.querySelectorAll("[data-filter]").forEach((pill) => {
      pill.addEventListener("click", () => {
        state.filter = pill.dataset.filter;
        document.querySelectorAll("[data-filter]").forEach((p) => {
          const active = p === pill;
          p.classList.toggle("active", active);
          p.setAttribute("aria-selected", active ? "true" : "false");
        });
        renderScriptsView();
      });
    });

    $("search-input").addEventListener("input", (e) => {
      state.search = e.target.value;
      renderScriptsView();
    });

    $("filter-mine").addEventListener("change", (e) => {
      state.mineOnly = e.target.checked;
      renderScriptsView();
    });

    // Schedules controls
    $("timeline-show-all-hours").addEventListener("change", (e) => {
      state.showAllHours = e.target.checked;
      renderSchedulesView();
    });

    // Results filters
    document.querySelectorAll("[data-res-filter]").forEach((pill) => {
      pill.addEventListener("click", () => {
        state.resultsFilter = pill.dataset.resFilter;
        document.querySelectorAll("[data-res-filter]").forEach((p) => {
          const active = p === pill;
          p.classList.toggle("active", active);
          p.setAttribute("aria-selected", active ? "true" : "false");
        });
        renderResultsView();
      });
    });

    $("results-search-input").addEventListener("input", (e) => {
      state.resultsSearch = e.target.value;
      renderResultsView();
    });

    $("btn-new-job").addEventListener("click", () => openJobDrawer(null));
    $("btn-empty-new").addEventListener("click", () => openJobDrawer(null));

    $("drawer-backdrop").addEventListener("click", closeDrawers);
    $("close-job-drawer").addEventListener("click", closeDrawers);
    $("btn-cancel-job").addEventListener("click", closeDrawers);
    $("close-logs-drawer").addEventListener("click", closeDrawers);

    // Copy modal controls
    $("close-copy-modal").addEventListener("click", closeCopyModal);
    $("btn-copy-modal-done").addEventListener("click", closeCopyModal);
    $("btn-copy-modal-select").addEventListener("click", () => {
      const ta = $("copy-modal-text");
      ta.focus();
      ta.select();
    });
    $("copy-modal-backdrop").addEventListener("click", (e) => {
      if (e.target === $("copy-modal-backdrop")) closeCopyModal();
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        if (!$("copy-modal-backdrop").classList.contains("hidden")) {
          closeCopyModal();
        } else if (isAnyOverlayOpen()) {
          closeDrawers();
        }
      } else if (
        e.key === "/" &&
        !isAnyOverlayOpen() &&
        !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)
      ) {
        e.preventDefault();
        if (state.view === "results") $("results-search-input").focus();
        else {
          switchView("scripts");
          $("search-input").focus();
        }
      }
    });

    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) {
        refreshAll({ background: true });
      }
    });

    // Delegated clicks — Scripts tab
    $("jobs-list").addEventListener("click", (e) => {
      const target = e.target.closest("[data-run-job],[data-stop-run],[data-open-run],[data-open-logs],[data-edit-job],[data-quick-ai]");
      if (!target) return;
      if (target.dataset.runJob) triggerRunNow(target.dataset.runJob, true, target);
      else if (target.dataset.stopRun) stopRun(target.dataset.stopRun, target);
      else if (target.dataset.openRun) openLogsDrawer(target.dataset.jobId, target.dataset.openRun);
      else if (target.dataset.quickAi) quickCopyAiError(target.dataset.quickAi, target);
      else if (target.dataset.openLogs) openLogsDrawer(target.dataset.openLogs, target.dataset.runId || null);
      else if (target.dataset.editJob) {
        const job = state.jobs.find((j) => j.id === target.dataset.editJob);
        if (job) openJobDrawer(job);
      }
    });

    // Delegated clicks — Schedules tab
    $("view-schedules").addEventListener("click", (e) => {
      const toggleBtn = e.target.closest("[data-toggle-enabled]");
      if (toggleBtn) {
        toggleJobEnabled(toggleBtn.dataset.toggleEnabled, toggleBtn);
        return;
      }
      const editBtn = e.target.closest("[data-edit-job]");
      if (editBtn) {
        const job = state.jobs.find((j) => j.id === editBtn.dataset.editJob);
        if (job) openJobDrawer(job);
      }
    });

    // Delegated clicks & keyboard — Results tab
    $("results-list").addEventListener("click", (e) => {
      const aiBtn = e.target.closest("[data-quick-ai]");
      if (aiBtn) {
        e.stopPropagation();
        quickCopyAiError(aiBtn.dataset.quickAi, aiBtn);
        return;
      }
      const stopBtn = e.target.closest("[data-stop-run]");
      if (stopBtn) {
        e.stopPropagation();
        stopRun(stopBtn.dataset.stopRun, stopBtn);
        return;
      }
      const row = e.target.closest("[data-open-run]");
      if (row) {
        openLogsDrawer(row.dataset.jobId, row.dataset.openRun);
      }
    });

    $("results-list").addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      const row = e.target.closest(".result-row[data-open-run]");
      if (!row || e.target !== row) return;
      e.preventDefault();
      openLogsDrawer(row.dataset.jobId, row.dataset.openRun);
    });

    // File browser
    $("btn-browse-inline").addEventListener("click", () => {
      const box = $("file-browser");
      const opening = box.classList.contains("hidden");
      box.classList.toggle("hidden");
      $("btn-browse-inline").textContent = opening ? "Hide" : "Browse";
      if (opening) {
        const { rootId, parentPath } = parseScriptRefLocation($("field-script-ref").value);
        loadBrowser(rootId, parentPath);
      }
    });

    $("browser-root-select").addEventListener("change", (e) => loadBrowser(e.target.value, ""));
    $("browser-filter-input").addEventListener("input", (e) => {
      state.browserFilter = e.target.value;
      renderBrowserList();
    });
    $("browser-breadcrumbs").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-browse-path]");
      if (btn) loadBrowser(state.browserRootId, btn.dataset.browsePath);
    });
    $("browser-list").addEventListener("click", (e) => {
      const folderBtn = e.target.closest("[data-browse-path]");
      if (folderBtn) {
        loadBrowser(state.browserRootId, folderBtn.dataset.browsePath);
        return;
      }
      const scriptBtn = e.target.closest("[data-pick-script]");
      if (scriptBtn) pickScript(scriptBtn.dataset.pickScript);
    });

    // Cadence controls
    $("cadence-pills").addEventListener("click", (e) => {
      const pill = e.target.closest(".cadence-pill");
      if (!pill) return;
      state.cadenceType = pill.dataset.type;
      updateCadenceUI();
    });

    $("weekly-day-chips").addEventListener("click", (e) => {
      const chip = e.target.closest(".day-chip");
      if (!chip) return;
      const d = Number(chip.dataset.day);
      if (state.weeklyDays.has(d) && state.weeklyDays.size > 1) state.weeklyDays.delete(d);
      else state.weeklyDays.add(d);
      updateCadenceUI();
    });

    [
      "field-time",
      "field-monthly-mode",
      "field-monthly-day",
      "field-every-hours",
      "field-start-hour",
      "field-end-hour",
      "field-interval-weekdays",
    ].forEach((id) => $(id).addEventListener("change", updateCadenceUI));

    ["field-time", "field-monthly-day"].forEach((id) =>
      $(id).addEventListener("input", updateCadenceUI)
    );

    // Auto-open #more-options if native validation fails inside it
    $("job-form").addEventListener(
      "invalid",
      (e) => {
        const moreOpts = $("more-options");
        if (moreOpts.contains(e.target)) {
          moreOpts.open = true;
        }
      },
      true
    );

    $("job-form").addEventListener("submit", saveJobForm);

    $("btn-delete-job").addEventListener("click", async () => {
      if (!state.editingJobId) return;
      const btn = $("btn-delete-job");
      if (!state.deleteArmed) {
        state.deleteArmed = true;
        btn.textContent = "Confirm delete (deletes script & history)";
        return;
      }
      btn.disabled = true;
      try {
        await api(`api/jobs/${encodeURIComponent(state.editingJobId)}`, { method: "DELETE" });
        showToast("Script deleted");
        closeDrawers();
        await refreshAll();
      } catch (err) {
        $("job-form-error").textContent = err.message;
        $("job-form-error").classList.remove("hidden");
        state.deleteArmed = false;
        btn.textContent = "Delete";
      } finally {
        btn.disabled = false;
      }
    });

    // Logs drawer controls
    $("logs-history-strip").addEventListener("click", (e) => {
      const bar = e.target.closest("[data-select-run]");
      if (bar) selectRun(bar.dataset.selectRun);
    });

    $("btn-logs-run-now").addEventListener("click", (e) => {
      if (state.logsJobId) triggerRunNow(state.logsJobId, true, e.currentTarget);
    });

    $("btn-logs-stop").addEventListener("click", (e) => {
      const btn = e.currentTarget;
      const targetRunId = btn.dataset.activeRunId || state.selectedRunId;
      if (targetRunId) stopRun(targetRunId, btn);
    });

    $("btn-copy-ai").addEventListener("click", () => {
      if (state.selectedAiPrompt) copyText(state.selectedAiPrompt, "Copied error context for AI agent");
    });

    $("btn-copy-log").addEventListener("click", () => {
      copyText($("log-output").textContent || "", "Copied log to clipboard");
    });

    $("btn-log-reconnect").addEventListener("click", () => {
      if (state.selectedRunId) {
        startLiveStream(state.selectedRunId, state.runReqId);
      }
    });

    // Pause autoscroll when user scrolls away from bottom; resume when scrolled back
    $("log-output").addEventListener("scroll", (e) => {
      if (state.programmaticLogScroll) return;
      const el = e.currentTarget;
      const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
      const autoBox = $("log-autoscroll");
      if (el.scrollTop < state.lastLogScrollTop && distFromBottom > 24) {
        if (autoBox.checked) autoBox.checked = false;
      } else if (distFromBottom <= 8) {
        if (!autoBox.checked) autoBox.checked = true;
      }
      state.lastLogScrollTop = el.scrollTop;
    });
  }

  async function loadMe() {
    try {
      state.me = await api("api/me");
      document.title = state.me.app_name || "Analytics Scheduler";
      $("app-title").textContent = state.me.app_name || "Analytics Scheduler";
      $("user-name").textContent = state.me.user || "Analyst";
      $("user-avatar").textContent = (state.me.user || "A").slice(0, 1).toUpperCase();
      updateGreeting();
      updateDynamicCopy();
      hideConnBanner();
      return true;
    } catch (err) {
      $("user-name").textContent = "Offline";
      $("user-avatar").textContent = "!";
      showConnBanner(err.message || "Unable to connect to Analytics Scheduler.");
      showToast(err.message);
      return false;
    }
  }

  async function init() {
    populateHourSelects();
    bindEvents();
    await loadMe();
    await refreshAll();
    scheduleNextPoll();
  }

  init();
})();
