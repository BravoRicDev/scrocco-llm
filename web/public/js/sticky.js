(function () {
  var filterInput = document.getElementById("sticky-filter");
  var tbody = document.querySelector("#sticky-table tbody");
  var countEl = document.getElementById("sticky-count");

  function update() {
    var q = filterInput ? filterInput.value.trim().toLowerCase() : "";
    var rows = tbody ? tbody.querySelectorAll("tr[data-group]") : [];
    var visible = 0;
    rows.forEach(function (tr) {
      var group = (tr.getAttribute("data-group") || "").toLowerCase();
      var show = q === "" || group.indexOf(q) !== -1;
      tr.style.display = show ? "" : "none";
      if (show) visible++;
    });
    if (countEl) countEl.textContent = visible + " attive";
  }

  function reload() {
    window.location.reload();
  }

  if (filterInput) {
    filterInput.addEventListener("input", update);
  }

  update();
  // FIX: Removed auto-reload every 30s - it was disruptive to UX and caused
  // unnecessary traffic. Users can manually refresh the page when needed.
  // The client-side filter still works without full page reloads.
})();
