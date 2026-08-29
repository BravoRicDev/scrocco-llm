(function () {
  "use strict";

  var cfg = window.scwCharts || {};
  var days = Number(cfg.days) || 30;

  function fmtDate(ts) {
    var d = new Date(ts * 1000);
    if (Number.isNaN(d.getTime())) return String(ts);
    return d.toLocaleDateString("it-IT", { day: "2-digit", month: "2-digit" });
  }

  function toArr(v) {
    return Array.isArray(v) ? v : [];
  }

  function toNum(v) {
    var n = Number(v);
    return Number.isFinite(n) ? n : 0;
  }

  function elWidth(id) {
    var el = document.getElementById(id);
    var w = el ? el.clientWidth : 0;
    if (w < 320) w = Math.max(320, Math.min(800, document.body.clientWidth || 800));
    return w;
  }

  function baseOpts(id) {
    return {
      width: elWidth(id),
      height: 260,
      legend: { show: true },
      scales: { x: { time: false } },
      axes: [
        { stroke: "#666", grid: { stroke: "#eee" }, values: function (u, ticks) { return ticks.map(fmtDate); } },
        { stroke: "#666", grid: { stroke: "#eee" } }
      ],
      series: [{ label: "Data" }]
    };
  }

  function emptyState(id) {
    var el = document.getElementById(id);
    if (el) el.innerHTML = '<p>Nessun dato</p>';
  }

  function renderLine(id, label, xs, ys, color) {
    if (!xs.length || !window.uPlot) return emptyState(id);
    var opts = baseOpts(id);
    opts.series.push({ label: label, stroke: color, width: 2 });
    new uPlot(opts, [xs, ys], document.getElementById(id));
  }

  function renderBars(id, label, xs, ys, color) {
    if (!xs.length || !window.uPlot) return emptyState(id);
    var opts = baseOpts(id);
    opts.series.push({
      label: label,
      stroke: color,
      fill: color + "66",
      width: 2,
      paths: uPlot.paths.bars({ size: [0.6] })
    });
    new uPlot(opts, [xs, ys], document.getElementById(id));
  }

  function renderErr(id, label, xs, ys, color, threshold) {
    if (!xs.length || !window.uPlot) return emptyState(id);
    var opts = baseOpts(id);
    opts.scales.y = { min: 0 };
    opts.hooks = {
      draw: function (u) {
        var ctx = u.ctx;
        var x0 = u.axes[0]._pos;
        var x1 = u.axes[0]._end;
        var y = u.valToPos(threshold, "y");
        ctx.save();
        ctx.strokeStyle = color;
        ctx.setLineDash([6, 4]);
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.moveTo(x0, y);
        ctx.lineTo(x1, y);
        ctx.stroke();
        ctx.restore();
      }
    };
    opts.series.push({ label: label, stroke: color, width: 2 });
    new uPlot(opts, [xs, ys], document.getElementById(id));
  }

  function load() {
    window.scw.fetch("/api/charts/data?days=" + encodeURIComponent(days))
      .then(function (resp) {
        if (!resp) return null;
        return resp.json();
      })
      .then(function (data) {
        var s = (data && data.series) || {};
        var labels = toArr(s.labels);
        var xs = labels.map(function (l) {
          var t = Date.parse(l + "T00:00:00Z") / 1000;
          return Number.isFinite(t) ? t : 0;
        });

        renderLine("chart-p95", "P95 / avg (ms)", xs, toArr(s.p95).map(toNum), "#1c64f2");
        renderBars("chart-calls", "Chiamate", xs, toArr(s.calls).map(toNum), "#057a55");
        renderLine("chart-cost", "Costo (USD)", xs, toArr(s.cost).map(toNum), "#d97706");
        renderErr("chart-err", "Error rate %", xs, toArr(s.err_rate).map(toNum), "#dc2626", 20);

        var note = document.getElementById("chart-note");
        if (!note) return;
        if (data && data.p95_source === "avg") {
          note.textContent = "Nota: p95 non disponibile per giorno dal gateway; mostrato avg_dur_ms (media mobile).";
          note.style.display = "";
        } else if (data && data.up === false) {
          note.textContent = "Gateway non raggiungibile: dati non disponibili.";
          note.style.display = "";
        } else {
          note.style.display = "none";
        }
      })
      .catch(function () {
        ["chart-p95", "chart-calls", "chart-cost", "chart-err"].forEach(emptyState);
        var note = document.getElementById("chart-note");
        if (note) {
          note.textContent = "Gateway non raggiungibile: dati non disponibili.";
          note.style.display = "";
        }
      });
  }

  var select = document.getElementById("days");
  if (select) {
    select.addEventListener("change", function () {
      window.location.href = "/observability/charts?days=" + encodeURIComponent(select.value);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", load);
  } else {
    load();
  }
})();
