(function () {
  "use strict";
  var card = document.getElementById("diffCard");
  var out = document.getElementById("diffOut");
  var idSpan = document.getElementById("diffId");

  function fetcher(p) {
    if (window.scw && window.scw.fetch) return window.scw.fetch(p, { method: "GET" });
    return fetch(p);
  }

  document.querySelectorAll("[data-diff]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var id = btn.getAttribute("data-diff");
      idSpan.textContent = "#" + id;
      out.textContent = "Carico…";
      card.style.display = "block";
      fetcher("/config-history/" + id + "/diff")
        .then(function (r) { return r.json(); })
        .then(function (j) {
          if (j.error) { out.textContent = "Errore: " + j.error.message; return; }
          out.innerHTML = "";
          (j.lines || []).forEach(function (l) {
            var span = document.createElement("span");
            span.textContent = (l.type === " " ? "  " : l.type + " ") + l.text;
            span.style.display = "block";
            if (l.type === "+") span.style.background = "rgba(34,197,94,.15)";
            if (l.type === "-") span.style.background = "rgba(239,68,68,.15)";
            out.appendChild(span);
          });
          if (!j.lines || !j.lines.length) out.textContent = "(nessuna differenza)";
        })
        .catch(function (e) { out.textContent = "Errore di rete: " + e.message; });
    });
  });
})();
