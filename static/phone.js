// Phone behaviour, loaded on every page:
// - The bottom tab bar slides away while scrolling down (like the browser's address bar) and
//   comes back on scrolling up or at the top or bottom of the page. html.nav-hidden moves the
//   bar and everything placed above it (--tabbar-off in style.css).
// - Installed app (no browser around it): pulling down at the top refreshes the page. A page can
//   set window.pullRefresh (async) to refresh in place; otherwise it reloads.
(function () {
  const phone = matchMedia("(max-width: 720px)");
  const root = document.documentElement;

  // Tab bar
  if (document.querySelector(".tabbar")) {
    const DIST = 24;                            // px in one direction before the bar changes
    let last = scrollY, run = 0, ticking = false;
    const set = (hide) => root.classList.toggle("nav-hidden", hide);
    const editing = () => document.activeElement && document.activeElement.matches("input, select, textarea, [contenteditable]");
    const update = () => {
      ticking = false;
      const y = scrollY, dy = y - last;
      last = y;
      if (!phone.matches) return set(false);
      if (editing() || document.querySelector("dialog[open]")) return;   // keyboard or dialog: leave it as it is
      if (y <= 8 || y >= root.scrollHeight - innerHeight - 8) { run = 0; return set(false); }
      run = (dy > 0) === (run > 0) ? run + dy : dy;
      if (run > DIST) set(true);
      else if (run < -DIST) set(false);
    };
    addEventListener("scroll", () => { if (!ticking) { ticking = true; requestAnimationFrame(update); } }, { passive: true });
    phone.addEventListener("change", () => set(false));
    addEventListener("pageshow", () => { last = scrollY; set(false); });
  }

  // Pull to refresh in the installed app
  const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  if (!standalone) return;
  const PULL = 80;                              // px to pull before releasing refreshes
  const ind = document.createElement("div");
  ind.className = "ptr";
  ind.setAttribute("aria-hidden", "true");
  ind.innerHTML = '<svg class="i"><use href="#i-refresh"/></svg>';
  document.body.append(ind);
  let start = null, pull = 0, busy = false;
  const show = (px) => { ind.style.setProperty("--pull", px); ind.classList.toggle("ready", px >= PULL); };
  const blocked = () => busy || document.querySelector("dialog[open]") || root.classList.contains("fp-lock");
  addEventListener("touchstart", (e) => {
    start = scrollY <= 0 && e.touches.length === 1 && !blocked() ? e.touches[0].clientY : null;
    pull = 0;
  }, { passive: true });
  addEventListener("touchmove", (e) => {
    if (start === null) return;
    if (scrollY > 0) { start = null; return show(0); }
    pull = Math.max(0, e.touches[0].clientY - start);
    show(Math.min(pull, PULL * 1.4));
  }, { passive: true });
  addEventListener("touchend", async () => {
    if (start === null) return;
    start = null;
    if (pull < PULL) return show(0);
    busy = true;
    ind.classList.add("busy");
    show(PULL);
    try {
      if (window.pullRefresh) await window.pullRefresh();
      else { location.reload(); return; }
    } catch (e) { /* the page shows its own errors */ }
    busy = false;
    ind.classList.remove("busy");
    show(0);
  });
})();
