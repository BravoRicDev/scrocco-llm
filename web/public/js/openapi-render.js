(function () {
  "use strict";
  var root = document.getElementById("root");
  fetch("/api/v1/openapi.json")
    .then(function (r) { return r.json(); })
    .then(function (spec) {
      document.getElementById("ver").textContent = "v" + (spec.info && spec.info.version || "");
      var byTag = {};
      Object.keys(spec.paths || {}).forEach(function (p) {
        Object.keys(spec.paths[p]).forEach(function (method) {
          var op = spec.paths[p][method];
          var tag = (op.tags && op.tags[0]) || "api";
          (byTag[tag] = byTag[tag] || []).push({ method: method, path: p, op: op });
        });
      });
      root.innerHTML = "";
      Object.keys(byTag).sort().forEach(function (tag) {
        var h = document.createElement("h2");
        h.className = "tag-h";
        h.textContent = tag;
        root.appendChild(h);
        byTag[tag].sort(function (a, b) { return (a.path + a.method).localeCompare(b.path + b.method); })
          .forEach(function (e) {
            var div = document.createElement("div");
            div.className = "op";
            div.innerHTML = '<span class="m m-' + e.method + '">' + e.method.toUpperCase() + "</span> " +
              "<code>" + e.path + "</code> — " + (e.op.summary || "");
            if (e.method === "get") {
              var form = document.createElement("form");
              form.style.marginTop = "6px";
              form.innerHTML = '<input placeholder="Bearer token (agtok_…)" style="width:280px"> ' +
                '<button type="submit">Try</button> <pre style="white-space:pre-wrap"></pre>';
              form.addEventListener("submit", function (ev) {
                ev.preventDefault();
                var tok = form.querySelector("input").value.trim();
                var out = form.querySelector("pre");
                out.textContent = "…";
                fetch(e.path.replace(/\{[^}]+\}/g, "1"), { headers: tok ? { authorization: "Bearer " + tok } : {} })
                  .then(function (r) { return r.text().then(function (t) { out.textContent = r.status + "\n" + t.slice(0, 4000); }); })
                  .catch(function (err) { out.textContent = "errore: " + err.message; });
              });
              div.appendChild(form);
            } else {
              var pre = document.createElement("pre");
              pre.textContent = e.method.toUpperCase() + " " + e.path + "  (body JSON)";
              pre.style.marginTop = "4px";
              div.appendChild(pre);
            }
            root.appendChild(div);
          });
      });
    })
    .catch(function (err) { root.textContent = "Errore nel caricamento dello spec: " + err.message; });
})();
