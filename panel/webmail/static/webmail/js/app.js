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
