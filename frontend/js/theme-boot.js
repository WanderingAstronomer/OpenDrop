/* Resolve the theme BEFORE first paint (no flash) on the static legal pages.
   index.html inlines this same logic (CSP-hashed) so the map has zero extra requests;
   the legal pages load it as a same-origin script, which 'script-src self' already allows. */
(function () {
  try {
    var t = localStorage.getItem("opendrop_theme");
    if (t !== "light" && t !== "dark") {
      t = (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) ? "dark" : "light";
    }
    document.documentElement.setAttribute("data-theme", t);
  } catch (e) {
    document.documentElement.setAttribute("data-theme", "light");
  }
})();
