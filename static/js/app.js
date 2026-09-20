(function () {
  "use strict";

  var toggle = document.querySelector("[data-nav-toggle]");
  var nav = document.querySelector("[data-nav]");
  if (toggle && nav) {
    toggle.addEventListener("click", function () {
      var open = nav.classList.toggle("is-open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  // Confirm destructive actions. The action itself is always authorized and
  // validated again on the server; this is only a courtesy prompt.
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      var text = form.getAttribute("data-confirm") || "هل أنت متأكد؟";
      if (!window.confirm(text)) {
        event.preventDefault();
      }
    });
  });
})();