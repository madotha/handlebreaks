// Status page: live progress and tab counts without reloading the page. Every few
// seconds a small JSON snapshot is fetched; the list of the open tab (Queue, Awaiting
// review or History, which can be long) is only reloaded when the file being encoded
// changes, on "Refresh", or when the filter changes.
(function () {
  const script = document.currentScript;
  const liveUrl = script.dataset.liveUrl, queueUrl = script.dataset.queueUrl;
  const $ = (sel) => document.querySelector(sel);
  const panel = $("[data-current-panel]");
  const rows = $("[data-queue-rows]");
  const filterForm = $("[data-queue-filter]");
  const filter = $("[data-queue-filter] select");     // only in the Queue and History tabs
  const view = filterForm.dataset.view;
  const liveBox = $("[data-live]");
  let currentKey = panel.dataset.currentKey;
  let running = false;

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

  async function poll(force) {
    if (running) return;
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
        setText("[data-cur-name]", cur.short);
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
    }
  }

  // "Replace…" on finished encodes (static/replace.js); reload the page when done
  const msg = $("[data-replace-msg]");
  window.ReplaceDialog.attach(document, {
    say(text, kind) { msg.hidden = false; msg.className = "scan-msg " + kind; msg.textContent = text; },
    done: () => setTimeout(() => location.reload(), 2500),
  });

  // "Replace all finished…" and "Replace selected…" in the Awaiting review tab (static/bulkreplace.js)
  const bulk = {
    say(text, kind) { msg.hidden = false; msg.className = "scan-msg " + kind; msg.textContent = text; },
    done: () => location.reload(),
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
