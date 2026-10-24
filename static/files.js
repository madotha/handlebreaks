// Files page: folder tree that loads each folder when it's opened, "Show more" for
// long file lists, and selection for queueing. A ticked folder is sent as a whole
// (the server expands it), so files inside it don't need to be loaded.
(function () {
  const script = document.currentScript;
  const form = document.getElementById("queue-form");
  if (!form) return;
  const count = form.querySelector("[data-selected-count]");
  const button = form.querySelector("[data-queue-button]");
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
  }

  // Remember open folders for this tab, so they open again after a reload (e.g. sorting)
  const OPEN_KEY = "files-open-folders";
  const openSet = new Set((() => { try { return JSON.parse(sessionStorage.getItem(OPEN_KEY)) || []; } catch (e) { return []; } })());
  function saveOpen() { try { sessionStorage.setItem(OPEN_KEY, JSON.stringify([...openSet])); } catch (e) { /* storage blocked */ } }

  async function load(details) {
    const body = details.querySelector(":scope > .dir-body");
    if (body.dataset.loaded) return;
    body.dataset.loaded = "1";
    try {
      const r = await fetch(body.dataset.src);
      if (!r.ok) throw new Error(r.status);
      body.innerHTML = await r.text();
    } catch (e) {
      body.innerHTML = '<p class="flash error">Could not load this folder. Reload the page and try again.</p>';
      delete body.dataset.loaded;
      return;
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

  function init(root) {
    root.querySelectorAll("details.dir").forEach((d) => {
      d.addEventListener("toggle", () => {
        if (d.open) { openSet.add(d.dataset.path); load(d); } else { openSet.delete(d.dataset.path); }
        saveOpen();
      });
      if (!d.open && openSet.has(d.dataset.path)) d.open = true;   // fires toggle -> loads
      else if (d.open) load(d);
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
      await load(parent);
    } catch (err) {
      say("The scan request failed. Reload the page and try again.", "error");
    } finally {
      btn.classList.remove("busy");
      btn.disabled = false;
    }
  });

  // Re-scan one file or folder and reload the folder level that shows it
  async function rescan(path, levelBody) {
    const body = new FormData();
    body.append("csrf", form.querySelector('input[name="csrf"]').value);
    body.append("path", path);
    await fetch(form.querySelector("[data-scan-path]").formAction, { method: "POST", body, headers: { Accept: "application/json" } });
    const parent = levelBody && levelBody.closest("details.dir");
    if (!parent) { location.reload(); return; }
    delete levelBody.dataset.loaded;
    await load(parent);
  }

  // "Replace…": rename, then let the worker move the encode in place of the original
  const dialog = document.querySelector("[data-replace-dialog]");
  let replacing = null;
  form.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-replace]");
    if (!btn || !dialog) return;
    e.preventDefault();
    const encoded = btn.dataset.encoded;
    const dot = encoded.lastIndexOf(".");
    replacing = btn;
    dialog.querySelector("[data-r-encoded]").textContent = encoded;
    dialog.querySelector("[data-r-original]").textContent = btn.dataset.original;
    dialog.querySelector("[data-r-name]").value = encoded.slice(0, dot);
    dialog.querySelector("[data-r-ext]").textContent = encoded.slice(dot);
    dialog.querySelector("[data-r-keep]").checked = true;
    dialog.querySelector("[data-r-error]").hidden = true;
    dialog.showModal();
  });
  if (dialog) dialog.querySelector("[data-r-ok]").addEventListener("click", async (e) => {
    e.preventDefault();
    const btn = replacing;
    const name = dialog.querySelector("[data-r-name]").value.trim() + dialog.querySelector("[data-r-ext]").textContent;
    const body = new FormData();
    body.append("csrf", form.querySelector('input[name="csrf"]').value);
    body.append("key", btn.dataset.key);
    body.append("new_name", name);
    if (dialog.querySelector("[data-r-keep]").checked) body.append("keep_original", "1");
    const r = await fetch(script.dataset.replaceUrl, { method: "POST", body, headers: { Accept: "application/json" } });
    const res = await r.json();
    if (res.error) {
      const err = dialog.querySelector("[data-r-error]");
      err.textContent = res.error;
      err.hidden = false;
      return;
    }
    dialog.close();
    const levelBody = btn.closest(".dir-body");
    const folder = btn.dataset.key.split("/").slice(0, -1).join("/");
    say(`Replacing ${btn.dataset.original} … waiting for the worker`, "busy");
    for (let i = 0; i < 600; i++) {                 // up to ~20 minutes (e.g. a copy across disks)
      await new Promise((ok) => setTimeout(ok, 2000));
      const st = await (await fetch(res.status_url)).json();
      if (st.state === "pending") continue;
      say(st.message, st.state === "done" ? "ok" : "error");
      if (st.state === "done") await rescan(folder, levelBody);
      return;
    }
    say("The worker hasn't picked up the replace job yet. Is it running? See the Status page.", "error");
  });

  form.addEventListener("change", update);
  form.addEventListener("submit", (e) => {
    const n = Number(button.dataset.count || 0);
    if (n > 200 && !confirm(`Queue ${n.toLocaleString()} files for encoding?`)) e.preventDefault();
  });
  init(form);
})();
