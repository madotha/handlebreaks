// Notifications: polls /events (webui.events()) and shows new ones as popups in the corner,
// each with a link to where it happened and a close button. They hide after NOTIFY_SECONDS
// (a bar at the bottom runs down until then; hovering the popups or focusing one holds them; errors may
// stay until closed). The last id seen is shared by all tabs in localStorage, so an event pops
// up once even with several pages open, and moving to another page doesn't lose what came in
// meanwhile. The popups on screen are kept per tab in sessionStorage with their time left, so
// the next page or a reload shows them again where they were.
(function () {
  const box = document.querySelector("[data-toasts]");
  if (!box) return;
  const url = box.dataset.eventsUrl;
  const KEY = "events:last", OPEN = "events:open", EVERY = 10000;
  const max = () => (matchMedia("(max-width: 720px)").matches ? 3 : 4);    // popups shown at once
  const ICONS = { success: "check", error: "alert", warn: "alert", info: "info" };
  let own = null;                                   // used when localStorage is blocked
  const seen = () => {
    try { const v = parseInt(localStorage.getItem(KEY), 10); return isNaN(v) ? own : v; } catch (e) { return own; }
  };
  const remember = (id) => { own = id; try { localStorage.setItem(KEY, String(id)); } catch (e) {} };

  // Popups on screen: element -> {ev, total (seconds, 0 = until closed)}
  const open = new Map();
  function timeLeft(t, total) {                     // ms left on the popup's time bar
    const bar = t.querySelector(".toast-bar");
    const anim = bar && bar.getAnimations ? bar.getAnimations()[0] : null;
    const done = anim ? anim.effect.getComputedTiming().progress : 0;   // includes a restored head start
    return total * 1000 * (1 - (done || 0));
  }
  function save() {
    const list = [...open].map(([t, o]) => ({ ev: o.ev, total: o.total, left: o.total ? timeLeft(t, o.total) : 0 }));
    try { sessionStorage.setItem(OPEN, JSON.stringify(list)); } catch (e) {}
  }
  function restore() {
    let list = [];
    try { list = JSON.parse(sessionStorage.getItem(OPEN) || "[]"); } catch (e) {}
    list.filter((o) => !o.total || o.left > 300).forEach((o) => show(o.ev, o.total, o.left, true));
  }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text) e.textContent = text;                 // file names are text, never HTML
    return e;
  }
  function nameEl(cls, text) {                      // break long names after . _ - (like the wbr filter)
    const e = el("span", cls);
    text.split(/(?<=[._-])(?=\S)/).forEach((part, i) => {
      if (i) e.append(document.createElement("wbr"));
      e.append(part);
    });
    return e;
  }
  function icon(name) {
    const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    s.setAttribute("class", "i");
    s.setAttribute("aria-hidden", "true");
    const u = document.createElementNS("http://www.w3.org/2000/svg", "use");
    u.setAttribute("href", `#i-${name}`);
    s.append(u);
    return s;
  }

  function dismiss(t) {
    if (t.classList.contains("leaving")) return;
    open.delete(t);
    save();
    t.classList.add("leaving");
    setTimeout(() => t.remove(), 200);
  }

  // total: seconds until it hides (0 = until closed); left: ms of that still to go (restored popups)
  function show(ev, total, left, restored) {
    const t = el("div", `toast toast-${ev.level}${restored ? " restored" : ""}`);
    const mark = el("span", "toast-icon");
    mark.append(icon(ICONS[ev.level] || "info"));
    const body = el("div", "toast-body");
    body.append(el("strong", "toast-title", ev.title));
    if (ev.where) body.append(el("span", "toast-where", ev.where));
    if (ev.file) body.append(nameEl("toast-file", ev.file));
    if (ev.text) body.append(nameEl("toast-text", ev.text));
    if (Date.now() - new Date(ev.at) > 120000 && window.formatLocalTime) {   // happened while no page was open
      body.append(el("span", "toast-time", window.formatLocalTime(ev.at)));
    }
    const actions = el("div", "toast-actions");
    const go = el("a", "icon-btn");
    go.href = ev.url;
    go.setAttribute("aria-label", "Show");
    go.dataset.tip = "Show";
    go.append(icon("open"));
    const close = el("button", "icon-btn");
    close.type = "button";
    close.setAttribute("aria-label", "Close");
    close.dataset.tip = "Close";
    close.append(icon("close"));
    close.addEventListener("click", () => dismiss(t));
    actions.append(go, close);
    t.append(mark, body, actions);
    box.append(t);
    open.set(t, { ev, total });

    if (total) {
      // The time bar is the timer: its animation pauses while the popups are hovered or this one has
      // focus (CSS), and the popup hides when it ends, so the bar and the hiding always match. A
      // restored popup starts the animation part-way through (negative delay).
      const bar = el("span", "toast-bar");
      bar.style.animationDuration = `${total}s`;
      if (left !== undefined) bar.style.animationDelay = `${-(total * 1000 - left)}ms`;
      bar.addEventListener("animationend", () => dismiss(t));
      t.append(bar);
    }
    const live = [...box.querySelectorAll(".toast:not(.leaving)")];
    live.slice(0, Math.max(0, live.length - max())).forEach(dismiss);   // the oldest make room
    save();
  }

  let busy = false;
  async function poll() {
    if (busy || document.hidden) return;
    busy = true;
    try {
      const after = seen();
      const r = await fetch(after === null ? url : `${url}?after=${after}`, { headers: { Accept: "application/json" } });
      if (!r.ok) return;
      const data = await r.json();
      const now = seen();                           // another tab may have shown some meanwhile
      if (after === null || now === null || now > data.last) {   // first visit or the database was reset: start now
        remember(data.last);
        return;
      }
      const fresh = data.events.filter((e) => e.id > now);
      remember(Math.max(now, data.last));
      fresh.forEach((e) => show(e, !data.seconds || (e.level === "error" && data.errors_stay) ? 0 : data.seconds));
    } catch (e) {
      // web UI restarting or unreachable: try again with the next poll
    } finally {
      busy = false;
    }
  }

  restore();
  poll();
  setInterval(poll, EVERY);
  // Save the time left when leaving the page (also when the tab is hidden: phones may not send pagehide)
  window.addEventListener("pagehide", save);
  document.addEventListener("visibilitychange", () => { if (document.hidden) save(); });
  window.addEventListener("pageshow", (e) => {      // back/forward cache: the popups may have changed meanwhile
    if (!e.persisted) return;
    open.clear();
    box.replaceChildren();
    restore();
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
})();
