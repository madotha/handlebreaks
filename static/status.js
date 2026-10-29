// Status page: live progress and tab counts without reloading the page. Every few
// seconds a small JSON snapshot is fetched; the list of the open tab (Queue, Awaiting
// review or History, which can be long) is only reloaded when the file being encoded
// changes, on "Refresh", or when the filter changes.
(function () {
  const script = document.currentScript;
  const liveUrl = script.dataset.liveUrl, queueUrl = script.dataset.queueUrl, moveUrl = script.dataset.moveUrl;
  const $ = (sel) => document.querySelector(sel);
  const panel = $("[data-current-panel]");
  const rows = $("[data-queue-rows]");
  const filterForm = $("[data-queue-filter]");
  const filter = $("[data-queue-filter] select");     // only in the Queue and History tabs
  const view = filterForm.dataset.view;
  const liveBox = $("[data-live]");
  let currentKey = panel.dataset.currentKey;
  const currentTab = $(".view-tabs [aria-current]");        // narrow screens: the tab strip scrolls, so show the open tab
  if (currentTab) currentTab.parentElement.scrollLeft = Math.max(0, currentTab.offsetLeft + currentTab.offsetWidth - currentTab.parentElement.clientWidth);
  let running = false, again = false;          // again: a forced refresh came in while one was running

  function sortParams() {                        // keep the column sort when reloading the list
    return filterForm.dataset.sort ? { sort: filterForm.dataset.sort, dir: filterForm.dataset.dir } : {};
  }

  function setText(sel, value) {
    const el = $(sel);
    if (el) el.textContent = value;
  }

  async function reloadQueue(append) {
    const more = $("[data-queue-more]");
    const url = append && more ? more.dataset.queueMore
      : `${queueUrl}?${new URLSearchParams({ view, state: filter ? filter.value : "", offset: 0, ...sortParams() })}`;
    const r = await fetch(url);
    if (!r.ok) return;
    const html = await r.text();
    const picked = new Set([...rows.querySelectorAll('input[name="key"]:checked')].map((c) => c.value));
    if (append) rows.insertAdjacentHTML("beforeend", html); else rows.innerHTML = html;
    rows.querySelectorAll('input[name="key"]').forEach((c) => { c.checked = picked.has(c.value); });
    updateSelection();
    $("[data-queue-empty]").hidden = rows.children.length > 0;
    const next = r.headers.get("X-Next");
    const shownBox = $("[data-queue-shown]");               // "Showing 100 of 250" while there is more
    shownBox.hidden = !next;
    shownBox.querySelector("[data-shown]").textContent = rows.children.length.toLocaleString();
    shownBox.querySelector("[data-total]").textContent = Number(r.headers.get("X-Shown-Total") || 0).toLocaleString();
    let btn = $("[data-queue-more]");
    if (next) {
      if (!btn) {
        btn = document.createElement("button");
        btn.type = "button";
        btn.className = "secondary";
        btn.textContent = "Show more";
        btn.setAttribute("data-queue-more", next);
        btn.addEventListener("click", () => reloadQueue(true));
        shownBox.after(btn);
      }
      btn.dataset.queueMore = next;
    } else if (btn) {
      btn.remove();
    }
  }

  // Selecting entries for the buttons below the tabs
  const selectAll = $("[data-select-all]");
  const needSelection = [...document.querySelectorAll("[data-needs-selection]")];
  needSelection.forEach((b) => { b.dataset.label = b.textContent; });
  function updateSelection() {
    const boxes = [...rows.querySelectorAll('input[name="key"]')];
    const n = boxes.filter((c) => c.checked).length;
    needSelection.forEach((b) => {
      b.disabled = n === 0;
      b.textContent = n ? `${b.dataset.label.replace("…", "")} (${n.toLocaleString()})${b.dataset.label.endsWith("…") ? "…" : ""}` : b.dataset.label;
    });
    selectAll.checked = n > 0 && n === boxes.length;
    selectAll.indeterminate = n > 0 && n < boxes.length;
  }
  rows.addEventListener("change", (e) => { if (e.target.name === "key") updateSelection(); });
  selectAll.addEventListener("change", () => {
    rows.querySelectorAll('input[name="key"]').forEach((c) => { c.checked = selectAll.checked; });
    updateSelection();
  });

  // Permanent deletes (Kept originals tab) ask first: how many files and how much space
  const gb = (n) => {                              // the same units as the size filter of the pages
    for (const unit of ["B", "KB", "MB", "GB"]) { if (n < 1024) return unit === "B" ? `${n} B` : `${n.toFixed(1)} ${unit}`; n /= 1024; }
    return `${n.toFixed(2)} TB`;
  };
  document.querySelectorAll("[data-confirm-delete]").forEach((b) => b.addEventListener("click", (e) => {
    const picked = [...rows.querySelectorAll('input[name="key"]:checked')];
    const bytes = picked.reduce((sum, c) => sum + Number(c.dataset.size || 0), 0);
    if (!confirm(`Delete ${picked.length.toLocaleString()} original${picked.length === 1 ? "" : "s"} (${gb(bytes)}) permanently? This cannot be undone.`)) e.preventDefault();
  }));
  document.querySelectorAll("form[data-confirm]").forEach((f) => f.addEventListener("submit", (e) => {
    if (!confirm(f.dataset.confirm)) e.preventDefault();
  }));

  // Changing the order of waiting entries (Queue tab): the arrow buttons and dragging a row's
  // handle. Ticked rows move together when the row you grab is ticked. "Move to top / bottom"
  // are ordinary buttons of the #bulk form.
  async function move(op, keys, target) {
    const body = new URLSearchParams({ csrf: $("#bulk input[name=csrf]").value, op, json: "1", target: target || "" });
    keys.forEach((k) => body.append("key", k));
    const r = await fetch(moveUrl, { method: "POST", body });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) alert(j.message || "The entries could not be moved.");
    await reloadQueue();
  }
  const keysOf = (tr) => {
    const box = tr.querySelector('input[name="key"]');
    return box && box.checked ? [...rows.querySelectorAll('input[name="key"]:checked')].map((c) => c.value) : [tr.dataset.key];
  };
  rows.addEventListener("click", (e) => {
    const b = e.target.closest("[data-move]");
    if (b) move(b.dataset.move, keysOf(b.closest("tr")));
  });

  let dragKeys = null;
  const clearDrop = () => rows.querySelectorAll(".drop-before, .drop-after, .dragging")
    .forEach((r) => r.classList.remove("drop-before", "drop-after", "dragging"));
  // Where a drop on this row lands: the key to insert before ("" = the end), or null if
  // that place isn't known (the end of a list with more rows to load)
  function dropTarget(tr, after) {
    if (!after) return tr.dataset.key;
    let next = tr.nextElementSibling;
    while (next && !next.matches("[data-waiting]")) next = next.nextElementSibling;
    if (next) return next.dataset.key;
    return $("[data-queue-more]") ? null : "";
  }
  rows.addEventListener("dragstart", (e) => {
    const grip = e.target.closest && e.target.closest("[data-grip]");
    if (!grip) return;
    const tr = grip.closest("tr");
    dragKeys = keysOf(tr);
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", dragKeys.join("\n"));
    e.dataTransfer.setDragImage(tr, 12, 12);
    tr.classList.add("dragging");
  });
  rows.addEventListener("dragover", (e) => {
    const tr = dragKeys && e.target.closest("tr[data-waiting]");
    if (!tr) return;
    const after = e.clientY > tr.getBoundingClientRect().top + tr.offsetHeight / 2;
    const target = dropTarget(tr, after);
    rows.querySelectorAll(".drop-before, .drop-after").forEach((r) => r.classList.remove("drop-before", "drop-after"));
    if (target === null || dragKeys.includes(tr.dataset.key)) return;
    e.preventDefault();
    tr.classList.add(after ? "drop-after" : "drop-before");
  });
  rows.addEventListener("drop", (e) => {
    const tr = dragKeys && e.target.closest("tr[data-waiting]");
    if (!tr) return;
    e.preventDefault();
    const target = dropTarget(tr, e.clientY > tr.getBoundingClientRect().top + tr.offsetHeight / 2);
    const keys = dragKeys;
    clearDrop();
    dragKeys = null;
    if (target !== null && !keys.includes(tr.dataset.key)) move("before", keys, target);
  });
  rows.addEventListener("dragend", () => { clearDrop(); dragKeys = null; });

  async function poll(force) {
    if (running) { if (force) again = true; return; }
    running = true;
    try {
      const r = await fetch(liveUrl);
      if (!r.ok) return;
      const s = await r.json();
      const cur = s.current;
      $("[data-current]").hidden = !cur;
      $("[data-idle]").hidden = !!cur;
      $("[data-stop-form]").hidden = !cur || !!s.stopping || !s.can_stop;
      $("[data-no-stop]").hidden = !cur || s.can_stop;
      $("[data-stop-key]").value = cur ? cur.key : "";        // stop exactly the file shown
      $("[data-stopping]").hidden = !s.stopping;
      setText("[data-stopping-mode]", s.stopping === "all" ? "all" : "");
      $("[data-paused]").hidden = !s.paused;
      if (cur) {
        setText("[data-cur-i]", cur.i);
        setText("[data-cur-n]", cur.n);
        setText("[data-cur-where]", cur.where);
        setText("[data-cur-name]", cur.file);
        setText("[data-cur-pct]", cur.pct || "0");
        setText("[data-cur-fps]", cur.fps || "–");
        setText("[data-cur-eta]", cur.eta || "–");
        $("[data-cur-bar]").style.width = (cur.pct || 0) + "%";
      }
      document.querySelectorAll("[data-count]").forEach((el) => {
        el.textContent = (s.counts[el.dataset.count] || 0).toLocaleString();
      });
      const removeAll = $("[data-remove-all]");
      if (removeAll) removeAll.disabled = !s.counts.waiting;
      const scanBox = $("[data-scan-running]");
      scanBox.hidden = !s.scan.running;
      if (s.scan.running) {
        setText("[data-scan-label]", s.scan.label || "Scan");
        setText("[data-scan-started]", s.scan.started ? window.formatLocalTime(s.scan.started) : "");
      }
      const key = cur ? cur.key : "";
      if (force || key !== currentKey) {           // next file started: entry states changed
        currentKey = key;
        await reloadQueue(false);
      }
    } finally {
      running = false;
      if (again) { again = false; poll(true); }
    }
  }

  // "Replace…" on finished encodes (static/replace.js). The lists refresh whenever a replace
  // moves on (event of static/replaceprogress.js), up to Plex's check, and when all are done.
  const msg = $("[data-replace-msg]");
  window.ReplaceDialog.attach(document, {
    say(text, kind) { msg.hidden = false; msg.className = "scan-msg " + kind; msg.textContent = text; },
    done: () => poll(true),
  });
  document.addEventListener("replaceprogress", () => poll(true));

  // "Replace all finished…" and "Replace selected…" in the Awaiting review tab (static/bulkreplace.js)
  const bulk = {
    say(text, kind) { msg.hidden = false; msg.className = "scan-msg " + kind; msg.textContent = text; },
    done: () => poll(true),
  };
  const replaceAll = $("[data-bulk-replace-all]"), replaceSelected = $("[data-bulk-replace-selected]");
  if (replaceAll) replaceAll.addEventListener("click", () => {
    const all = new FormData();
    all.append("all", "1");
    window.BulkReplace.open(all, bulk);
  });
  if (replaceSelected) replaceSelected.addEventListener("click", () => {
    const picked = new FormData();
    rows.querySelectorAll('input[name="key"]:checked').forEach((c) => picked.append("key", c.value));
    window.BulkReplace.open(picked, bulk);
  });

  setInterval(() => { if (liveBox.checked && !document.hidden) poll(false); }, 3000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && liveBox.checked) poll(false); });
  $("[data-refresh]").addEventListener("click", () => poll(true));
  if (filter) filter.addEventListener("change", () => {
    document.querySelectorAll("[data-state]").forEach((i) => { i.value = filter.value; });   // "Dismiss" applies to what the filter shows
    reloadQueue(false);
  });
  const more = $("[data-queue-more]");
  if (more) more.addEventListener("click", () => reloadQueue(true));
})();
