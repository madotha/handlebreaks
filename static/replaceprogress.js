// Progress bar of all ongoing replaces at the top of the Status and Files pages
// (templates/_replace_progress.html). Polls /replace/progress: every 3 s while replaces are
// ongoing, every 15 s otherwise, only while the page is visible. The replace dialogs call
// window.ReplaceProgress.kick() when they started a job, so the bar appears at once.
// When a replace moves on (moved, synced, checked by Plex, failed) or the wave ends, it fires a
// "replaceprogress" event on document, so a page can refresh its lists (the Status page does).
(() => {
  const box = document.querySelector("[data-rp]");
  if (!box) return;
  const $ = (s) => box.querySelector(s);
  const list = $("[data-rp-list]"), toggle = $("[data-rp-toggle]");
  const FAST = 3000, SLOW = 15000;
  let timer = null, busy = false, fast = false, last = null;

  function render(p) {
    const show = p.total > 0 && (p.active || p.finished);
    box.hidden = !show;
    if (!show) { list.hidden = true; toggle.setAttribute("aria-expanded", "false"); return; }
    box.classList.toggle("is-finished", p.finished);
    box.classList.toggle("has-failed", p.failed > 0);
    const settled = p.done + p.failed;
    let label;
    if (p.finished) {
      label = `${p.done} replaced` + (p.failed ? ` · ${p.failed} failed` : "");
    } else {
      label = `Replacing ${settled} of ${p.total}` + (p.waiting ? ` · ${p.waiting} waiting for the worker` : "");
    }
    $("[data-rp-label]").textContent = label;
    for (const k of ["done", "checking", "failed"]) {
      $(`[data-rp-seg="${k}"]`).style.width = `${(100 * p[k]) / p.total}%`;
    }
    const plex = p.plex;
    $("[data-rp-plex]").textContent = plex
      ? "Plex " + [plex.ok ? `✓ ${plex.ok}` : "", plex.open ? `… ${plex.open}` : "", plex.failed ? `✗ ${plex.failed}` : ""].filter(Boolean).join("  ")
      : "";
    list.replaceChildren(...p.items.map((it) => {
      const li = document.createElement("li");
      li.className = `rp-item rp-item-${it.stage}`;
      const name = document.createElement("span");
      name.className = "rp-name";
      name.textContent = it.name;
      name.title = it.name;
      const state = document.createElement("span");
      state.className = "rp-state";
      state.textContent = it.label;
      li.append(name, state);
      return li;
    }));
  }

  async function poll() {
    if (busy || document.hidden) return schedule();
    busy = true;
    try {
      const r = await fetch(box.dataset.url, { headers: { Accept: "application/json" } });
      if (r.ok) {
        const p = await r.json();
        render(p);
        fast = p.active || p.finished;
        const plex = p.plex || {};
        const sig = [p.total, p.waiting, p.checking, p.done, p.failed, plex.ok, plex.open, plex.failed].join("/");
        if (last !== null && sig !== last && p.total) {
          document.dispatchEvent(new CustomEvent("replaceprogress", { detail: p }));
        }
        last = sig;
      }
    } catch (e) { /* offline for a moment: try again later */ }
    busy = false;
    schedule();
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(poll, fast ? FAST : SLOW);
  }

  toggle.addEventListener("click", () => {
    list.hidden = !list.hidden;
    toggle.setAttribute("aria-expanded", String(!list.hidden));
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  window.ReplaceProgress = { kick() { fast = true; poll(); } };
  poll();
})();
