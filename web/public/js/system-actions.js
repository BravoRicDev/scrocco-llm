(function () {
  var MSG = "Sei sicuro di voler procedere?";

  function scwConfirm(msg) {
    return window.scw && typeof window.scw.confirm === "function"
      ? window.scw.confirm(msg)
      : window.confirm(msg);
  }

  function confirmText(form) {
    var m = form.getAttribute("data-confirm");
    return m ? m : MSG;
  }

  function attach() {
    var forms = document.querySelectorAll("form[data-confirm], form[data-destruct]");
    Array.prototype.forEach.call(forms, function (form) {
      if (form.dataset.scwSystemBound) return;
      form.dataset.scwSystemBound = "1";
      form.addEventListener("submit", function (e) {
        if (form.dataset.scwSkip) return;
        if (scwConfirm(confirmText(form))) {
          form.dataset.scwSkip = "1";
          form.submit();
          return;
        }
        e.preventDefault();
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", attach);
  } else {
    attach();
  }
})();