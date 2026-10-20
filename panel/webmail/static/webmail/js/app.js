// One-shot forms (compose/send): block a second submit while the first is in
// flight. htmx's hx-disabled-elt covers boosted requests; this also covers a
// plain (non-htmx) submit and re-enables the form if htmx reports an error.
document.addEventListener(
  "submit",
  function (event) {
    var form = event.target;
    if (!form.matches || !form.matches("form[data-once-submit]")) return;
    if (form.classList.contains("is-submitting")) {
      event.preventDefault();
      return;
    }
    form.classList.add("is-submitting");
  },
  true
);
["htmx:responseError", "htmx:sendError", "htmx:timeout"].forEach(function (name) {
  document.addEventListener(name, function (event) {
    var form = event.target && event.target.closest && event.target.closest("form[data-once-submit]");
    if (form) form.classList.remove("is-submitting");
  });
});
// Don't let a "Sending…" state end up in htmx's back/forward page snapshot.
document.addEventListener("htmx:beforeHistorySave", function () {
  document.querySelectorAll("form.is-submitting").forEach(function (form) {
    form.classList.remove("is-submitting", "htmx-request");
    form.querySelectorAll("[disabled]").forEach(function (el) { el.removeAttribute("disabled"); });
  });
});

// Auto-size sandboxed HTML email iframes to their content.
// Delegated on document so it also catches iframes swapped in by htmx boost.
document.addEventListener(
  "load",
  function (event) {
    var frame = event.target;
    if (frame.tagName === "IFRAME" && frame.classList.contains("mail-frame")) {
      try {
        var doc = frame.contentDocument;
        frame.style.height = doc.documentElement.scrollHeight + 24 + "px";
      } catch (err) {
        // cross-origin or inaccessible; leave default height
      }
    }
  },
  true
);
