(function () {
  const sel = document.getElementById("f-status");
  const table = document.getElementById("key-health-table");
  const rows = Array.prototype.slice.call(document.querySelectorAll(".kh-row"));

  function applyFilter() {
    const value = sel ? sel.value : "all";
    rows.forEach((row) => {
      const show = value === "all" || row.getAttribute("data-status") === value;
      row.style.display = show ? "" : "none";
    });
  }

  if (sel) {
    sel.addEventListener("change", applyFilter);
  }

  if (!table || !window.scw) return;

  document.querySelectorAll(".kh-unretire").forEach((btn) => {
    btn.addEventListener("click", async function () {
      const unique = btn.getAttribute("data-unique");
      if (!unique) return;
      if (!window.confirm("Riativare il deployment ritirato? L'operazione è distruttiva per lo stato retired.")) {
        return;
      }
      btn.disabled = true;
      btn.textContent = "…";
      try {
        const res = await window.scw.fetch("/key-health/" + encodeURIComponent(unique) + "/unretire", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        });
        let data = null;
        try { data = await res.json(); } catch (e) { data = null; }
        if (res.ok && data && data.ok) {
          window.location.href = "/key-health?flash=" + encodeURIComponent("deployment riattivato") + "&flashType=success";
          return;
        }
        window.location.href = "/key-health?flash=" + encodeURIComponent("Gateway: " + (data && data.error ? data.error : "errore")) + "&flashType=error";
      } catch (err) {
        window.location.href = "/key-health?flash=" + encodeURIComponent("errore di rete") + "&flashType=error";
      }
    });
  });
})();
