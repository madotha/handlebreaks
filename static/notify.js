// Notifications: polls /events (webui.events()) and shows new ones as popups in the corner,
// each with a link to where it happened and a close button. They hide after NOTIFY_SECONDS
// (a bar at the bottom runs down until then; hovering the popups or focusing one holds them; errors may
// stay until closed). The last id seen is
// shared by all tabs in localStorage, so an event pops up once even with several pages open,
// and moving to another page doesn't lose what came in meanwhile.
(function () {
  const box = document.querySelector("[data-toasts]");
  if (!box) return;
  const url = box.dataset.eventsUrl;
  const KEY = "events:last", EVERY = 10000;
  const max = () => (matchMedia("(max-width: 720px)").matches ? 3 : 4);    // popups shown at once
  const ICONS = { success: "check", error: "alert", warn: "alert", info: "info" };
  let own = null;                                   // used when localStorage is blocked
  const seen = () => {
    try { const v = parseInt(localStorage.getItem(KEY), 10); return isNaN(v) ? own : v; } catch (e) { return own; }
  };
  const remember = (id) => { own = id; try { localStorage.setItem(KEY, String(id)); } catch (e) {} };

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
    t.classList.add("leaving");
    setTimeout(() => t.remove(), 200);
  }

  function show(ev, opts) {
    const t = el("div", `toast toast-${ev.level}`);
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

    const live = [...box.querySelectorAll(".toast:not(.leaving)")];
    live.slice(0, Math.max(0, live.length - max())).forEach(dismiss);   // the oldest make room

    const sticky = !opts.seconds || (ev.level === "error" && opts.errors_stay);
    if (sticky) return;
    // The time bar is the timer: its animation pauses while the popups are hovered or this one has focus
    // (CSS), and the popup hides when it ends, so the bar and the hiding always match.
    const bar = el("span", "toast-bar");
    bar.style.animationDuration = `${opts.seconds}s`;
    bar.addEventListener("animationend", () => dismiss(t));
    t.append(bar);
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
      fresh.forEach((e) => show(e, data));
    } catch (e) {
      // web UI restarting or unreachable: try again with the next poll
    } finally {
      busy = false;
    }
  }

  poll();
  setInterval(poll, EVERY);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
})();
