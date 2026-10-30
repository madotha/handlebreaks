// Progress bar of all ongoing replaces at the top of the Status and Files pages
// (templates/_replace_progress.html). Polls /replace/progress: every 3 s while replaces are
// ongoing, every 15 s otherwise, only while the page is visible. The replace dialogs call
// window.ReplaceProgress.kick() when they started a job, so the bar appears at once.
// When a replace moves on (moved, synced, checked by Plex, failed) or the wave ends, it fires a
// "replaceprogress" event on document, so a page can refresh its lists (the Status page does).
// Replaces the worker hasn't started yet can be cancelled here: one by one in the details
// list, or all at once ("Cancel waiting"); POST /replace/cancel.
(() => {
  const box = document.querySelector("[data-rp]");
  if (!box) return;
  const $ = (s) => box.querySelector(s);
  const list = $("[data-rp-list]"), toggle = $("[data-rp-toggle]"), cancelAll = $("[data-rp-cancel-all]");
  const FAST = 3000, SLOW = 15000;
  let timer = null, busy = false, fast = false, last = null;

  function render(p) {
    const show = p.total > 0 && (p.active || p.finished);
    box.hidden = !show;
    if (!show) { list.hidden = true; toggle.setAttribute("aria-expanded", "false"); cancelAll.hidden = true; return; }
    box.classList.toggle("is-finished", p.finished);
    box.classList.toggle("has-failed", p.failed > 0);
    const settled = p.done + p.failed + p.cancelled;
    let label;
    if (p.finished) {
      label = `${p.done} replaced` + (p.failed ? ` · ${p.failed} failed` : "") + (p.cancelled ? ` · ${p.cancelled} cancelled` : "");
    } else {
      label = `Replacing ${settled} of ${p.total}` + (p.waiting ? ` · ${p.waiting} waiting for the worker` : "")
        + (p.cancelled ? ` · ${p.cancelled} cancelled` : "");
    }
    cancelAll.hidden = !p.waiting;
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
      if (it.stage === "waiting") {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "rp-cancel";
        b.textContent = "Cancel";
        b.title = "Cancel this replace; nothing has been moved yet";
        b.addEventListener("click", () => cancel([it.id], b));
        li.append(b);
      }
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
        const sig = [p.total, p.waiting, p.moving, p.checking, p.done, p.failed, p.cancelled, plex.ok, plex.open, plex.failed].join("/");
        if (last !== null && sig !== last && p.total) {
          document.dispatchEvent(new CustomEvent("replaceprogress", { detail: p }));
        }
        last = sig;
      }
    } catch (e) { /* offline for a moment: try again later */ }
    busy = false;
    schedule();
  }

  async function cancel(ids, btn) {
    const body = new URLSearchParams({ csrf: box.dataset.csrf, json: "1" });
    if (ids) ids.forEach((i) => body.append("id", i)); else body.append("all", "1");
    if (btn) btn.disabled = true;
    try {
      const r = await fetch(box.dataset.cancelUrl, { method: "POST", body, headers: { Accept: "application/json" } });
      const j = await r.json().catch(() => ({}));
      if (j.late) alert(j.message);
    } catch (e) { alert("The replace could not be cancelled. Try again."); }
    if (btn) btn.disabled = false;
    poll();
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(poll, fast ? FAST : SLOW);
  }

  cancelAll.addEventListener("click", () => {
    if (confirm("Cancel every replace the worker hasn't started yet? No file is moved; the encodes stay in the output folder.")) cancel(null, cancelAll);
  });
  toggle.addEventListener("click", () => {
    list.hidden = !list.hidden;
    toggle.setAttribute("aria-expanded", String(!list.hidden));
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  window.ReplaceProgress = { kick() { fast = true; poll(); } };
  poll();
})();
