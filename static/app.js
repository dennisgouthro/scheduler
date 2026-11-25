(() => {
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

    // Job drawer state
    editingJobId: null,
    deleteArmed: false,
    cadenceType: "weekdays",
    weeklyDays: new Set([0, 2, 4]),
    browserRootId: null,
    browserPath: "",

    // Logs drawer state
    logsJobId: null,
    logsRuns: [],
    selectedRunId: null,
    selectedAiPrompt: "",
    eventSource: null,
  };

  const DAY_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const $ = (id) => document.getElementById(id);

  async function api(path, options = {}) {
    const res = await fetch(path.replace(/^\//, ""), {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    const text = await res.text();
    let data = {};
    try {
      data = text ? JSON.parse(text) : {};
    } catch {
      data = { error: text };
    }
    if (!res.ok) {
      throw new Error(data.error || `Request failed (${res.status})`);
    }
    return data;
  }

  let toastTimer = null;
  function showToast(msg) {
    const el = $("toast");
    el.textContent = msg;
    el.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), 2800);
  }

  function escapeHtml(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
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
    const h = Number(hStr) || 0;
    const m = Number(mStr) || 0;
    const suffix = h < 12 ? "AM" : "PM";
    const h12 = h % 12 || 12;
    return `${h12}:${String(m).padStart(2, "0")} ${suffix}`;
  }

  function jobHealthState(job) {
    if (job.active_run) return "running";
    if (!job.enabled) return "paused";
    if (!job.last_run) return "never";
    if (job.last_run.status === "succeeded") return "healthy";
    if (job.last_run.status === "failed" || job.last_run.status === "timed_out") return "failed";
    return "paused";
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

    const failedJobs = state.jobs.filter((j) => jobHealthState(j) === "failed").length;
    const badge = $("nav-badge-failed");
    badge.textContent = failedJobs;
    badge.classList.toggle("hidden", failedJobs === 0);
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
      if (state.mineOnly && (job.owner || "").toLowerCase() !== myName) return false;
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
    if (st === "paused") {
      return `<span class="status-pill status-paused">${job.enabled ? "Stopped" : "Paused"}</span>`;
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
  function renderScriptsView() {
    renderCounts();
    renderNavCounts();
    renderQueuePill();

    const list = $("jobs-list");
    const panel = $("deck-panel");
    const empty = $("empty-state");
    const filtered = getFilteredJobs();

    if (!filtered.length) {
      list.innerHTML = "";
      panel.classList.add("hidden");
      empty.classList.remove("hidden");
      if (state.jobs.length === 0) {
        $("empty-title").textContent = "No scripts registered";
        $("empty-body").textContent =
          "Pick any Python script from your analytics share and configure when it runs in seconds.";
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
        const nextTxt = job.next_run_at
          ? `Next: ${formatRelative(job.next_run_at)}`
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
                return `<button class="mini-bar bar-${escapeHtml(r.status)}" style="height:${h}px" data-open-run="${escapeHtml(r.id)}" data-job-id="${escapeHtml(job.id)}" title="${escapeHtml(tip)}" aria-label="${escapeHtml(tip)}"></button>`;
              })
              .join("")
          : `<span class="muted">—</span>`;

        const dashLink = job.dashboard_url
          ? `<a class="dash-link" href="${escapeHtml(job.dashboard_url)}" target="_blank" rel="noopener" title="Open live dashboard">↗</a>`
          : "";

        const actionPrimary = job.active_run
          ? `<button class="btn btn-danger btn-xs" data-stop-run="${escapeHtml(job.active_run.id)}">■ Stop</button>`
          : `<button class="btn btn-run btn-xs" data-run-job="${escapeHtml(job.id)}">▶ Run</button>`;

        const rowClasses = [
          "job-row",
          !job.enabled ? "is-paused" : "",
          st === "failed" ? "is-failed" : "",
          st === "running" ? "is-running" : "",
        ]
          .filter(Boolean)
          .join(" ");

        const errorBar =
          st === "failed" && job.last_run?.error_summary
            ? `<div class="row-error-bar">
                 <span class="row-error-text" data-open-logs="${escapeHtml(job.id)}" data-run-id="${escapeHtml(job.last_run.id)}" title="Click to inspect full log">${escapeHtml(job.last_run.error_summary)}</span>
                 <div class="row-error-actions">
                   <button class="btn btn-ai btn-xs" data-quick-ai="${escapeHtml(job.last_run.id)}">✦ AI Fix</button>
                 </div>
               </div>`
            : "";

        return `
          <div class="${rowClasses}">
            <div class="job-row-main">
              <div class="col-pipeline">
                <span class="orb orb-${st}"></span>
                <div class="pipeline-text">
                  <div class="job-title-line">
                    <button class="job-title-btn" data-open-logs="${escapeHtml(job.id)}">${escapeHtml(job.name)}</button>
                    ${dashLink}
                  </div>
                  <div class="job-subline" title="${escapeHtml(job.script_ref)}">${escapeHtml(job.script_ref)}</div>
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
                <button class="btn btn-ghost btn-xs" data-open-logs="${escapeHtml(job.id)}">Logs</button>
                <button class="btn btn-ghost btn-xs" data-edit-job="${escapeHtml(job.id)}">Edit</button>
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
            `<button class="upcoming-chip" data-edit-job="${escapeHtml(j.id)}">
               <span class="upcoming-time">${escapeHtml(formatRelative(j.next_run_at))}</span>
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

    const headCells = DAY_SHORT.map((d) => `<th>${d}</th>`).join("");
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
              return `<button class="tl-chip" data-edit-job="${escapeHtml(job.id)}" title="${escapeHtml(job.name)} — ${escapeHtml(job.schedule_label)}"><span class="tl-chip-time">${mm}</span>${escapeHtml(job.name)}${mTag}</button>`;
            })
            .join("");
          return `<td>${chips}</td>`;
        }).join("");

        const busy = maxInCell >= 3;
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
          const nextTxt = j.next_run_at ? formatRelative(j.next_run_at) : "—";
          const stateBadge = j.enabled
            ? `<span class="status-pill status-healthy">Active</span>`
            : `<span class="status-pill status-paused">Paused</span>`;
          const toggleLabel = j.enabled ? "Pause" : "Resume";
          return `
            <div class="roster-row ${!j.enabled ? "is-paused" : ""}">
              <div class="pipeline-text">
                <span class="cadence-main" style="font-weight:600">${escapeHtml(j.name)}</span>
                <span class="job-subline">${escapeHtml(j.owner || "Team")}</span>
              </div>
              <span class="cadence-main">${escapeHtml(j.schedule_label)}</span>
              <span class="result-mono">${escapeHtml(nextTxt)}</span>
              <div>${stateBadge}</div>
              <div class="col-actions">
                <button class="btn btn-subtle btn-xs" data-toggle-enabled="${escapeHtml(j.id)}">${toggleLabel}</button>
                <button class="btn btn-ghost btn-xs" data-edit-job="${escapeHtml(j.id)}">Reschedule</button>
              </div>
            </div>
          `;
        })
        .join("");
    }
  }

  // ==================== 3. RESULTS VIEW (Unified Team Run Feed) ====================
  function renderResultsView() {
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
    const rate = finished > 0 ? Math.round((ok / finished) * 100) : 100;
    $("res-pass-rate").textContent = finished > 0 ? `${rate}% pass rate` : "No completed runs";

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
    const list = $("results-list");

    if (!filtered.length) {
      list.innerHTML = "";
      panel.classList.add("hidden");
      empty.classList.remove("hidden");
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
          ? r.error_summary || `Exited with code ${r.exit_code}`
          : isActive
            ? "Streaming console output..."
            : r.status === "stopped"
              ? r.error_summary || "Stopped"
              : "Completed cleanly (exit 0)";

        return `
          <div class="result-row ${isFailed ? "is-failed" : ""} ${isActive ? "is-running" : ""}" data-open-run="${escapeHtml(r.id)}" data-job-id="${escapeHtml(r.job_id)}">
            <div>${renderRunStatusPill(r.status)}</div>
            <div class="pipeline-text">
              <span class="cadence-main" style="font-weight:600">${escapeHtml(r.job_name)}</span>
              <span class="job-subline">${escapeHtml(r.script_ref || "")}</span>
            </div>
            <span class="res-trigger">${escapeHtml(trig)}</span>
            <span class="result-mono" title="${escapeHtml(formatTimestamp(r.started_at || r.queued_at))}">${escapeHtml(formatRelative(r.started_at || r.queued_at))}</span>
            <span class="result-mono">${escapeHtml(formatDuration(r.duration_sec))}</span>
            <span class="result-outcome ${isFailed ? "is-err" : ""}" title="${escapeHtml(outcomeText)}">${escapeHtml(outcomeText)}</span>
            <div class="col-actions">
              ${isFailed ? `<button class="btn btn-ai btn-xs" data-quick-ai="${escapeHtml(r.id)}">✦ AI Fix</button>` : ""}
              ${isActive ? `<button class="btn btn-danger btn-xs" data-stop-run="${escapeHtml(r.id)}">■ Stop</button>` : `<button class="btn btn-ghost btn-xs" data-open-run="${escapeHtml(r.id)}" data-job-id="${escapeHtml(r.job_id)}">Logs</button>`}
            </div>
          </div>
        `;
      })
      .join("");
  }

  async function refreshAll() {
    try {
      const [jobsData, runsData] = await Promise.all([api("api/jobs"), api("api/runs?limit=120")]);
      state.jobs = jobsData.jobs || [];
      state.queue = jobsData.queue || state.queue;
      state.allRuns = runsData.runs || [];

      renderScriptsView();
      if (state.view === "schedules") renderSchedulesView();
      if (state.view === "results") renderResultsView();
    } catch (err) {
      showToast(err.message);
    }
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

    $("row-time").classList.toggle("hidden", type === "manual" || type === "interval");
    $("row-weekly-days").classList.toggle("hidden", type !== "weekly");
    $("row-monthly").classList.toggle("hidden", type !== "monthly");
    $("row-interval").classList.toggle("hidden", type !== "interval");

    const monthlyMode = $("field-monthly-mode").value;
    $("field-monthly-day").classList.toggle("hidden", monthlyMode !== "day_of_month");

    document.querySelectorAll(".day-chip").forEach((chip) => {
      chip.classList.toggle("active", state.weeklyDays.has(Number(chip.dataset.day)));
    });

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
      else preview = `Runs monthly on day ${$("field-monthly-day").value || 1} at ${timeStr}`;
    } else if (type === "interval") {
      const every = Number($("field-every-hours").value) || 2;
      const sh = Number($("field-start-hour").value) || 7;
      const eh = Number($("field-end-hour").value) || 18;
      const wd = $("field-interval-weekdays").checked ? "weekdays" : "every day";
      preview = `Runs every ${every === 1 ? "hour" : `${every} hours`}, ${formatHour12(sh)}–${formatHour12(eh)} (${wd})`;
    }
    $("schedule-preview").textContent = preview;
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
      if (day_mode === "day_of_month") out.day = Number($("field-monthly-day").value) || 1;
      return out;
    }
    return {
      every_hours: Number($("field-every-hours").value) || 2,
      start_hour: Number($("field-start-hour").value) || 7,
      end_hour: Math.max(Number($("field-start-hour").value) || 7, Number($("field-end-hour").value) || 18),
      minute: 0,
      weekdays_only: $("field-interval-weekdays").checked,
    };
  }

  // ==================== SHARE FILE BROWSER ====================
  async function loadBrowser(rootId, relPath = "") {
    state.browserRootId = rootId || state.me?.roots?.[0]?.id || "projects";
    state.browserPath = relPath;
    const listEl = $("browser-list");
    listEl.innerHTML = `<div class="browser-item muted">Scanning folder...</div>`;

    try {
      const data = await api(
        `api/browse?root=${encodeURIComponent(state.browserRootId)}&path=${encodeURIComponent(relPath)}`
      );

      $("browser-breadcrumbs").innerHTML = data.breadcrumbs
        .map((crumb, idx) => {
          const sep = idx > 0 ? `<span class="crumb-sep">/</span>` : "";
          return `${sep}<button type="button" class="crumb-btn" data-browse-path="${escapeHtml(crumb.path)}">${escapeHtml(crumb.label)}</button>`;
        })
        .join("");

      const rows = [];
      for (const folder of data.folders || []) {
        const badge = folder.is_project ? `<span class="project-tag">✦ Project</span>` : "";
        rows.push(`
          <button type="button" class="browser-item" data-browse-path="${escapeHtml(folder.path)}">
            <span class="browser-item-left">📁 <span>${escapeHtml(folder.name)}</span></span>
            ${badge}
          </button>
        `);
      }
      for (const script of data.scripts || []) {
        rows.push(`
          <button type="button" class="browser-item" data-pick-script="${escapeHtml(script.script_ref)}">
            <span class="browser-item-left">🐍 <span>${escapeHtml(script.name)}</span></span>
            <span class="muted">Select</span>
          </button>
        `);
      }

      listEl.innerHTML = rows.length
        ? rows.join("")
        : `<div class="browser-item muted">No folders or .py scripts here.</div>`;
    } catch (err) {
      listEl.innerHTML = `<div class="browser-item" style="color: var(--failed)">${escapeHtml(err.message)}</div>`;
    }
  }

  async function pickScript(scriptRef) {
    $("field-script-ref").value = scriptRef;
    const txt = $("selected-script-text");
    txt.textContent = scriptRef;
    txt.classList.add("has-value");
    $("file-browser").classList.add("hidden");

    try {
      const info = await api(`api/inspect?script_ref=${encodeURIComponent(scriptRef)}`);
      if (!$("field-name").value.trim() && info.suggested_name) {
        $("field-name").value = info.suggested_name;
      }
      if (!$("field-description").value.trim() && info.suggested_description) {
        $("field-description").value = info.suggested_description;
      }
      if (!$("field-freshness").value.trim() && info.suggested_freshness_path) {
        $("field-freshness").value = info.suggested_freshness_path;
      }
      if (info.readme_found) {
        $("readme-hint-text").textContent = "Auto-filled title & note from README.md";
        $("readme-hint").classList.remove("hidden");
      } else {
        $("readme-hint").classList.add("hidden");
      }
    } catch {
      $("readme-hint").classList.add("hidden");
    }
  }

  // ==================== JOB DRAWER ====================
  function closeDrawers() {
    $("drawer-backdrop").classList.add("hidden");
    $("job-drawer").classList.add("hidden");
    $("logs-drawer").classList.add("hidden");
    closeEventStream();
  }

  function openJobDrawer(job = null) {
    closeEventStream();
    $("logs-drawer").classList.add("hidden");
    $("drawer-backdrop").classList.remove("hidden");
    $("job-drawer").classList.remove("hidden");
    $("job-form-error").classList.add("hidden");
    $("readme-hint").classList.add("hidden");
    state.deleteArmed = false;

    const delBtn = $("btn-delete-job");
    delBtn.textContent = "Delete";

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
      delBtn.classList.remove("hidden");

      state.cadenceType = job.schedule_type || "manual";
      const cfg = job.schedule_config || {};
      $("field-time").value = cfg.time || "07:00";
      state.weeklyDays = new Set(Array.isArray(cfg.days) && cfg.days.length ? cfg.days : [0, 2, 4]);
      $("field-monthly-mode").value = cfg.day_mode || "first_business_day";
      $("field-monthly-day").value = cfg.day || 1;
      $("field-every-hours").value = String(cfg.every_hours || 2);
      $("field-start-hour").value = String(cfg.start_hour ?? 7);
      $("field-end-hour").value = String(cfg.end_hour ?? 18);
      $("field-interval-weekdays").checked = cfg.weekdays_only !== false;
      $("more-options").open = Boolean(job.dashboard_url || job.args || !job.enabled);
    } else {
      state.editingJobId = null;
      $("job-drawer-title").textContent = "Add Script";
      $("job-drawer-sub").textContent = "Pick a Python script from the share and set its schedule.";
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
      $("field-interval-weekdays").checked = true;
      $("more-options").open = false;

      $("file-browser").classList.remove("hidden");
      loadBrowser(roots[0]?.id, "");
    }

    updateCadenceUI();
  }

  async function saveJobForm(e) {
    e.preventDefault();
    const errEl = $("job-form-error");
    errEl.classList.add("hidden");

    const payload = {
      name: $("field-name").value.trim(),
      description: $("field-description").value.trim(),
      script_ref: $("field-script-ref").value.trim(),
      schedule_type: state.cadenceType,
      schedule_config: collectScheduleConfig(),
      dashboard_url: $("field-dashboard-url").value.trim(),
      owner: $("field-owner").value.trim(),
      timeout_minutes: Number($("field-timeout").value) || 45,
      args: $("field-args").value.trim(),
      freshness_path: $("field-freshness").value.trim(),
      enabled: $("field-enabled").checked,
    };

    if (!payload.script_ref) {
      errEl.textContent = "Please choose a Python script from the folder browser above.";
      errEl.classList.remove("hidden");
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

  async function toggleJobEnabled(jobId) {
    const job = state.jobs.find((j) => j.id === jobId);
    if (!job) return;
    try {
      await api(`api/jobs/${encodeURIComponent(jobId)}`, {
        method: "PUT",
        body: JSON.stringify({ enabled: !job.enabled }),
      });
      showToast(job.enabled ? `Paused ${job.name}` : `Resumed ${job.name}`);
      await refreshAll();
    } catch (err) {
      showToast(err.message);
    }
  }

  // ==================== LOGS & TELEMETRY DRAWER ====================
  function closeEventStream() {
    if (state.eventSource) {
      state.eventSource.close();
      state.eventSource = null;
    }
    $("log-live-badge").classList.add("hidden");
  }

  function updateLogLineCount() {
    const text = $("log-output").textContent || "";
    const count = text ? text.split("\n").length : 0;
    $("log-line-count").textContent = `${count} line${count === 1 ? "" : "s"}`;
  }

  async function openLogsDrawer(jobId, preferredRunId = null) {
    closeEventStream();
    $("job-drawer").classList.add("hidden");
    $("drawer-backdrop").classList.remove("hidden");
    $("logs-drawer").classList.remove("hidden");
    state.logsJobId = jobId;

    const job = state.jobs.find((j) => j.id === jobId);
    $("logs-drawer-title").textContent = job ? job.name : "Run Telemetry";
    $("logs-drawer-script").textContent = job ? job.script_ref : "";

    try {
      const data = await api(`api/jobs/${encodeURIComponent(jobId)}/runs`);
      state.logsRuns = data.runs || [];
      renderHistoryStrip();

      const targetId = preferredRunId || state.logsRuns[0]?.id || null;
      if (targetId) {
        await selectRun(targetId);
      } else {
        state.selectedRunId = null;
        state.selectedAiPrompt = "";
        $("run-status-pill").className = "status-pill status-never";
        $("run-status-pill").textContent = "No runs yet";
        $("run-meta-text").textContent = 'Click "Run now" to test this script.';
        $("run-error-callout").classList.add("hidden");
        $("btn-copy-ai").classList.add("hidden");
        $("btn-logs-stop").classList.add("hidden");
        $("btn-logs-run-now").classList.remove("hidden");
        $("log-output").textContent = "No run logs yet for this script.";
        updateLogLineCount();
      }
    } catch (err) {
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
        return `<button class="history-bar bar-${escapeHtml(r.status)} ${sel}" style="height:${pct}%" data-select-run="${escapeHtml(r.id)}" title="${escapeHtml(tip)}"></button>`;
      })
      .join("");
  }

  async function selectRun(runId) {
    closeEventStream();
    state.selectedRunId = runId;
    renderHistoryStrip();

    try {
      const data = await api(`api/runs/${encodeURIComponent(runId)}`);
      const run = data.run;
      state.selectedAiPrompt = data.ai_prompt || "";

      const pill = $("run-status-pill");
      const isActive = run.status === "running" || run.status === "queued";
      const isFailed = run.status === "failed" || run.status === "timed_out";

      if (isActive) {
        pill.className = "status-pill status-running";
        pill.innerHTML = `<span class="pulse-dot"></span>${run.status === "queued" ? "Queued" : "Running"}`;
      } else if (run.status === "succeeded") {
        pill.className = "status-pill status-healthy";
        pill.textContent = "Succeeded";
      } else if (isFailed) {
        pill.className = "status-pill status-failed";
        pill.textContent = run.status === "timed_out" ? "Timed out" : "Failed";
      } else {
        pill.className = "status-pill status-paused";
        pill.textContent = "Stopped";
      }

      const bits = [];
      if (run.triggered_by) bits.push(`by ${run.triggered_by}`);
      bits.push(formatTimestamp(run.started_at || run.queued_at));
      if (run.duration_sec != null) bits.push(`took ${formatDuration(run.duration_sec)}`);
      if (run.exit_code !== null && run.exit_code !== undefined) bits.push(`exit ${run.exit_code}`);
      $("run-meta-text").textContent = bits.join(" · ");

      $("btn-logs-stop").classList.toggle("hidden", !isActive);
      $("btn-logs-run-now").classList.toggle("hidden", isActive);
      $("btn-copy-ai").classList.toggle("hidden", !isFailed);

      const errBox = $("run-error-callout");
      if (run.error_summary) {
        errBox.textContent = run.error_summary;
        errBox.classList.remove("hidden");
      } else {
        errBox.classList.add("hidden");
      }

      const logEl = $("log-output");
      logEl.textContent = data.log || (isActive ? "Waiting for output..." : "(No console output)");
      updateLogLineCount();
      if ($("log-autoscroll").checked) logEl.scrollTop = logEl.scrollHeight;

      if (isActive) {
        startLiveStream(runId);
      }
    } catch (err) {
      showToast(err.message);
    }
  }

  function startLiveStream(runId) {
    closeEventStream();
    $("log-live-badge").classList.remove("hidden");
    const es = new EventSource(`api/runs/${encodeURIComponent(runId)}/stream`);
    state.eventSource = es;
    const logEl = $("log-output");

    es.onmessage = async (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "snapshot") {
          logEl.textContent = msg.log || "";
        } else if (msg.type === "line") {
          logEl.textContent += msg.text;
        } else if (msg.type === "done") {
          closeEventStream();
          await refreshAll();
          if (state.logsJobId) {
            const runsData = await api(`api/jobs/${encodeURIComponent(state.logsJobId)}/runs`);
            state.logsRuns = runsData.runs || [];
            await selectRun(runId);
          }
          return;
        }
        updateLogLineCount();
        if ($("log-autoscroll").checked) logEl.scrollTop = logEl.scrollHeight;
      } catch {
        // Ignore malformed SSE chunks
      }
    };

    es.onerror = () => {
      closeEventStream();
    };
  }

  async function triggerRunNow(jobId, openDrawerAfter = true) {
    try {
      const data = await api(`api/jobs/${encodeURIComponent(jobId)}/run`, { method: "POST" });
      showToast("Script started");
      await refreshAll();
      if (openDrawerAfter) {
        await openLogsDrawer(jobId, data.run?.id);
      }
    } catch (err) {
      showToast(err.message);
    }
  }

  async function stopRun(runId) {
    try {
      await api(`api/runs/${encodeURIComponent(runId)}/stop`, { method: "POST" });
      showToast("Stopping run...");
      setTimeout(() => {
        refreshAll();
        if (state.logsJobId && state.selectedRunId === runId) {
          openLogsDrawer(state.logsJobId, runId);
        }
      }, 450);
    } catch (err) {
      showToast(err.message);
    }
  }

  async function quickCopyAiError(runId) {
    try {
      const data = await api(`api/runs/${encodeURIComponent(runId)}`);
      if (data.ai_prompt) {
        await copyText(data.ai_prompt, "Copied scrubbed error context for AI agent");
      }
    } catch (err) {
      showToast(err.message);
    }
  }

  async function copyText(text, successMsg) {
    try {
      await navigator.clipboard.writeText(text);
      showToast(successMsg);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
      showToast(successMsg);
    }
  }

  function switchView(viewName) {
    state.view = viewName;
    document.querySelectorAll(".nav-tab").forEach((t) => t.classList.toggle("active", t.dataset.view === viewName));
    $("view-scripts").classList.toggle("hidden", viewName !== "scripts");
    $("view-schedules").classList.toggle("hidden", viewName !== "schedules");
    $("view-results").classList.toggle("hidden", viewName !== "results");
    if (viewName === "schedules") renderSchedulesView();
    if (viewName === "results") renderResultsView();
  }

  // ==================== EVENTS ====================
  function bindEvents() {
    document.querySelectorAll(".nav-tab").forEach((tab) => {
      tab.addEventListener("click", () => switchView(tab.dataset.view));
    });

    // Scripts filters
    document.querySelectorAll("[data-filter]").forEach((pill) => {
      pill.addEventListener("click", () => {
        state.filter = pill.dataset.filter;
        document.querySelectorAll("[data-filter]").forEach((p) => p.classList.toggle("active", p === pill));
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
        document.querySelectorAll("[data-res-filter]").forEach((p) => p.classList.toggle("active", p === pill));
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

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        closeDrawers();
      } else if (e.key === "/" && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) {
        e.preventDefault();
        if (state.view === "results") $("results-search-input").focus();
        else {
          switchView("scripts");
          $("search-input").focus();
        }
      }
    });

    // Delegated clicks — Scripts tab
    $("jobs-list").addEventListener("click", (e) => {
      const target = e.target.closest("[data-run-job],[data-stop-run],[data-open-run],[data-open-logs],[data-edit-job],[data-quick-ai]");
      if (!target) return;
      if (target.dataset.runJob) triggerRunNow(target.dataset.runJob, true);
      else if (target.dataset.stopRun) stopRun(target.dataset.stopRun);
      else if (target.dataset.openRun) openLogsDrawer(target.dataset.jobId, target.dataset.openRun);
      else if (target.dataset.quickAi) quickCopyAiError(target.dataset.quickAi);
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
        toggleJobEnabled(toggleBtn.dataset.toggleEnabled);
        return;
      }
      const editBtn = e.target.closest("[data-edit-job]");
      if (editBtn) {
        const job = state.jobs.find((j) => j.id === editBtn.dataset.editJob);
        if (job) openJobDrawer(job);
      }
    });

    // Delegated clicks — Results tab
    $("results-list").addEventListener("click", (e) => {
      const aiBtn = e.target.closest("[data-quick-ai]");
      if (aiBtn) {
        e.stopPropagation();
        quickCopyAiError(aiBtn.dataset.quickAi);
        return;
      }
      const stopBtn = e.target.closest("[data-stop-run]");
      if (stopBtn) {
        e.stopPropagation();
        stopRun(stopBtn.dataset.stopRun);
        return;
      }
      const row = e.target.closest("[data-open-run]");
      if (row) {
        openLogsDrawer(row.dataset.jobId, row.dataset.openRun);
      }
    });

    // File browser
    const toggleBrowser = () => {
      const box = $("file-browser");
      const opening = box.classList.contains("hidden");
      box.classList.toggle("hidden");
      if (opening) loadBrowser($("browser-root-select").value || state.me?.roots?.[0]?.id, "");
    };
    $("btn-toggle-browser").addEventListener("click", toggleBrowser);
    $("btn-browse-inline").addEventListener("click", toggleBrowser);

    $("browser-root-select").addEventListener("change", (e) => loadBrowser(e.target.value, ""));
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

    $("job-form").addEventListener("submit", saveJobForm);

    $("btn-delete-job").addEventListener("click", async () => {
      if (!state.editingJobId) return;
      const btn = $("btn-delete-job");
      if (!state.deleteArmed) {
        state.deleteArmed = true;
        btn.textContent = "Confirm delete";
        return;
      }
      try {
        await api(`api/jobs/${encodeURIComponent(state.editingJobId)}`, { method: "DELETE" });
        showToast("Script deleted");
        closeDrawers();
        await refreshAll();
      } catch (err) {
        $("job-form-error").textContent = err.message;
        $("job-form-error").classList.remove("hidden");
      }
    });

    $("logs-history-strip").addEventListener("click", (e) => {
      const bar = e.target.closest("[data-select-run]");
      if (bar) selectRun(bar.dataset.selectRun);
    });

    $("btn-logs-run-now").addEventListener("click", () => {
      if (state.logsJobId) triggerRunNow(state.logsJobId, true);
    });

    $("btn-logs-stop").addEventListener("click", () => {
      if (state.selectedRunId) stopRun(state.selectedRunId);
    });

    $("btn-copy-ai").addEventListener("click", () => {
      if (state.selectedAiPrompt) copyText(state.selectedAiPrompt, "Copied error context for AI agent");
    });

    $("btn-copy-log").addEventListener("click", () => {
      copyText($("log-output").textContent || "", "Copied log to clipboard");
    });
  }

  async function init() {
    populateHourSelects();
    bindEvents();

    try {
      state.me = await api("api/me");
      document.title = state.me.app_name || "Analytics Scheduler";
      $("app-title").textContent = state.me.app_name || "Analytics Scheduler";
      $("user-name").textContent = state.me.user || "Analyst";
      $("user-avatar").textContent = (state.me.user || "A").slice(0, 1).toUpperCase();
      updateGreeting();
    } catch (err) {
      showToast(err.message);
    }

    await refreshAll();
    setInterval(refreshAll, 12000);
  }

  init();
})();
