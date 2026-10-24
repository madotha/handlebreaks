// Status page: live progress and queue counts without reloading the page. Every few
// seconds a small JSON snapshot is fetched; the queue list (which can be long) is only
// reloaded when the file being encoded changes, on "Refresh", or when the filter changes.
(function () {
  const script = document.currentScript;
  const liveUrl = script.dataset.liveUrl, queueUrl = script.dataset.queueUrl;
  const $ = (sel) => document.querySelector(sel);
  const panel = $("[data-current-panel]");
  const rows = $("[data-queue-rows]");
  const filter = $("[data-queue-filter] select");
  const liveBox = $("[data-live]");
  let currentKey = panel.dataset.currentKey;
  let running = false;

  function sortParams() {                        // keep the column sort when reloading the list
    const f = $("[data-queue-filter]");
    return f.dataset.sort ? { sort: f.dataset.sort, dir: f.dataset.dir } : {};
  }

  function setText(sel, value) {
    const el = $(sel);
    if (el) el.textContent = value;
  }

  async function reloadQueue(append) {
    const more = $("[data-queue-more]");
    const url = append && more ? more.dataset.queueMore
      : `${queueUrl}?${new URLSearchParams({ state: filter.value, offset: 0, ...sortParams() })}`;
    const r = await fetch(url);
    if (!r.ok) return;
    const html = await r.text();
    const picked = new Set([...rows.querySelectorAll('input[name="key"]:checked')].map((c) => c.value));
    if (append) rows.insertAdjacentHTML("beforeend", html); else rows.innerHTML = html;
    rows.querySelectorAll('input[name="key"]').forEach((c) => { c.checked = picked.has(c.value); });
    updateSelection();
    $("[data-queue-empty]").hidden = rows.children.length > 0;
    const next = r.headers.get("X-Next");
    let btn = $("[data-queue-more]");
    if (next) {
      if (!btn) {
        btn = document.createElement("button");
        btn.type = "button";
        btn.className = "secondary";
        btn.textContent = "Show more";
        btn.setAttribute("data-queue-more", next);
        btn.addEventListener("click", () => reloadQueue(true));
        rows.closest(".table-wrap").after(btn);
      }
      btn.dataset.queueMore = next;
    } else if (btn) {
      btn.remove();
    }
  }

  // Selecting waiting entries for "Remove selected"
  const selectAll = $("[data-select-waiting]");
  function updateSelection() {
    const boxes = [...rows.querySelectorAll('input[name="key"]')];
    const n = boxes.filter((c) => c.checked).length;
    const btn = $("[data-remove-selected]");
    btn.disabled = n === 0;
    btn.textContent = n ? `Remove selected (${n.toLocaleString()})` : "Remove selected";
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
      $("[data-stop-form]").hidden = !cur || !!s.stopping;
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
      $("[data-remove-all]").disabled = !s.counts.waiting;
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

  setInterval(() => { if (liveBox.checked && !document.hidden) poll(false); }, 3000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && liveBox.checked) poll(false); });
  $("[data-refresh]").addEventListener("click", () => poll(true));
  filter.addEventListener("change", () => reloadQueue(false));
  const more = $("[data-queue-more]");
  if (more) more.addEventListener("click", () => reloadQueue(true));
})();
