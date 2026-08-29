(function () {
  "use strict";
  var btnView = document.getElementById("btnView");
  var btnSave = document.getElementById("btnSave");
  var tableWrap = document.getElementById("tableWrap");
  var rawArea = document.getElementById("rawArea");
  var banner = document.getElementById("csvBanner");
  var table = document.getElementById("csvTable");
  var header = window.__csvHeader || [];

  function showBanner(msg, ok) {
    banner.textContent = msg;
    banner.className = "alert " + (ok ? "success" : "error");
    banner.style.display = "block";
  }

  function csvCell(v) {
    var s = String(v == null ? "" : v);
    if (/[",\n\r]/.test(s)) return '"' + s.replace(/"/g, '""') + '"';
    return s;
  }

  // Costruisce il CSV testuale dalla tabella contenteditable.
  function tableToRaw() {
    var lines = [header.map(csvCell).join(",")];
    var trs = table.querySelectorAll("tbody tr");
    trs.forEach(function (tr) {
      var cells = Array.prototype.map.call(tr.querySelectorAll("td"), function (td) {
        return csvCell(td.textContent);
      });
      lines.push(cells.join(","));
    });
    return lines.join("\r\n") + "\r\n";
  }

  btnView.addEventListener("click", function () {
    var mode = btnView.getAttribute("data-mode");
    if (mode === "table") {
      // passo a raw: sincronizzo dalla tabella
      rawArea.value = tableToRaw();
      tableWrap.style.display = "none";
      rawArea.style.display = "block";
      btnView.setAttribute("data-mode", "raw");
      btnView.textContent = "Vista: Raw";
    } else {
      tableWrap.style.display = "block";
      rawArea.style.display = "none";
      btnView.setAttribute("data-mode", "table");
      btnView.textContent = "Vista: Tabella";
    }
  });

  btnSave.addEventListener("click", function () {
    var raw = btnView.getAttribute("data-mode") === "raw" ? rawArea.value : tableToRaw();
    if (window.scw && typeof window.scw.confirm === "function" && !window.scw.confirm("Salvare il CSV? Il gateway fa un backup automatico prima.")) return;
    btnSave.disabled = true;
    var fetcher = (window.scw && window.scw.fetch) ? window.scw.fetch : function (p, o) {
      return fetch(p, Object.assign({ headers: { "content-type": "application/json" }, body: JSON.stringify(o.json), method: o.method }, {}));
    };
    fetcher("/api/csv-editor/save", { method: "POST", json: { raw: raw } })
      .then(function (r) { return r.json().then(function (j) { return { status: r.status, j: j }; }); })
      .then(function (res) {
        if (res.status === 200 && res.j.ok) {
          showBanner("Salvato. Righe: " + res.j.rows + (res.j.backup ? " · backup " + res.j.backup : ""), true);
        } else {
          showBanner("Errore: " + ((res.j.error && res.j.error.message) || "salvataggio fallito"), false);
        }
      })
      .catch(function (e) { showBanner("Errore di rete: " + e.message, false); })
      .finally(function () { btnSave.disabled = false; });
  });
})();
