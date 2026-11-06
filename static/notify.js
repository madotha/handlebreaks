// Notifications: polls /events (webui.events()) and shows new ones as popups in the corner,
// each with a link to where it happened and a close button. They hide after NOTIFY_SECONDS
// (a bar at the bottom runs down until then; hovering the popups or focusing one holds them; errors may
// stay until closed). The last id seen is shared by all tabs in localStorage, so an event pops
// up once even with several pages open, and moving to another page doesn't lose what came in
// meanwhile. The popups on screen are kept per tab in sessionStorage with their time left, so
// the next page or a reload shows them again where they were. When more come than fit, the
// oldest wait hidden behind a "+N more" chip (their time bar only starts once they show);
// the chip shows them all (and then closes all), and a waiting one moves up whenever one on
// screen goes.
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
  const more = document.createElement("button");    // "+N more": popups waiting for room
  more.type = "button";
  more.className = "toast-more";
  more.hidden = true;
  box.prepend(more);
  let expanded = false;                             // the chip was clicked: show all
  more.addEventListener("click", () => {
    if (expanded) [...open.keys()].forEach(dismiss);   // "Close all"
    else { expanded = true; layout(); }
  });
  function layout() {
    const live = [...box.querySelectorAll(".toast:not(.leaving)")];
    if (live.length <= max()) expanded = false;
    const waiting = expanded ? 0 : live.length - max();
    live.forEach((t, i) => { t.hidden = i < waiting; });   // the oldest wait; a hidden one's bar doesn't run
    box.classList.toggle("expanded", expanded);
    more.hidden = !expanded && waiting <= 0;
    more.textContent = expanded ? "Close all" : `+${waiting} more`;
    more.setAttribute("aria-label", expanded ? "Close all notifications" : `Show ${waiting} more notification${waiting === 1 ? "" : "s"}`);
  }
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
    layout();                                       // a waiting popup moves up
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
    layout();
    save();
  }

  // The page tabs: files awaiting review on Status, the encode's progress (a ring on phones),
  // a dot on Files while a scan runs
  function navState(nav) {
    document.querySelectorAll("[data-nav-badge]").forEach((b) => {
      b.hidden = !nav.review;
      b.textContent = nav.review > 99 ? "99+" : String(nav.review);
      b.title = `${nav.review.toLocaleString()} awaiting review`;
    });
    document.querySelectorAll('[data-nav="status"]').forEach((a) => {
      a.classList.toggle("encoding", nav.pct !== null);
      a.style.setProperty("--p", nav.pct === null ? 0 : nav.pct);
    });
    window.renderScanProgress(nav.scan || null, nav.scanning);
  }

  // The running scan's progress in every [data-scan-progress] (templates/_scan_progress.html).
  // Also called by static/status.js with its faster live updates. Once a scan seen here ends,
  // the box offers a reload, since the page shows the data from before the scan.
  // The page tabs and the header pill follow it too: the dot on Files (its tooltip: phase and
  // percentage), on phones a ring around the Files icon while the percentage is known.
  const seenScan = new WeakSet();
  window.renderScanProgress = function (scan, scanning, result) {
    scanning = scanning === undefined ? !!scan : scanning;
    const pct = scan && scan.pct !== null && scan.pct !== undefined ? scan.pct : null;
    document.querySelectorAll("[data-nav-dot]").forEach((d) => {
      d.hidden = !scanning;
      d.title = !scan ? "Scanning" : `${scan.label}: ${scan.phase}${pct === null ? "" : `, ${pct} %`}`;
    });
    document.querySelectorAll('[data-nav="files"]').forEach((a) => {
      a.classList.toggle("scanning", pct !== null);
      a.style.setProperty("--p", pct === null ? 0 : pct);
    });
    document.querySelectorAll("[data-scan-pill]").forEach((pill) => {
      pill.hidden = !scanning;
      pill.querySelector("[data-scan-pill-pct]").textContent = pct === null ? "" : ` ${pct} %`;
      if (scan) pill.dataset.tip = `${scan.label}: ${scan.phase}${scan.detail ? ` (${scan.detail})` : ""}`;
    });
    document.querySelectorAll("[data-scan-progress]").forEach((box) => {
      const q = (sel) => box.querySelector(sel);
      if (!box.dataset.init) {                         // a scan rendered by the server counts as seen
        box.dataset.init = "1";
        if (!box.hidden) seenScan.add(box);
      }
      box.querySelectorAll("[data-sp-running]").forEach((el) => { el.hidden = !scan; });
      q("[data-sp-meter]").hidden = !scan;
      if (scan) {
        seenScan.add(box);
        box.dataset.own = scan.own ? "1" : "";
        box.hidden = false;
        q("[data-sp-done]").hidden = true;
        q("[data-sp-result]").hidden = true;
        const known = scan.pct !== null && scan.pct !== undefined;
        q("[data-sp-label]").textContent = scan.label;
        q("[data-sp-phase]").textContent = scan.phase;
        q("[data-sp-pct]").textContent = known ? `${scan.pct} %` : "";
        q("[data-sp-meter]").classList.toggle("indeterminate", !known);
        q("[data-sp-bar]").style.width = (known ? scan.pct : 0) + "%";
        q("[data-sp-detail]").textContent = scan.detail || "";
        q("[data-sp-eta]").textContent = scan.eta ? ` · ${scan.eta}` : "";
        q("[data-sp-since]").textContent = scan.started ? ` · running since ${window.formatLocalTime(scan.started)}` : "";
      } else if (seenScan.has(box)) {
        const res = box.dataset.own && result && (result.summary || result.error) ? result : null;
        const shown = q("[data-sp-result]");             // the Status page knows how its own scan went
        if (!result && !shown.hidden) return;            // the slower poll here knows nothing about it
        q("[data-sp-done]").hidden = !!res;
        q("[data-sp-reload]").href = location.href;
        shown.hidden = !res;
        if (res) {
          shown.className = "sp-done flash " + (res.error ? "error" : "ok");
          shown.textContent = res.error || `Finished: ${res.summary}`;
        }
      } else {
        box.hidden = true;
      }
    });
  };

  let busy = false;
  async function poll() {
    if (busy || document.hidden) return;
    busy = true;
    try {
      const after = seen();
      const r = await fetch(after === null ? url : `${url}?after=${after}`, { headers: { Accept: "application/json" } });
      if (!r.ok) return;
      const data = await r.json();
      if (data.nav) navState(data.nav);
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
    box.replaceChildren(more);
    restore();
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
})();
