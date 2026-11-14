// Files page: folder tree that loads each folder when it's opened, "Show more" for
// long file lists, and selection for queueing. A ticked folder is sent as a whole
// (the server expands it), so files inside it don't need to be loaded.
(function () {
  const cancelUrl = document.currentScript.dataset.cancelUrl;
  // Filter panel (left of the tree; a drawer on phones). On wider screens every click
  // applies at once; in the drawer "Show files" applies all changes together.
  const layout = document.querySelector("[data-files-layout]");
  const filterForm = document.querySelector("[data-filter-form]");
  const range = document.querySelector("[data-date-range]");
  const phone = matchMedia("(max-width: 720px)");
  if (layout && filterForm) {
    const panel = layout.querySelector("[data-filter-panel]");
    const toggle = document.querySelector("[data-filter-toggle]");
    const backdrop = layout.querySelector("[data-filter-backdrop]");
    const datesSet = () => [...range.querySelectorAll('input[type="date"]')].some((i) => i.value);
    const apply = () => {
      sessionStorage.setItem("files:filter-scroll", panel.scrollTop);
      filterForm.requestSubmit();
    };
    filterForm.addEventListener("change", (e) => {
      const t = e.target;
      if (t.name === "added") range.hidden = t.value !== "custom";
      if (phone.matches || t.type === "search") return;
      if (t.type === "date" || (t.name === "added" && t.value === "custom")) {
        // a custom range applies once both dates are set, or with "Show files" for an open end
        const dates = [...range.querySelectorAll('input[type="date"]')];
        if (dates.every((i) => i.value)) apply();
        else {
          filterForm.classList.add("fp-pending");
          if (t.name === "added") dates[0].focus();
        }
        return;
      }
      apply();
    });
    // Leave empty fields out of the URL, and the dates unless the custom range is chosen
    filterForm.addEventListener("formdata", (e) => {
      const data = e.formData;
      const custom = data.get("added") === "custom";
      ["q", "added", "from", "to"].forEach((name) => {
        if (!data.get(name) || (!custom && (name === "from" || name === "to"))) data.delete(name);
      });
    });
    const saved = sessionStorage.getItem("files:filter-scroll");
    if (saved) { panel.scrollTop = +saved; sessionStorage.removeItem("files:filter-scroll"); }

    const setOpen = (open) => {           // phones: the drawer
      layout.classList.toggle("fp-open", open);
      backdrop.hidden = !open;
      document.documentElement.classList.toggle("fp-lock", open);
      toggle.setAttribute("aria-expanded", open);
      if (open) panel.querySelector("input, button").focus();
      else toggle.focus();
    };
    const sync = () => toggle.setAttribute("aria-expanded",
      phone.matches ? layout.classList.contains("fp-open") : !layout.classList.contains("fp-collapsed"));
    toggle.addEventListener("click", () => {
      if (phone.matches) { setOpen(!layout.classList.contains("fp-open")); return; }
      const hidden = layout.classList.toggle("fp-collapsed");
      localStorage.setItem("files:filters", hidden ? "hidden" : "shown");
      sync();
    });
    panel.querySelector("[data-filter-close]").addEventListener("click", () => {
      if (phone.matches) setOpen(false);
      else toggle.click();
    });
    backdrop.addEventListener("click", () => setOpen(false));
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && layout.classList.contains("fp-open")) setOpen(false);
    });
    phone.addEventListener("change", () => {
      if (!phone.matches && layout.classList.contains("fp-open")) setOpen(false);
      const choice = localStorage.getItem("files:filters");
      layout.classList.toggle("fp-collapsed", !phone.matches && (choice === "hidden" || (!choice && innerWidth < 1440)));
      sync();
    });
    sync();
  }
  // The date pickers: no future dates, "from" never after "to"; a click anywhere in a field opens its picker
  if (range) {
    const [from, to] = range.querySelectorAll('input[type="date"]');
    const now = new Date();
    const today = new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
    const limits = () => { to.min = from.value; from.max = to.value || today; to.max = today; };
    [from, to].forEach((input) => {
      input.addEventListener("change", limits);
      input.addEventListener("click", () => { try { input.showPicker(); } catch (e) { /* already open or unsupported */ } });
    });
    limits();
  }

  const form = document.getElementById("queue-form");
  if (!form) return;
  const count = form.querySelector("[data-selected-count]");
  const button = form.querySelector("[data-queue-button]");
  const bulkButton = form.querySelector("[data-bulk-replace]");
  const renameButton = form.querySelector("[data-bulk-rename]");   // only with the language tag on
  const toggleOf = (details) => details && details.querySelector(":scope > summary .dir-toggle");

  // Inputs below a ticked folder are ticked and disabled: the folder covers them,
  // and disabled inputs aren't submitted twice. data-locked marks inputs that are
  // disabled for good (already queued, or a folder with nothing to select).
  function cover(root, on) {
    root.querySelectorAll('input[name="key"], .dir-toggle').forEach((i) => {
      if ("locked" in i.dataset) return;
      i.checked = on;
      i.disabled = on;
    });
  }

  function update() {
    let n = 0;
    form.querySelectorAll(".dir-toggle:checked:not(:disabled)").forEach((t) => { n += Number(t.dataset.count); });
    n += form.querySelectorAll('input[name="key"]:checked:not(:disabled)').length;
    count.textContent = n.toLocaleString();
    button.disabled = n === 0;
    button.dataset.count = n;
    bulkButton.disabled = n === 0;
    if (renameButton) renameButton.disabled = n === 0;
    form.classList.toggle("has-selection", n > 0);     // phones: shows the selection bar above the tab bar
    if (window.sharePresetFor) {                       // name the preset "Share's preset" means for the ticked files
      const picked = [...form.querySelectorAll('.dir-toggle:checked:not(:disabled), input[name="key"]:checked:not(:disabled)')]
        .map((i) => i.value);
      window.sharePresetFor(form.querySelector("select[data-share-presets]"), picked);
    }
  }

  // Folders only open when clicked: every page load starts collapsed. Open folders are
  // tracked only on this page, so they stay open when their level is reloaded (↻, Replace…).
  const openSet = new Set();

  // Loaded folders are kept for this tab while the data doesn't change (data-version
  // changes with the database and the queue), so open folders show at once after a reload.
  const CACHE_PREFIX = "files-folder:";
  function cacheGet(url) {
    try {
      const c = JSON.parse(sessionStorage.getItem(CACHE_PREFIX + url));
      return c && c.v === form.dataset.version ? c.html : null;
    } catch (e) { return null; }
  }
  function cachePut(url, html) {
    try {
      for (let i = sessionStorage.length - 1; i >= 0; i--) {   // drop entries of older versions
        const k = sessionStorage.key(i);
        if (k.startsWith(CACHE_PREFIX) && !k.includes(form.dataset.version)) {
          const c = JSON.parse(sessionStorage.getItem(k) || "{}");
          if (c.v !== form.dataset.version) sessionStorage.removeItem(k);
        }
      }
      const entry = JSON.stringify({ v: form.dataset.version, html });
      try {
        sessionStorage.setItem(CACHE_PREFIX + url, entry);
      } catch (e) {                                            // full: make room once
        Object.keys(sessionStorage).filter((k) => k.startsWith(CACHE_PREFIX)).forEach((k) => sessionStorage.removeItem(k));
        sessionStorage.setItem(CACHE_PREFIX + url, entry);
      }
    } catch (e) { /* too big or storage blocked: just don't keep it */ }
  }

  // Folder loads still running (pull to refresh waits for them)
  const pending = new Set();
  let allFresh = false;                         // pull to refresh: nothing from the cache
  function track(promise) {
    pending.add(promise);
    promise.finally(() => pending.delete(promise));
    return promise;
  }

  async function load(details, fresh) {
    fresh = fresh || allFresh;
    const body = details.querySelector(":scope > .dir-body");
    if (body.dataset.loaded) return;
    body.dataset.loaded = "1";
    const url = `${form.dataset.folderUrl}?path=${encodeURIComponent(details.dataset.path)}` +
      (form.dataset.folderQuery ? "&" + form.dataset.folderQuery : "");
    const cached = fresh ? null : cacheGet(url);
    if (cached !== null) {
      body.innerHTML = cached;
      if (window.localizeTimes) window.localizeTimes(body);
    } else {
      body.innerHTML = '<p class="muted small loading">Loading…</p>';
      try {
        const r = await fetch(url);
        if (!r.ok) throw new Error(r.status);
        const html = await r.text();
        body.innerHTML = html;
        if (window.localizeTimes) window.localizeTimes(body);
        if (!fresh) cachePut(url, html);     // after a rescan the page's version is outdated
      } catch (e) {
        body.innerHTML = '<p class="flash error">Could not load this folder. Reload the page and try again.</p>';
        delete body.dataset.loaded;
        return;
      }
    }
    const parent = toggleOf(details);
    if (parent && parent.checked) cover(body, true);
    init(body);
  }

  async function more(btn) {
    btn.disabled = true;
    const r = await fetch(btn.dataset.more);
    if (!r.ok) { btn.disabled = false; return; }
    const tbody = btn.closest(".files-level").querySelector("tbody");
    const tmp = document.createElement("tbody");
    tmp.innerHTML = await r.text();
    const parent = toggleOf(btn.closest("details"));
    if (parent && parent.checked) cover(tmp, true);
    if (window.localizeTimes) window.localizeTimes(tmp);
    tbody.append(...tmp.children);
    const next = r.headers.get("X-Next");
    if (next) {
      btn.dataset.more = next;
      btn.disabled = false;
      const shown = tbody.children.length;
      const label = btn.querySelector("[data-more-label]");
      label.textContent = label.textContent.replace(/^\(\S+/, "(" + shown.toLocaleString());
    } else {
      btn.remove();
    }
    update();
  }

  // "Show more folders": the response holds the next folders and, if any are left, a new button
  async function moreDirs(btn) {
    btn.disabled = true;
    const r = await fetch(btn.dataset.moreDirs);
    if (!r.ok) { btn.disabled = false; return; }
    const tmp = document.createElement("div");
    tmp.innerHTML = await r.text();
    const parent = toggleOf(btn.closest("details"));
    if (parent && parent.checked) cover(tmp, true);
    init(tmp);
    btn.replaceWith(...tmp.childNodes);
  }

  function init(root) {
    root.querySelectorAll("details.dir").forEach((d) => {
      d.addEventListener("toggle", () => {
        if (d.open) { openSet.add(d.dataset.path); track(load(d)); } else { openSet.delete(d.dataset.path); }
      });
      if (openSet.has(d.dataset.path)) d.open = true;   // reloaded level: fires toggle -> loads
    });
    // The folder checkbox sits inside <summary>: cancel the click so the folder
    // doesn't open or close, then apply the new state ourselves.
    root.querySelectorAll(".dir-toggle").forEach((t) => {
      // Set from outside (static/rangeselect.js): cover the content like a click would
      t.addEventListener("change", () => cover(t.closest("details").querySelector(":scope > .dir-body"), t.checked));
      t.addEventListener("click", (e) => {
        const wanted = t.checked;
        e.preventDefault();
        setTimeout(() => {
          t.checked = wanted;
          cover(t.closest("details").querySelector(":scope > .dir-body"), wanted);
          update();
        });
      });
    });
    root.querySelectorAll("[data-more]").forEach((b) => b.addEventListener("click", () => more(b)));
    root.querySelectorAll("[data-more-dirs]").forEach((b) => b.addEventListener("click", () => moreDirs(b)));
    update();
  }

  // ↻ buttons: re-read one file or folder with ffprobe, then reload the folder it's in
  const msg = form.querySelector("[data-scan-msg]");
  let msgTimer = null;
  function say(text, kind) {
    msg.hidden = false;
    msg.className = "scan-msg " + kind;
    msg.textContent = text;
    // Phones show the message above the tab bar, over the list: it hides by itself (tap: at once)
    clearTimeout(msgTimer);
    if (kind !== "busy" && phone.matches) msgTimer = setTimeout(() => { msg.hidden = true; }, kind === "error" ? 10000 : 5000);
  }
  msg.addEventListener("click", () => { if (phone.matches) msg.hidden = true; });
  form.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-scan-path]");
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    const files = Number(btn.dataset.files || 1);
    if (files > 300 && !confirm(`Re-read all ${files.toLocaleString()} files of this folder with ffprobe? ` +
        `This takes about ${Math.ceil(files * 0.035 / 60)} min or more.`)) return;
    const body = new FormData();
    body.append("csrf", form.querySelector('input[name="csrf"]').value);
    body.append("path", btn.value);
    btn.classList.add("busy");
    btn.disabled = true;
    say("Scanning …", "busy");
    try {
      const r = await fetch(btn.formAction, { method: "POST", body, headers: { Accept: "application/json" } });
      const res = await r.json();
      if (res.error) { say(res.error, "error"); return; }
      say(res.message, "ok");
      if (!res.done) return;
      // reload the level the scanned row belongs to (a top-level share: the whole page)
      const row = btn.closest("details.dir") && btn.closest("summary") ? btn.closest("details.dir") : btn.closest("tr");
      const level = row.parentElement.closest(".dir-body");
      const parent = level && level.closest("details.dir");
      if (!parent) { location.reload(); return; }
      delete level.dataset.loaded;
      await load(parent, true);
    } catch (err) {
      say("The scan request failed. Reload the page and try again.", "error");
    } finally {
      btn.classList.remove("busy");
      btn.disabled = false;
    }
  });

  // "Replace…" (static/replace.js): the server has already rescanned the folder when the
  // job is reported done, so only reload the folder level that shows the file
  async function reloadLevel(levelBody) {
    const parent = levelBody && levelBody.closest("details.dir");
    if (!parent) { location.reload(); return; }
    delete levelBody.dataset.loaded;
    await load(parent, true);
  }
  window.ReplaceDialog.attach(form, { say, done: (btn) => reloadLevel(btn.closest(".dir-body")) });

  // Delete encode (static/deleteencode.js) and cancelling a replace that hasn't started:
  // the server answers right away, then the folder level of the file is loaded again
  window.onEncodeDeleted = (res, btn) => {
    say(res.message, res.kind === "error" ? "error" : "ok");
    if (res.ok && btn && btn.isConnected) reloadLevel(btn.closest(".dir-body"));
  };
  form.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-cancel-replace]");
    if (!btn) return;
    e.preventDefault();
    const body = new FormData();
    body.append("csrf", form.querySelector('input[name="csrf"]').value);
    body.append("id", btn.dataset.cancelReplace);
    body.append("json", "1");
    btn.disabled = true;
    try {
      const res = await (await fetch(cancelUrl, { method: "POST", body, headers: { Accept: "application/json" } })).json();
      say(res.message, res.ok ? "ok" : "error");
      if (window.ReplaceProgress) window.ReplaceProgress.kick();
      await reloadLevel(btn.closest(".dir-body"));
    } catch (err) {
      say("The replace could not be cancelled. Reload the page and try again.", "error");
      btn.disabled = false;
    }
  });

  // The table headers stick below the action bar, which changes height with the window;
  // on phones, messages and popups sit above the selection bar (--sel-h, 0 while it's hidden)
  const bar = form.querySelector(".action-bar");
  const selBar = form.querySelector("[data-sel-actions]");
  if (bar) new ResizeObserver(() => {
    document.documentElement.style.setProperty("--bar-h", `${bar.getBoundingClientRect().height}px`);
  }).observe(bar);
  if (selBar) new ResizeObserver(() => {
    document.documentElement.style.setProperty("--sel-h", `${selBar.getBoundingClientRect().height}px`);
  }).observe(selBar);

  // Installed app, pull down at the top (static/phone.js): load the tree again with the folders
  // that are open, then tick again what was ticked
  window.pullRefresh = async () => {
    const tree = form.querySelector(".tree");
    const picked = new Set([...form.querySelectorAll('input[name="key"]:checked, input[name="folder"]:checked')].map((i) => i.value));
    const r = await fetch(`${form.dataset.folderUrl}?path=` + (form.dataset.folderQuery ? "&" + form.dataset.folderQuery : ""));
    if (!r.ok) { say("Could not load the folders. Try again in a moment.", "error"); return; }
    allFresh = true;
    try {
      tree.innerHTML = await r.text();
      if (window.localizeTimes) window.localizeTimes(tree);
      init(tree);
      do {                                      // reopened folders open theirs once loaded
        await Promise.all([...pending]);
        await new Promise((done) => setTimeout(done));
      } while (pending.size);
    } finally {
      allFresh = false;
    }
    form.querySelectorAll('input[name="key"], input[name="folder"]').forEach((i) => {
      if (picked.has(i.value) && !i.disabled && !i.checked) {
        i.checked = true;
        i.dispatchEvent(new Event("change", { bubbles: true }));
      }
    });
    update();
  };

  // Phones: the open folders around the scroll position stay named below the header; tapping
  // one closes it and scrolls back to it
  const crumbs = document.querySelector("[data-crumbs]");
  let crumbKey = "", crumbTick = false;
  function updateCrumbs() {
    crumbTick = false;
    const line = document.querySelector(".top").getBoundingClientRect().bottom;
    const path = !phone.matches ? [] : [...form.querySelectorAll("details.dir[open]")].filter((d) =>
      d.querySelector(":scope > summary").getBoundingClientRect().bottom < line && d.getBoundingClientRect().bottom > line + 48);
    const key = path.map((d) => d.dataset.path).join("\n");
    if (key === crumbKey) return;
    crumbKey = key;
    crumbs.hidden = !path.length;
    crumbs.replaceChildren(...path.flatMap((d, i) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = d.querySelector(":scope > summary .dirname").textContent;
      b.title = "Close this folder";
      b.addEventListener("click", () => {
        d.open = false;
        d.querySelector(":scope > summary").scrollIntoView({ block: "start" });
      });
      if (!i) return [b];
      const sep = document.createElement("span");
      sep.textContent = "›";
      sep.setAttribute("aria-hidden", "true");
      return [sep, b];
    }));
  }
  if (crumbs) {
    const later = () => { if (!crumbTick) { crumbTick = true; requestAnimationFrame(updateCrumbs); } };
    addEventListener("scroll", later, { passive: true });
    form.addEventListener("toggle", later, true);
    phone.addEventListener("change", later);
  }

  // "Collapse all": close every folder, forget them for reloads and drop their content
  // (reopening is quick: loaded folders are kept in sessionStorage)
  const collapseBtn = form.querySelector("[data-collapse-all]");
  const syncCollapse = () => { collapseBtn.disabled = !form.querySelector("details.dir[open]"); };
  form.addEventListener("toggle", syncCollapse, true);    // toggle doesn't bubble: capture it
  syncCollapse();
  collapseBtn.addEventListener("click", () => {
    form.querySelectorAll("details.dir[open]").forEach((d) => {
      if (!d.isConnected) return;                           // inside a share unloaded just before
      d.open = false;
      const body = d.querySelector(":scope > .dir-body");
      // top level: unload the whole subtree, unless files in it are ticked (keep the selection)
      if (!d.parentElement.closest("details.dir") && !body.querySelector("input:checked:not(:disabled)")) {
        body.innerHTML = "";
        delete body.dataset.loaded;
      }
    });
    openSet.clear();
    update();
    syncCollapse();
  });

  // "Replace selected…" (static/bulkreplace.js): the ticked files and folders, with the filters
  bulkButton.addEventListener("click", () => {
    window.BulkReplace.open(new FormData(form), { say, done: () => location.reload() });
  });
  if (renameButton) renameButton.addEventListener("click", () => {
    window.BulkRename.open(new FormData(form), { say, done: () => location.reload() });
  });

  // Phones: the file rows are compact; tapping a file name shows duration, date added and ↻
  const compact = window.matchMedia("(max-width: 720px)");
  form.addEventListener("click", (e) => {
    const cell = e.target.closest("td.name");
    if (cell && compact.matches) cell.parentElement.classList.toggle("expanded");
  });

  form.addEventListener("change", update);
  form.addEventListener("submit", (e) => {
    const n = Number(button.dataset.count || 0);
    if (n > 200 && !confirm(`Queue ${n.toLocaleString()} files for encoding?`)) e.preventDefault();
  });
  init(form);
})();
