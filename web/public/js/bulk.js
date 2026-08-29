(function () {
  var $ = function (sel) { return document.querySelector(sel); };
  var $$ = function (sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); };

  var actionSel = $("#bulk-action");
  var idInput = $("#bulk-id");
  var idGroup = $("#bulk-id-group");
  var createFields = $("#bulk-create-fields");
  var preview = $("#bulk-preview");
  var selectAll = $("#bulk-select-all");

  var operations = [];

  function num(v, dflt) { var n = Number(v); return isNaN(n) ? dflt : n; }

  function render() {
    preview.textContent = JSON.stringify(operations, null, 2);
  }

  function toggleFields() {
    var action = actionSel ? actionSel.value : "create";
    if (action === "create") {
      idGroup.style.display = "none";
      createFields.style.display = "block";
    } else {
      idGroup.style.display = "block";
      createFields.style.display = "none";
    }
  }

  function opFromForm() {
    var action = actionSel.value;
    if (action === "delete") {
      if (!idInput.value.trim()) { alert("Inserisci un id per l'operazione delete"); return null; }
      return { action: "delete", id: idInput.value.trim() };
    }
    if (action === "update") {
      var op = { action: "update" };
      if (idInput.value.trim()) op.id = idInput.value.trim();
      ["profile", "modello", "endpoint", "data", "key"].forEach(function (f) {
        var v = $("#bulk-" + f) ? $("#bulk-" + f).value : "";
        if (v.trim()) op[f] = v.trim();
      });
      var ctx = $("#bulk-context").value;
      if (ctx !== "") op.context = num(ctx, undefined);
      var prio = $("#bulk-priority").value;
      if (prio !== "") op.priority = num(prio, 0);
      if (!op.id) { alert("Inserisci un id per l'operazione update"); return null; }
      if (!Object.keys(op).some(function (k) { return k !== "action" && k !== "id"; })) {
        alert("Inserisci almeno un campo da aggiornare"); return null;
      }
      return op;
    }
    var required = ["profile", "modello", "endpoint", "data", "key"];
    for (var i = 0; i < required.length; i++) {
      var el = $("#bulk-" + required[i]);
      if (!el || !el.value.trim()) { alert("Campo richiesto mancante per create: " + required[i]); return null; }
    }
    var ctx = $("#bulk-context").value;
    var op = {
      action: "create",
      profile: $("#bulk-profile").value.trim(),
      modello: $("#bulk-modello").value.trim(),
      endpoint: $("#bulk-endpoint").value.trim(),
      data: $("#bulk-data").value.trim(),
      key: $("#bulk-key").value,
      context: ctx === "" ? 64 : num(ctx, 64),
    };
    return op;
  }

  function addSelected() {
    var checked = $$(".bulk-dep:checked").map(function (c) { return c.value; });
    return checked;
  }

  if (actionSel) {
    actionSel.addEventListener("change", toggleFields);
    toggleFields();
  }

  if ($("#bulk-add")) {
    $("#bulk-add").addEventListener("click", function () {
      var action = actionSel.value;
      if (action === "update" || action === "delete") {
        var selected = addSelected();
        if (selected.length) {
          selected.forEach(function (idv) {
            var op = { action: action, id: idv };
            if (action === "update") {
              var prio = $("#bulk-priority").value;
              if (prio !== "") op.priority = num(prio, 0);
            }
            if (operations.length < 50) operations.push(op);
          });
        } else if (idInput.value.trim()) {
          var op = opFromForm();
          if (op && operations.length < 50) operations.push(op);
        } else {
          alert("Seleziona dei deployment oppure inserisci un id");
          return;
        }
      } else {
        var op = opFromForm();
        if (op && operations.length < 50) operations.push(op);
      }
      render();
      if (operations.length >= 50) alert("Raggiunto il limite di 50 operazioni");
    });
  }

  if (selectAll) {
    selectAll.addEventListener("change", function () {
      $$(".bulk-dep").forEach(function (c) { c.checked = selectAll.checked; });
    });
  }

  if ($("#bulk-clear")) {
    $("#bulk-clear").addEventListener("click", function () { operations = []; render(); });
  }

  if ($("#bulk-submit")) {
    $("#bulk-submit").addEventListener("click", async function () {
      if (!operations.length) { alert("Nessuna operazione in anteprima"); return; }
      if (!window.scw || !window.scw.confirm) return;
      if (!window.confirm("Eseguire " + operations.length + " operazioni?")) return;
      try {
        var resp = await window.scw.fetch("/deployments/bulk", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ operations: operations }),
        });
        if (resp.status === 400) {
          var body = await resp.json();
          var html = "Errore: ";
          if (body && body.results && body.results.length) {
            html += body.results.map(function (e) { return e && e.message ? e.message : JSON.stringify(e); }).join("; ");
          } else if (body && body.error && body.error.message) {
            html += body.error.message;
          } else {
            html += "richiesta non valida";
          }
          alert(html);
          return;
        }
        if (resp.redirected) { window.location.href = resp.url; return; }
        if (resp.ok) { window.location.href = "/deployments?flash=" + encodeURIComponent("Bulk eseguito: " + operations.length + " operazioni applicate") + "&flashType=success"; }
      } catch (err) {
        console.error("Bulk failed:", err);
        alert("Errore di rete durante il bulk");
      }
    });
  }
})();
