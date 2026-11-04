// Phones: the bottom tab bar slides away while scrolling down (like the browser's address bar) and
// comes back on scrolling up or at the top or bottom of the page. html.nav-hidden moves the bar and
// everything placed above it (--tabbar-off in style.css).
(function () {
  if (!document.querySelector(".tabbar")) return;
  const phone = matchMedia("(max-width: 720px)");
  const root = document.documentElement;
  const DIST = 24;                              // px in one direction before the bar changes
  let last = scrollY, run = 0, ticking = false;
  const set = (hide) => root.classList.toggle("nav-hidden", hide);
  const editing = () => document.activeElement && document.activeElement.matches("input, select, textarea, [contenteditable]");

  function update() {
    ticking = false;
    const y = scrollY, dy = y - last;
    last = y;
    if (!phone.matches) return set(false);
    if (editing() || document.querySelector("dialog[open]")) return;   // keyboard or dialog: leave it as it is
    if (y <= 8 || y >= root.scrollHeight - innerHeight - 8) { run = 0; return set(false); }
    run = (dy > 0) === (run > 0) ? run + dy : dy;
    if (run > DIST) set(true);
    else if (run < -DIST) set(false);
  }
  addEventListener("scroll", () => { if (!ticking) { ticking = true; requestAnimationFrame(update); } }, { passive: true });
  phone.addEventListener("change", () => set(false));
  addEventListener("pageshow", () => { last = scrollY; set(false); });
})();
