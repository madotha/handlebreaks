// Theme toggle in the header: system → light → dark. The choice is kept in localStorage
// and applied before the first paint by the inline script in base.html.
(function () {
  const btn = document.querySelector("[data-theme-toggle]");
  if (!btn) return;
  const modes = ["auto", "light", "dark"];
  const labels = { auto: "Theme: follows the system", light: "Theme: light", dark: "Theme: dark" };
  const current = () => document.documentElement.dataset.theme || "auto";
  function show(mode) {
    btn.querySelectorAll("[data-theme-icon]").forEach((el) => { el.hidden = el.dataset.themeIcon !== mode; });
    btn.dataset.tip = labels[mode] + " (click to change)";
  }
  btn.addEventListener("click", () => {
    const next = modes[(modes.indexOf(current()) + 1) % modes.length];
    if (next === "auto") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = next;
    try { if (next === "auto") localStorage.removeItem("theme"); else localStorage.setItem("theme", next); } catch (e) { /* blocked */ }
    show(next);
  });
  show(current());
})();
