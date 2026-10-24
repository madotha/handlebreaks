// Files page: folder tree that loads each folder when it's opened, "Show more" for
// long file lists, and selection for queueing. A ticked folder is sent as a whole
// (the server expands it), so files inside it don't need to be loaded.
(function () {
  const form = document.getElementById("queue-form");
  if (!form) return;
  const count = form.querySelector("[data-selected-count]");
  const button = form.querySelector("[data-queue-button]");
  const bulkButton = form.querySelector("[data-bulk-replace]");
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

  async function load(details, fresh) {
    const body = details.querySelector(":scope > .dir-body");
    if (body.dataset.loaded) return;
    body.dataset.loaded = "1";
    const url = `${form.dataset.folderUrl}?path=${encodeURIComponent(details.dataset.path)}` +
      (form.dataset.folderQuery ? "&" + form.dataset.folderQuery : "");
    const cached = fresh ? null : cacheGet(url);
    if (cached !== null) {
      body.innerHTML = cached;
    } else {
      body.innerHTML = '<p class="muted small loading">Loading…</p>';
      try {
        const r = await fetch(url);
        if (!r.ok) throw new Error(r.status);
        const html = await r.text();
        body.innerHTML = html;
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
        if (d.open) { openSet.add(d.dataset.path); load(d); } else { openSet.delete(d.dataset.path); }
      });
      if (openSet.has(d.dataset.path)) d.open = true;   // reloaded level: fires toggle -> loads
    });
    // The folder checkbox sits inside <summary>: cancel the click so the folder
    // doesn't open or close, then apply the new state ourselves.
    root.querySelectorAll(".dir-toggle").forEach((t) => {
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
  function say(text, kind) {
    msg.hidden = false;
    msg.className = "scan-msg " + kind;
    msg.textContent = text;
  }
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

  form.addEventListener("change", update);
  form.addEventListener("submit", (e) => {
    const n = Number(button.dataset.count || 0);
    if (n > 200 && !confirm(`Queue ${n.toLocaleString()} files for encoding?`)) e.preventDefault();
  });
  init(form);
})();
