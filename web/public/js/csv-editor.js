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
  // Le celle chiave non modificate dall'utente emettono la sentinella: il server
  // le rimpiazza con i valori reali letti dal gateway (merge per indice).
  function tableToRaw() {
    var lines = [header.map(csvCell).join(",")];
    var trs = table.querySelectorAll("tbody tr");
    trs.forEach(function (tr) {
      var cells = Array.prototype.map.call(tr.querySelectorAll("td"), function (td) {
        var isMasked = td.getAttribute("data-masked") === "true";
        var orig = (td.getAttribute("data-original") || "").trim();
        var curr = td.textContent.trim();
        if (isMasked && curr === orig) {
          return csvCell("__UNCHANGED_SECRET__");
        }
        if (curr === "") {
          return "";
        }
        return csvCell(td.textContent);
      });
      lines.push(cells.join(","));
    });
    return lines.join("\r\n") + "\r\n";
  }

  function confirmFn() {
    return (window.scw && typeof window.scw.confirm === "function") ? window.scw.confirm : window.confirm;
  }

  btnView.addEventListener("click", function () {
    var mode = btnView.getAttribute("data-mode");
    if (mode === "table") {
      // passo a raw: sincronizzo dalla tabella, ma le modifiche in tabella non
      // sono riconciliabili con le chiavi protette in vista raw.
      if (!confirmFn()("Attenzione: la Vista Raw mostra il file sorgente originale del server e non può sincronizzare le modifiche parziali fatte in tabella senza rischiare la corruzione delle chiavi protette. Continuare e perdere le modifiche non salvate della tabella?")) return;
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
    var currentHash = window.__csvHash || "";
    fetcher("/api/csv-editor/save", { method: "POST", json: { raw: raw, hash: currentHash } })
      .then(function (r) { return r.json().then(function (j) { return { status: r.status, j: j }; }); })
      .then(function (res) {
        if (res.status === 200 && res.j.ok) {
          if (res.j.hash) window.__csvHash = res.j.hash;
          showBanner("Salvato. Righe: " + res.j.rows + (res.j.backup ? " · backup " + res.j.backup : ""), true);
        } else if (res.status === 409) {
          showBanner("Conflitto (409): il file CSV è stato modificato sul server. Ricaricare la pagina prima di salvare.", false);
        } else {
          showBanner("Errore: " + ((res.j.error && res.j.error.message) || "salvataggio fallito"), false);
        }
      })
      .catch(function (e) { showBanner("Errore di rete: " + e.message, false); })
      .finally(function () { btnSave.disabled = false; });
  });
})();
