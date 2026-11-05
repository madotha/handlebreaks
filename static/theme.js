// Theme button in the header: system → light → dark for this browser only. The choice is kept in
// localStorage (a stored "auto" also overrides the setting) and applied before the first paint by
// the inline script in _app_head.html; without one, the setting UI_THEME (html[data-theme-default])
// counts. Settings → Appearance can drop this browser's choice ("themechange" event).
(function () {
  const btn = document.querySelector("[data-theme-toggle]");
  const root = document.documentElement;
  const modes = ["auto", "light", "dark"];
  const labels = { auto: "follows the system", light: "light", dark: "dark" };
  const current = () => root.dataset.theme || "auto";
  function stored() { try { return localStorage.getItem("theme"); } catch (e) { return null; } }
  function show() {
    if (!btn) return;
    const mode = current();
    btn.querySelectorAll("[data-theme-icon]").forEach((el) => { el.hidden = el.dataset.themeIcon !== mode; });
    btn.dataset.tip = "Theme in this browser: " + labels[mode] + (stored() ? "" : " (the setting)") + ". Click to change";
  }
  window.showThemeToggle = show;
  if (!btn) return;
  btn.addEventListener("click", () => {
    const next = modes[(modes.indexOf(current()) + 1) % modes.length];
    if (next === "auto") delete root.dataset.theme;
    else root.dataset.theme = next;
    if (window.themeColor) window.themeColor(next);
    try { localStorage.setItem("theme", next); } catch (e) { /* blocked */ }
    show();
    window.dispatchEvent(new Event("themechange"));
  });
  show();
})();
