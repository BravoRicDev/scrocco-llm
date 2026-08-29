(function () {
  var flashEl = document.getElementById("scw-keys-flash");

  function showFlash(msg, type) {
    if (!flashEl) return;
    flashEl.textContent = msg || "";
    flashEl.style.display = msg ? "block" : "none";
    flashEl.className = "alert" + (type === "error" ? " alert-error" : " alert-success");
  }

  function payloadFor(form) {
    var kind = form.getAttribute("data-kind");
    if (kind === "aliases") {
      var table = document.getElementById(form.getAttribute("data-table"));
      var object = {};
      if (table) {
        table.querySelectorAll("tbody tr[data-alias]").forEach(function (row) {
          object[row.getAttribute("data-alias")] = row.getAttribute("data-modello") || "";
        });
      }
      var alias = form.querySelector('[name="alias"]').value.trim();
      var modello = form.querySelector('[name="modello"]').value.trim();
      if (!alias) return null;
      object[alias] = modello;
      return { kind: "aliases", object: object };
    }
    if (kind === "alias_key") {
      var akAlias = form.querySelector('[name="alias"]').value.trim();
      var akKey = form.querySelector('[name="key"]').value.trim();
      if (!akAlias || !akKey) return null;
      return { kind: "alias_key", alias: akAlias, key: akKey };
    }
    if (kind === "client_key") {
      var profile = form.querySelector('[name="profile"]').value.trim();
      var ckKey = form.querySelector('[name="key"]').value.trim();
      if (!profile || !ckKey) return null;
      return { kind: "client_key", profile: profile, key: ckKey };
    }
    return null;
  }

  function submit(path, payload) {
    return window.scw.fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(function (resp) {
      if (resp && resp.url) {
        window.location.href = resp.url;
        return;
      }
      window.location.reload();
    }).catch(function (err) {
      showFlash("Errore di rete: " + (err && err.message ? err.message : err), "error");
    });
  }

  document.querySelectorAll("form.scw-keys-form").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var payload = payloadFor(form);
      if (!payload) {
        showFlash("Compila tutti i campi richiesti", "error");
        return;
      }
      submit("/policy/keys", payload);
    });
  });

  document.querySelectorAll("button.scw-delete").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var kind = btn.getAttribute("data-kind");
      var name = btn.getAttribute("data-name");
      if (!window.confirm("Elimini " + kind + " \"" + name + "\"?")) return;
      submit("/policy/keys/delete", { kind: kind, name: name });
    });
  });
})();
