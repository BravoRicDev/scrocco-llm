(function () {
  const table = document.getElementById("live");
  if (!table) return;
  const tbody = table.tBodies[0];

  const statusEl = document.getElementById("live-status");
  const pauseBtn = document.getElementById("live-pause");
  const banner = document.getElementById("live-banner");
  const tagSel = document.getElementById("live-tag");
  const sinceInput = document.getElementById("live-since");

  let paused = false;
  let lastTs = "";

  function maxRowTs() {
    let max = "";
    const rows = tbody.rows;
    for (let i = 0; i < rows.length; i++) {
      const cell = rows[i].cells[0];
      if (!cell) continue;
      const v = cell.getAttribute("data-ts") || "";
      if (v > max) max = v;
    }
    return max;
  }

  function rowFor(e) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      "<td data-ts=\"" + (e.ts || "") + "\">" + esc(e.ts || "—") + "</td>" +
      "<td>" + esc(e.group || "—") + "</td>" +
      "<td><code>" + esc(e.dep || "—") + "</code></td>" +
      "<td>" + esc(e.model || "—") + "</td>" +
      "<td>" + (e.tries ?? "—") + "</td>" +
      "<td>" + (e.fb ?? 0) + "</td>" +
      "<td>" + (e.ms ?? "—") + "</td>" +
      "<td>" + statusBadge(e.status) + "</td>";
    return tr;
  }

  function statusBadge(st) {
    if (st === undefined || st === null) return "—";
    const ok = Number(st) < 400;
    return '<span class="badge ' + (ok ? "badge-active" : "badge-disabled") + '">' + st + "</span>";
  }

  function esc(v) {
    return String(v)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function escapeAttr(v) {
    return esc(v);
  }

  function applyTagFilter(eventEl) {
    const tag = tagSel.value;
    if (tag === "summary") return true;
    if (tag === "route") return eventEl.route !== undefined && eventEl.route !== null;
    if (tag === "identity") return eventEl.identity !== undefined && eventEl.identity !== null;
    if (tag === "fallback") return eventEl.fallback === true || eventEl.fb > 0;
    return true;
  }

  function prepend(events) {
    if (!Array.isArray(events)) return;
    const sinceVal = sinceInput.value.trim();
    events.forEach(function (e) {
      if (sinceVal) {
        const ets = e.ts || "";
        if (ets && ets <= sinceVal) return;
      }
      if (!applyTagFilter(e)) return;
      if ((e.ts || "") > lastTs) lastTs = e.ts;
      tbody.insertBefore(rowFor(e), tbody.firstChild);
    });
    while (tbody.rows.length > 500) {
      tbody.removeChild(tbody.lastChild);
    }
  }

  async function poll() {
    if (paused) return;
    try {
      const res = await window.scw.fetch("/api/live/events?since=" + encodeURIComponent(lastTs));
      if (res === null) return;
      const data = await res.json();
      if (data && data.up === false) {
        banner.style.display = "block";
      } else {
        banner.style.display = "none";
        prepend(data ? data.events : []);
      }
    } catch (e) {
      banner.style.display = "block";
    }
  }

  function setPaused(p) {
    paused = p;
    if (statusEl) {
      statusEl.textContent = p ? "pausa" : "live";
      statusEl.className = p ? "badge badge-disabled" : "badge badge-active";
    }
    if (pauseBtn) {
      pauseBtn.textContent = p ? "Riprendi" : "Pausa";
      pauseBtn.className = p ? "btn btn-primary" : "btn btn-danger";
    }
  }

  lastTs = maxRowTs();
  setPaused(false);

  if (pauseBtn) {
    pauseBtn.addEventListener("click", function () {
      setPaused(!paused);
    });
  }
  const reloadBtn = document.getElementById("live-reload");
  if (reloadBtn) {
    reloadBtn.addEventListener("click", function () {
      lastTs = "";
      tbody.innerHTML = "";
      poll();
    });
  }

  setInterval(poll, 2000);
  poll();
})();
