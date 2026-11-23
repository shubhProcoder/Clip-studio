// Runs before the page is drawn: the saved theme, else the OS setting (no flash of the wrong theme).
// Also restores a collapsed sidebar.
(function () {
  var t = null, side = null;
  try { t = localStorage.getItem("sc_theme"); side = localStorage.getItem("sc_side"); } catch (e) { t = null; }
  if (t !== "light" && t !== "dark") {
    t = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  document.documentElement.setAttribute("data-theme", t);
  if (side === "collapsed") document.documentElement.setAttribute("data-side", "collapsed");
})();
