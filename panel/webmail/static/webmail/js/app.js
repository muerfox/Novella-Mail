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

function closeTransientUi() {
  var nav = document.getElementById("nav-toggle");
  var account = document.getElementById("account-toggle");
  if (nav) nav.checked = false;
  if (account) account.checked = false;
}

// Close the folder drawer when a folder is chosen (hx-boost keeps the page
// state otherwise, so the drawer would stay open over the inbox).
document.addEventListener("click", function (event) {
  var folderLink = event.target.closest && event.target.closest(".folder-link");
  if (folderLink) {
    var nav = document.getElementById("nav-toggle");
    if (nav) nav.checked = false;
    return;
  }
  // Tap outside the account menu closes it.
  var account = document.getElementById("account-toggle");
  if (
    account &&
    account.checked &&
    !event.target.closest(".topbar-user")
  ) {
    account.checked = false;
  }
});

document.addEventListener("keydown", function (event) {
  if (event.key === "Escape") closeTransientUi();
});

// After htmx swaps in a new page body, make sure drawers aren't left open.
document.addEventListener("htmx:afterSettle", closeTransientUi);
