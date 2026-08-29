(function () {
  "use strict";
  var form = document.getElementById("pgForm");
  var runBtn = document.getElementById("pgRun");
  var out = document.getElementById("pgOut");
  var meta = document.getElementById("pgMeta");
  var content = document.getElementById("pgContent");
  var traceBody = document.querySelector("#pgTrace tbody");
  var errBox = document.getElementById("pgErr");

  function fetcher(p, o) {
    if (window.scw && window.scw.fetch) return window.scw.fetch(p, o);
    // FIX: Include credentials in fallback fetch to ensure cookies are sent
    return fetch(p, { method: o.method, headers: { "content-type": "application/json" }, body: JSON.stringify(o.json), credentials: "same-origin" });
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    errBox.style.display = "none";
    runBtn.disabled = true;
    runBtn.textContent = "Esecuzione…";
    var body = {
      model: document.getElementById("pgModel").value.trim(),
      prompt: document.getElementById("pgPrompt").value.trim(),
    };
    fetcher("/playground/run", { method: "POST", json: body })
      .then(function (r) {
        // FIX: Check response status before calling r.json()
        if (!r.ok) return r.text().then(function (text) { return { status: r.status, j: { error: { message: text } } }; });
        return r.json().then(function (j) { return { status: r.status, j: j }; });
      })
      .then(function (res) {
        if (res.status !== 200) {
          errBox.textContent = (res.j.error && res.j.error.message) || ("Errore " + res.status);
          errBox.style.display = "block";
          return;
        }
        var j = res.j;
        out.style.display = "block";
        meta.textContent = j.model + " · " + j.attempts + " tentativi · " + j.fallbacks + " fallback" + (j.ok ? "" : " · FALLITO");
        meta.className = "badge " + (j.ok ? "badge-active" : "badge-disabled");
        content.textContent = j.content || (j.error ? "(" + j.error + ")" : "(nessun contenuto)");
        traceBody.innerHTML = "";
        (j.trace || []).forEach(function (t) {
          var tr = document.createElement("tr");
          tr.innerHTML =
            "<td>" + (t.step != null ? t.step : "") + "</td>" +
            "<td><code>" + (t.unique || "") + "</code></td>" +
            "<td>" + (t.group || "") + "</td>" +
            "<td>" + (t.profile || "") + "</td>" +
            "<td>" + (t.reason || "") + "</td>" +
            '<td><span class="badge ' + (t.verdict === "ok" || t.verdict === "riuscito" ? "badge-active" : "badge-standalone") + '">' + (t.verdict || "") + "</span></td>";
          traceBody.appendChild(tr);
        });
      })
      .catch(function (e2) {
        errBox.textContent = "Errore di rete: " + e2.message;
        errBox.style.display = "block";
      })
      .finally(function () {
        runBtn.disabled = false;
        runBtn.textContent = "Esegui";
      });
  });
})();
