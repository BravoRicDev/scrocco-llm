(function () {
  const form = document.getElementById("probe-bulk-form");
  if (!form) return;

  const forceCheckbox = document.getElementById("bulk-force");
  const forceBtn = document.getElementById("probe-bulk-force");
  if (!forceBtn) return;

  forceBtn.addEventListener("click", function (event) {
    event.preventDefault();

    if (!window.confirm("Probe FORCE su tutti i deployment selezionati?")) {
      return;
    }
    if (!window.confirm("Ultima conferma: il force brucia quota sui free-tier. Continuare?")) {
      return;
    }

    if (forceCheckbox) {
      forceCheckbox.checked = true;
    }
    form.submit();
  });
})();
