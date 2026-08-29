(function () {
  var filterInput = document.getElementById("errors-filter");
  var tbody = document.querySelector("#errors-table tbody");
  var statusEl = document.getElementById("errors-status");
  var current = "";
  var timer = null;

  function fmtTs(ts) {
    var d = new Date(ts);
    if (isNaN(d.getTime())) return ts || "";
    return d.toLocaleString("it-IT");
  }

  function statusOf(e) {
    if (e.status !== undefined && e.status !== null && e.status !== "") return e.status;
    return e.level ? e.level : "—";
  }

  function typeOf(e) {
    if (e.type !== undefined && e.type !== null && e.type !== "") return e.type;
    if (e.op) return e.op;
    return e.dep ? "deployment" : "—";
  }

  function msgOf(e) {
    return e.msg !== undefined ? e.msg : JSON.stringify(e);
  }

  function render(events, up) {
    if (up) {
      statusEl.textContent = "aggiornato " + new Date().toLocaleTimeString("it-IT");
    } else {
      statusEl.textContent = "gateway giù";
    }

    tbody.innerHTML = "";
    var filtered = events;
    if (current) {
      var q = current.toLowerCase();
      filtered = events.filter(function (e) {
        return statusOf(e).toLowerCase().indexOf(q) !== -1
          || typeOf(e).toLowerCase().indexOf(q) !== -1
          || String(msgOf(e)).toLowerCase().indexOf(q) !== -1;
      });
    }

    if (filtered.length === 0) {
      var tr0 = document.createElement("tr");
      tr0.innerHTML = '<td colspan="4">Nessun errore registrato.</td>';
      tbody.appendChild(tr0);
      return;
    }

    filtered.forEach(function (e) {
      var tr = document.createElement("tr");
      var t;
      t = document.createElement("td");
      t.textContent = fmtTs(e.ts);
      tr.appendChild(t);
      t = document.createElement("td");
      t.innerHTML = '<span class="badge">' + statusOf(e) + '</span>';
      tr.appendChild(t);
      t = document.createElement("td");
      t.textContent = typeOf(e);
      tr.appendChild(t);
      t = document.createElement("td");
      t.textContent = msgOf(e);
      tr.appendChild(t);
      tbody.appendChild(tr);
    });
  }

  function poll() {
    var params = "?tail=500";
    if (current) params += "&filter=" + encodeURIComponent(current);
    // FIX: Use window.scw.fetch for CSRF token and 401 handling
    var fetcher = (window.scw && window.scw.fetch) ? window.scw.fetch : fetch;
    fetcher("/api/errors/events" + params)
      .then(function (r) {
        if (r && r.status === 401) {
          // Session expired - redirect to login with return URL
          var returnTo = window.location.pathname + window.location.search;
          if (returnTo !== "/login") {
            window.location.href = "/login?return_to=" + encodeURIComponent(returnTo);
          } else {
            window.location.href = "/login";
          }
          return null;
        }
        return r ? r.json() : Promise.reject("no response");
      })
      .then(function (data) {
        if (!data) return;
        render(data.events || [], data.up);
      })
      .catch(function () {
        render([], false);
      });
  }

  if (filterInput) {
    filterInput.addEventListener("input", function () {
      current = filterInput.value.trim();
      clearTimeout(timer);
      timer = setTimeout(poll, 300);
    });
    current = filterInput.value.trim();
  }

  if (tbody) {
    poll();
    // FIX: Increased from 5s to 30s to reduce server load; error log monitoring doesn't need real-time updates
    setInterval(poll, 30000);
  }
})();
