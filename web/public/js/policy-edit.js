(function () {
  var table = document.getElementById("policy-edit-table");
  var flashEl = document.getElementById("policy-flash");
  if (!table) return;

  // FIX: Sanitize input to prevent XSS when policy fields are displayed
  // NB: le entita' sono costruite via \u0026 (codice di '&') per evitare
  // che un editor/tool decodifichi "&amp;" in "&".
  function htmlEscape(text) {
    var AMP = "\u0026";
    var map = {
      "\u0026": AMP + "amp;",
      "\u003c": AMP + "lt;",
      "\u003e": AMP + "gt;",
      "\u0022": AMP + "quot;",
      "\u0027": AMP + "#39;"
    };
    return String(text).replace(/[\u0026\u003c\u003e\u0022\u0027]/g, function (c) {
      return map[c];
    });
  }

  function showFlash(msg, type) {
    if (!flashEl) return;
    flashEl.textContent = msg || "";
    flashEl.style.display = msg ? "block" : "none";
    flashEl.className = "alert" + (type === "error" ? " alert-error" : " alert-success");
  }

  function valueFor(fieldEl) {
    var type = fieldEl.getAttribute("data-type");
    var raw = fieldEl.value;
    if (type === "bool") return raw === "true";
    if (type === "int" || type === "num") return raw === "" ? null : Number(raw);
    if (type === "list") return raw;
    if (type === "map") return raw;
    return raw;
  }

  function closeForm(row) {
    var form = row.querySelector(".scw-inline-form");
    if (form) form.remove();
    row.classList.remove("scw-editing");
  }

  function openForm(row) {
    if (row.classList.contains("scw-editing")) return;
    var field = row.getAttribute("data-field");
    var type = row.getAttribute("data-type");
    var viewEl = row.querySelector(".scw-value-view");
    if (!viewEl) return;

    var input = document.createElement("input");
    input.setAttribute("data-field", field);
    input.setAttribute("data-type", type);
    var current = "";
    if (row.querySelector("pre")) {
      current = row.querySelector("pre").textContent;
    } else {
      current = viewEl.textContent === "—" ? "" : viewEl.textContent;
      if (type === "list") current = viewEl.textContent === "—" ? "" : viewEl.textContent;
    }
    input.className = "scw-value";

    if (type === "bool") {
      input.type = "select-one";
      var sel = document.createElement("select");
      var optTrue = document.createElement("option");
      optTrue.value = "true"; optTrue.textContent = "si";
      var optFalse = document.createElement("option");
      optFalse.value = "false"; optFalse.textContent = "no";
      sel.appendChild(optTrue);
      sel.appendChild(optFalse);
      sel.value = (current === "si" || current === true) ? "true" : "false";
      sel.className = "scw-value";
      var realInput = sel;
    } else if (type === "int" || type === "num") {
      input.type = "number";
      input.step = type === "num" ? "any" : "1";
      input.value = current;
      var realInput = input;
    } else if (type === "map") {
      var ta = document.createElement("textarea");
      ta.className = "scw-value";
      ta.rows = 5;
      ta.value = current;
      realInput = ta;
    } else {
      input.type = "text";
      input.value = current;
      realInput = input;
    }

    var form = document.createElement("span");
    form.className = "scw-inline-form";
    form.appendChild(realInput);

    var save = document.createElement("button");
    save.type = "button";
    save.className = "btn";
    save.textContent = "Salva";
    save.addEventListener("click", function () {
      submit(field, type, realInput.value, row);
    });
    var cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "btn";
    cancel.textContent = "Annulla";
    cancel.addEventListener("click", function () {
      closeForm(row);
    });

    form.appendChild(save);
    form.appendChild(cancel);

    viewEl.innerHTML = "";
    viewEl.appendChild(form);
    row.classList.add("scw-editing");
  }

  function submit(field, type, raw, row) {
    var payload;
    if (type === "map") {
      try {
        payload = JSON.parse(raw);
      } catch (e) {
        showFlash("JSON della mappa non valido", "error");
        return;
      }
    } else if (type === "list") {
      payload = raw;
    } else if (type === "bool") {
      payload = raw === "true";
    } else if (type === "int" || type === "num") {
      payload = raw === "" ? null : Number(raw);
    } else {
      // FIX: Sanitize raw value to prevent XSS
      payload = htmlEscape(String(raw));
    }

    var body = JSON.stringify({ field: field, value: payload });

    window.scw.fetch("/policy/field", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body,
    })
      .then(function (resp) {
        // la route fa un redirect; seguilo per ricaricare la pagina e flash
        if (resp && resp.url) {
          window.location.href = resp.url;
          return;
        }
        window.location.reload();
      })
      .catch(function (err) {
        showFlash("Errore di rete: " + (err && err.message ? err.message : err), "error");
      });
  }

  table.addEventListener("click", function (e) {
    var btn = e.target.closest ? e.target.closest("button") : null;
    if (btn) return; // gestione pulsanti interna
    var row = e.target.closest ? e.target.closest("tr.scw-row") : null;
    if (row && !row.classList.contains("scw-editing")) {
      openForm(row);
    }
  });
})();
