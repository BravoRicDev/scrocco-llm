(function () {
  var ta = document.getElementById("pr-raw");
  var validateEl = document.getElementById("pr-validate");
  var diffBtn = document.getElementById("pr-diff-btn");
  var saveBtn = document.getElementById("pr-save-btn");
  var diffCard = document.getElementById("pr-diff-card");
  var diffBody = document.getElementById("pr-diff-body");
  var diffSummary = document.getElementById("pr-diff-summary");
  var statusEl = document.getElementById("pr-status");

  if (!ta) return;

  var state = window.POLICY_RAW_STATE || { activeRaw: ta.value || "" };

  function esc(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function showValidate(msg) {
    if (!validateEl) return;
    validateEl.textContent = msg || "";
    validateEl.style.display = msg ? "block" : "none";
  }

  function showStatus(msg, type) {
    if (!statusEl) return;
    statusEl.textContent = msg || "";
    statusEl.style.display = msg ? "inline-block" : "none";
    statusEl.className = "badge" + (type === "error" ? " pr-badge-error" : " pr-badge-ok");
  }

  function yamlErrorLine(err) {
    if (!err || !err.message) return { line: null, column: null, message: String(err) };
    var m = /line\s+(\d+)(?:,\s*column\s+(\d+))?/i.exec(err.message);
    return {
      line: m ? parseInt(m[1], 10) : null,
      column: m && m[2] ? parseInt(m[2], 10) : null,
      message: err.message
        .replace(/^.*?at line \d+, column \d+:\s*/i, "")
        .replace(/^[^:]+:\s*/, ""),
    };
  }

  function validateRaw(raw) {
    if (!raw || !raw.trim()) {
      showValidate("YAML vuoto: inserisci il contenuto del file prima di salvarlo.");
      return { ok: false, line: null };
    }
    try {
      if (window.jsyaml) {
        window.jsyaml.load(raw, { json: true });
      }
      showValidate("");
      return { ok: true, line: null };
    } catch (err) {
      var info = yamlErrorLine(err);
      var where = info.line ? " alla riga " + info.line + (info.column ? ", colonna " + info.column : "") : "";
      showValidate("Errore di sintassi YAML" + where + ": " + esc(info.message));
      return { ok: false, line: info.line, message: info.message };
    }
  }

  function jsonBody(obj) {
    return {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(obj),
    };
  }

  function showFlash(type, msg) {
    window.location.href =
      "/policy-raw?flash=" + encodeURIComponent(msg) + "&flashType=" + type;
  }

  function renderDiff(data) {
    if (!diffBody || !diffCard) return;
    diffBody.innerHTML = "";
    var adds = 0;
    var dels = 0;
    data.lines.forEach(function (line) {
      var tr = document.createElement("tr");
      tr.className = line.type === "+" ? "pr-add" : line.type === "-" ? "pr-del" : "pr-ctx";
      if (line.type === "+") adds++;
      if (line.type === "-") dels++;

      var tdType = document.createElement("td");
      tdType.className = "pr-col-type";
      tdType.textContent = line.type;

      var tdText = document.createElement("td");
      var pre = document.createElement("pre");
      pre.className = "pr-line-text";
      pre.textContent = line.text;
      tdText.appendChild(pre);

      tr.appendChild(tdType);
      tr.appendChild(tdText);
      diffBody.appendChild(tr);
    });
    if (diffSummary) {
      diffSummary.textContent = adds === 0 && dels === 0
        ? "Nessuna modifica rispetto alla versione attiva."
        : "+" + adds + " righe aggiunte, " + dels + " righe rimosse.";
    }
    diffCard.style.display = "block";
  }

  if (diffBtn) {
    diffBtn.addEventListener("click", function () {
      var check = validateRaw(ta.value);
      if (!check.ok) {
        showStatus("correggi lo YAML per il diff", "error");
        return;
      }
      showStatus("…", "");
      window.scw.fetch("/api/policy-raw/diff", jsonBody({ raw: ta.value }))
        .then(function (resp) {
          if (!resp.ok) {
            return resp.json().catch(function () { return { error: { message: "diff fallito" } }; })
              .then(function (err) {
                showStatus((err && err.error && err.error.message) || "diff fallito", "error");
                throw new Error("diff failed");
              });
          }
          return resp.json();
        })
        .then(function (data) {
          if (data && data.lines) {
            renderDiff(data);
            showStatus("diff pronto", "");
          } else {
            showStatus("diff non valido", "error");
          }
        })
        .catch(function (err) {
          if (!(err && err.message === "diff failed")) {
            showStatus("Errore di rete", "error");
          }
        });
    });
  }

  if (saveBtn) {
    saveBtn.addEventListener("click", function () {
      var check = validateRaw(ta.value);
      if (!check.ok) {
        showStatus("correggi lo YAML prima di salvare", "error");
        if (ta.scrollIntoView) ta.scrollIntoView();
        return;
      }
      showStatus("salvataggio…", "");
      window.scw.fetch("/api/policy-raw/save", jsonBody({ raw: ta.value }))
        .then(function (resp) {
          if (!resp.ok) {
            return resp.json().catch(function () { return { error: { message: "salvataggio fallito" } }; })
              .then(function (err) {
                var msg = (err && err.error && err.error.message) || "salvataggio fallito";
                showStatus(msg, "error");
                showValidate(msg);
                throw new Error("save failed");
              });
          }
          return resp.json();
        })
        .then(function (data) {
          if (data && data.ok) {
            showFlash("success", "policy salvata e ricaricata");
          } else {
            showStatus("risposta non valida", "error");
            showValidate("risposta non valida dal salvataggio.");
          }
        })
        .catch(function (err) {
          if (!(err && err.message === "save failed")) {
            showStatus("Errore di rete", "error");
            showValidate("Errore di rete durante il salvataggio.");
          }
        });
    });
  }

  // FIX: Add debounce to YAML validation to prevent performance issues with large files
  var yamlValidateTimer = null;
  ta.addEventListener("input", function () {
    clearTimeout(yamlValidateTimer);
    yamlValidateTimer = setTimeout(function () {
      validateRaw(ta.value);
    }, 500);
  });

  // sincronizza lo stato attivo dall'endpoint di lettura (best-effort)
  window.scw.fetch("/api/policy-raw/data")
    .then(function (resp) {
      if (!resp || !resp.ok) return null;
      return resp.json();
    })
    .then(function (data) {
      if (data && typeof data.raw === "string") {
        state.activeRaw = data.raw;
        if (document.activeElement !== ta) ta.value = data.raw;
        if (data.path) {
          var pathEl = document.getElementById("pr-path");
          if (pathEl) pathEl.textContent = data.path;
        }
      }
    })
    .catch(function () { /* best-effort: usa il valore renderizzato dal server */ });
})();