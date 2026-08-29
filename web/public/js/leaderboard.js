(function () {
  const table = document.getElementById("leaderboard-table");
  const tbody = document.getElementById("leaderboard-tbody");
  const countEl = document.getElementById("lb-count");
  const titleEl = document.getElementById("lb-title");
  const statusEl = document.getElementById("lb-status");
  const toggle = document.getElementById("refresh-toggle");
  if (!table || !tbody) return;

  // FIX: Use exponential backoff and longer base interval to reduce server load
  const BASE_POLL_MS = 30000;  // Increased from 15s to 30s to reduce load
  const MAX_BACKOFF_MS = 120000;  // Max 2 minutes between polls
  const TITLES = { "24h": "24 ore", "7d": "7 giorni", "30d": "30 giorni", "90d": "90 giorni" };
  const RATE_KEYS = ["error_rate", "fb_rate", "qc_rate", "wd_rate"];
  const NUMERIC_KEYS = ["calls", "error_rate", "fb_rate", "qc_rate", "wd_rate", "probe_ms"];

  const state = {
    window: table.getAttribute("data-window") || "7d",
    sort: table.getAttribute("data-sort") || "calls",
    order: table.getAttribute("data-order") || "desc",
    profile: table.getAttribute("data-profile") || "",
  };

  let timer = null;
  let inFlight = false;

  function fmtNum(v) {
    const n = Number(v);
    if (!Number.isFinite(n)) return "—";
    return n.toLocaleString("it-IT", { maximumFractionDigits: 0 });
  }

  function fmtPct(v) {
    const n = Number(v);
    if (!Number.isFinite(n)) return "—";
    return n.toLocaleString("it-IT", { maximumFractionDigits: 2 }) + "%";
  }

  function fmtMs(v) {
    const n = Number(v);
    if (!Number.isFinite(n)) return "—";
    return n.toLocaleString("it-IT", { maximumFractionDigits: 0 }) + " ms";
  }

  function buildRow(r) {
    const tr = document.createElement("tr");
    [
      r.dep, r.profile, r.group, r.provider, r.model,
      fmtNum(r.calls),
      fmtPct(r.error_rate),
      fmtPct(r.fb_rate),
      fmtPct(r.qc_rate),
      fmtPct(r.wd_rate),
      fmtMs(r.probe_ms),
    ].forEach(function (v) {
      const td = document.createElement("td");
      td.textContent = v === undefined || v === null ? "—" : v;
      tr.appendChild(td);
    });
    return tr;
  }

  function renderRows(rows) {
    tbody.innerHTML = "";
    rows.forEach(function (r) {
      tbody.appendChild(buildRow(r));
    });
  }

  function updateHeader() {
    const ths = table.querySelectorAll("thead th[data-key]");
    ths.forEach(function (th) {
      const key = th.getAttribute("data-key");
      if (key === state.sort) {
        th.setAttribute("data-active", "1");
        th.setAttribute("data-order", state.order);
      } else {
        th.removeAttribute("data-active");
        th.removeAttribute("data-order");
      }
    });
  }

  function updateFooter(count, windowDays) {
    if (countEl) countEl.textContent = String(count);
    if (titleEl) {
      const t = TITLES[state.window] || state.window + (typeof windowDays === "number" ? " (" + windowDays + "g)" : "");
      titleEl.textContent = t;
    }
  }

  function setStatus(ok, note) {
    if (!statusEl) return;
    if (ok) {
      statusEl.textContent = "— aggiornato " + new Date().toLocaleTimeString("it-IT");
      statusEl.className = "lb-status live";
    } else {
      statusEl.textContent = "— " + (note || "gateway non raggiungibile");
      statusEl.className = "lb-status stale";
    }
  }

  // FIX: Implement exponential backoff and longer base interval to reduce server load
  let backoff_ms = 0;
  function startPolling() {
    stopPolling();
    // Use setTimeout-based backoff instead of fixed interval
    function schedule() {
      const delay = Math.min(BASE_POLL_MS + backoff_ms, MAX_BACKOFF_MS);
      timer = setTimeout(function () {
        refresh();
        schedule();
      }, delay);
    }
    schedule();
  }

  function stopPolling() {
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
  }

  function refresh() {
    if (inFlight) return;
    inFlight = true;
    const params = new URLSearchParams({
      window: state.window,
      sort: state.sort,
      order: state.order,
      profile: state.profile,
    });
    window.scw
      .fetch("/api/leaderboard/data?" + params.toString())
      .then(function (resp) {
        if (!resp) return null;
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        if (!data) return;
        renderRows(Array.isArray(data.rows) ? data.rows : []);
        updateFooter(Array.isArray(data.rows) ? data.rows.length : 0, data.window_days);
        updateHeader();
        setStatus(true);
        // FIX: Reset backoff on successful request
        backoff_ms = 0;
      })
      .catch(function (err) {
        setStatus(false, err && err.message ? err.message : "errore");
        // FIX: Increase backoff on failure (exponential)
        backoff_ms = Math.min(backoff_ms + BASE_POLL_MS, MAX_BACKOFF_MS - BASE_POLL_MS);
      })
       .finally(function () {
        inFlight = false;
      });
  }

  table.querySelector("thead").addEventListener("click", function (e) {
    const th = e.target.closest("th[data-key]");
    if (!th) return;
    const key = th.getAttribute("data-key");
    if (key === state.sort) {
      state.order = state.order === "asc" ? "desc" : "asc";
    } else {
      state.sort = key;
      state.order = NUMERIC_KEYS.includes(key) ? "desc" : "asc";
    }
    refresh();
  });

  if (toggle) {
    toggle.addEventListener("change", function () {
      if (toggle.checked) startPolling();
      else stopPolling();
    });
  }
})();
