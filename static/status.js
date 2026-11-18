// Status page: live progress and tab counts without reloading the page. Every few
// seconds a small JSON snapshot is fetched; the list of the open tab (Queue, Awaiting
// review or History, which can be long) is only reloaded when the file being encoded
// changes, when the tab's count changes (e.g. an encode finished), on "Refresh", or when
// the filter changes.
//
// The tabs switch in place: the tab's section ([data-status-tab]) of the chosen page is
// fetched and swapped in, the address follows (Back and reload work as before). Other tabs
// are fetched ahead while the page is idle and when a tab is pointed at, and kept for a
// short time, so a switch usually shows at once; a kept copy's list is reloaded right after.
(function () {
  const script = document.currentScript;
  const liveUrl = script.dataset.liveUrl, queueUrl = script.dataset.queueUrl, moveUrl = script.dataset.moveUrl;
  const keysUrl = script.dataset.keysUrl;
  const $ = (sel) => document.querySelector(sel);
  const panel = $("[data-current-panel]");
  const liveBox = $("[data-live]");
  let currentKey = panel.dataset.currentKey;
  let running = false, again = false;          // again: a forced refresh came in while one was running
  let T = null;                                 // the open tab (initTab)

  function setText(sel, value) {
    const el = $(sel);
    if (el) el.textContent = value;
  }

  // Permanent deletes (Kept originals tab) ask first: how many files and how much space
  const gb = (n) => {                              // the same units as the size filter of the pages
    for (const unit of ["B", "KB", "MB", "GB"]) { if (n < 1024) return unit === "B" ? `${n} B` : `${n.toFixed(1)} ${unit}`; n /= 1024; }
    return `${n.toFixed(2)} TB`;
  };

  // Everything that belongs to one tab's section; called again for every section swapped in.
  // Listeners sit on elements inside the section, so they go with it.
  function initTab(section) {
    const q = (sel) => section.querySelector(sel);
    const rows = q("[data-queue-rows]");
    const filterForm = q("[data-queue-filter]");
    const filter = q("[data-queue-filter] select");     // only in the Queue and History tabs
    const search = q("[data-queue-search]");
    const view = filterForm.dataset.view;
    const tabCount = q(`.view-tabs [data-count="${view}"]`);   // count of the open tab as rendered: a change
    const t = { section, view, viewCount: tabCount ? Number(tabCount.textContent.replace(/\D/g, "")) : null };
    const currentTab = q(".view-tabs [aria-current]");        // narrow screens: the tab strip scrolls, so show the open tab
    if (currentTab) currentTab.parentElement.scrollLeft = Math.max(0, currentTab.offsetLeft + currentTab.offsetWidth - currentTab.parentElement.clientWidth);

    function sortParams() {                        // keep the column sort when reloading the list
      return filterForm.dataset.sort ? { sort: filterForm.dataset.sort, dir: filterForm.dataset.dir } : {};
    }

    async function reloadQueue(append) {
      const more = q("[data-queue-more]");
      const url = append && more ? more.dataset.queueMore
        : `${queueUrl}?${new URLSearchParams({ view, state: filter ? filter.value : "", q: search.value.trim(), offset: 0, ...sortParams() })}`;
      const r = await fetch(url);
      if (!r.ok || !section.isConnected) return;
      const html = await r.text();
      const picked = new Set([...rows.querySelectorAll('input[type=checkbox][name="key"]:checked')].map((c) => c.value));
      if (append) rows.insertAdjacentHTML("beforeend", html); else rows.innerHTML = html;
      if (window.localizeTimes) window.localizeTimes(rows);
      rows.querySelectorAll('input[type=checkbox][name="key"]').forEach((c) => { c.checked = picked.has(c.value) || (matching && matching.includes(c.value)); });
      shownTotal = Number(r.headers.get("X-Shown-Total") || 0);
      updateSelection();
      q("[data-queue-empty]").hidden = rows.children.length > 0;
      const next = r.headers.get("X-Next");
      const shownBox = q("[data-queue-shown]");               // "Showing 100 of 250" while there is more
      shownBox.hidden = !next;
      shownBox.querySelector("[data-shown]").textContent = rows.children.length.toLocaleString();
      shownBox.querySelector("[data-total]").textContent = Number(r.headers.get("X-Shown-Total") || 0).toLocaleString();
      const summary = q("[data-view-summary]"), summaryText = r.headers.get("X-Summary");
      if (summary && summaryText !== null) { summary.textContent = summaryText; summary.hidden = !summaryText; }
      let btn = q("[data-queue-more]");
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
    t.reloadQueue = reloadQueue;

    // Selecting entries for the buttons below the tabs
    const bulkForm = q("#bulk");
    // Phones: the buttons for ticked entries are a bar at the bottom; toasts sit above it (--sel-h)
    const selBar = q("[data-bulk-sel]");
    const selObs = new ResizeObserver(() => {
      document.documentElement.style.setProperty("--sel-h", `${selBar.getBoundingClientRect().height}px`);
    });
    selObs.observe(selBar);
    // Phones: the buttons for the whole list are in the More menu (closed by the page's listeners)
    t.moreBtn = q("[data-more-toggle]");
    t.moreMenu = q("[data-more-menu]");
    t.setMore = (open) => { t.moreMenu.classList.toggle("open", open); t.moreBtn.setAttribute("aria-expanded", String(open)); };
    t.moreBtn.addEventListener("click", () => t.setMore(!t.moreMenu.classList.contains("open")));
    const selectAlls = [...section.querySelectorAll("[data-select-all]")];   // table header, and a separate one on phones
    const needSelection = [...section.querySelectorAll("[data-needs-selection]")];
    needSelection.forEach((b) => { b.dataset.label = b.textContent; });
    // "Select all N matching": every entry of the tab (with its filter and search), not only the
    // rows loaded so far; the keys of rows not shown go into the form as hidden inputs
    let matching = null, matchingSizes = null, shownTotal = Number(rows.dataset.total || 0);
    const matchBox = q("[data-select-matching]"), matchInputs = q("[data-matching-keys]");
    function clearMatching() {
      matching = matchingSizes = null;
      matchInputs.replaceChildren();
    }
    async function selectMatching() {
      const params = new URLSearchParams({ view, state: filter ? filter.value : "", q: search.value.trim() });
      const r = await fetch(`${keysUrl}?${params}`);
      if (!r.ok) return;
      const j = await r.json();
      matching = j.keys;
      matchingSizes = j.sizes;
      const shown = new Set([...rows.querySelectorAll('input[type=checkbox][name="key"]')].map((c) => c.value));
      matchInputs.replaceChildren(...matching.filter((k) => !shown.has(k)).map((k) => {
        const input = document.createElement("input");
        input.type = "hidden";
        input.name = "key";
        input.value = k;
        return input;
      }));
      rows.querySelectorAll('input[type=checkbox][name="key"]').forEach((c) => { c.checked = true; });
      updateSelection();
    }
    function renderMatching(n, boxes) {
      const button = (text, onClick) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "secondary";
        b.textContent = text;
        b.addEventListener("click", onClick);
        return b;
      };
      if (matching) {
        matchBox.hidden = false;
        matchBox.replaceChildren(`All ${matching.length.toLocaleString()} matching entries are selected.`,
          button("Clear selection", () => {
            clearMatching();
            rows.querySelectorAll('input[type=checkbox][name="key"]').forEach((c) => { c.checked = false; });
            updateSelection();
          }));
      } else if (n && n === boxes.length && shownTotal > rows.querySelectorAll("tr:not(.run-divider)").length) {
        matchBox.hidden = false;
        matchBox.replaceChildren(`All ${n.toLocaleString()} entries shown are selected.`,
          button(view === "queue" ? "Select all matching entries" : `Select all ${shownTotal.toLocaleString()} matching`, selectMatching));
      } else {
        matchBox.hidden = true;
      }
    }
    window.selectedStatusKeys = () => matching
      || [...rows.querySelectorAll('input[type=checkbox][name="key"]:checked')].map((c) => c.value);

    function updateSelection() {
      const boxes = [...rows.querySelectorAll('input[type=checkbox][name="key"]')];
      if (matching && boxes.some((c) => !c.checked)) clearMatching();   // unticking one ends "all matching"
      const n = matching ? matching.length : boxes.filter((c) => c.checked).length;
      renderMatching(boxes.filter((c) => c.checked).length, boxes);
      needSelection.forEach((b) => {
        b.disabled = n === 0;
        b.textContent = n ? `${b.dataset.label.replace("…", "")} (${n.toLocaleString()})${b.dataset.label.endsWith("…") ? "…" : ""}` : b.dataset.label;
      });
      bulkForm.classList.toggle("has-selection", n > 0);   // phones: shows the selection bar
      const ticked = boxes.filter((c) => c.checked).length;
      selectAlls.forEach((s) => {
        s.checked = ticked > 0 && ticked === boxes.length;
        s.indeterminate = ticked > 0 && ticked < boxes.length;
      });
    }
    rows.addEventListener("change", (e) => { if (e.target.name === "key") updateSelection(); });
    selectAlls.forEach((selectAll) => selectAll.addEventListener("change", () => {
      rows.querySelectorAll('input[type=checkbox][name="key"]').forEach((c) => { c.checked = selectAll.checked; });
      updateSelection();
    }));

    section.querySelectorAll("[data-confirm-delete]").forEach((b) => b.addEventListener("click", (e) => {
      const picked = [...rows.querySelectorAll('input[type=checkbox][name="key"]:checked')];
      const count = matching ? matching.length : picked.length;
      const bytes = matching && matchingSizes ? matchingSizes.reduce((a, b) => a + b, 0)
        : picked.reduce((sum, c) => sum + Number(c.dataset.size || 0), 0);
      if (!confirm(`Delete ${count.toLocaleString()} original${count === 1 ? "" : "s"} (${gb(bytes)}) permanently? This cannot be undone.`)) e.preventDefault();
    }));
    section.querySelectorAll("form[data-confirm]").forEach((f) => f.addEventListener("submit", (e) => {
      if (!confirm(f.dataset.confirm)) e.preventDefault();
    }));

    // Changing the order of waiting entries (Queue tab): the arrow buttons and dragging a row's
    // handle. Ticked rows move together when the row you grab is ticked. "Move to top / bottom"
    // are ordinary buttons of the #bulk form.
    async function move(op, keys, target) {
      const body = new URLSearchParams({ csrf: bulkForm.querySelector("input[name=csrf]").value, op, json: "1", target: target || "" });
      keys.forEach((k) => body.append("key", k));
      const r = await fetch(moveUrl, { method: "POST", body });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) alert(j.message || "The entries could not be moved.");
      await reloadQueue();
    }
    const keysOf = (tr) => {
      const box = tr.querySelector('input[type=checkbox][name="key"]');
      return box && box.checked ? [...rows.querySelectorAll('input[type=checkbox][name="key"]:checked')].map((c) => c.value) : [tr.dataset.key];
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
      return q("[data-queue-more]") ? null : "";
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

    // "Replace all finished…" and "Replace selected…" in the Awaiting review tab (static/bulkreplace.js)
    const replaceAll = q("[data-bulk-replace-all]"), replaceSelected = q("[data-bulk-replace-selected]");
    if (replaceAll) replaceAll.addEventListener("click", () => {
      const all = new FormData();
      all.append("all", "1");
      window.BulkReplace.open(all, bulk);
    });
    if (replaceSelected) replaceSelected.addEventListener("click", () => {
      const picked = new FormData();
      window.selectedStatusKeys().forEach((k) => picked.append("key", k));
      window.BulkReplace.open(picked, bulk);
    });

    // The result filter and the search reload the list; the page address keeps them for reloads,
    // and the column sort links carry the search on
    function filtersChanged() {
      clearMatching();
      section.querySelectorAll("[data-state]").forEach((i) => { i.value = filter ? filter.value : ""; });   // "Dismiss" applies to what is shown
      section.querySelectorAll("[data-q]").forEach((i) => { i.value = search.value.trim(); });
      const url = new URL(location.href);
      for (const [k, v] of [["state", filter ? filter.value : ""], ["q", search.value.trim()]]) {
        if (v) url.searchParams.set(k, v); else url.searchParams.delete(k);
      }
      history.replaceState(history.state, "", url);
      shownUrl = keyOf(url);
      section.querySelectorAll("a.sort-link, a.sort-chip").forEach((a) => {
        const u = new URL(a.href);
        if (search.value.trim()) u.searchParams.set("q", search.value.trim()); else u.searchParams.delete("q");
        a.href = u;
      });
      reloadQueue(false);
    }
    if (filter) filter.addEventListener("change", filtersChanged);
    let searchTimer = null;
    search.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(filtersChanged, 300); });
    filterForm.addEventListener("submit", (e) => { e.preventDefault(); clearTimeout(searchTimer); filtersChanged(); });
    const more = q("[data-queue-more]");
    if (more) more.addEventListener("click", () => reloadQueue(true));

    t.destroy = () => { selObs.disconnect(); clearTimeout(searchTimer); };
    updateSelection();
    return t;
  }

  // Phones: the More menu closes on a click outside it, after choosing an action, and with Esc
  document.addEventListener("click", (e) => {
    if (!T || !T.moreMenu.classList.contains("open") || T.moreBtn.contains(e.target)) return;
    if (!T.moreMenu.contains(e.target) || e.target.closest("button")) T.setMore(false);   // outside, or an action was chosen
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && T && T.moreMenu.classList.contains("open")) { T.setMore(false); T.moreBtn.focus(); }
  });

  // Encodes in several steps (2-pass, subtitle scan): one bar per step, same markup as status.html
  function renderSteps(cur) {
    const box = $("[data-cur-steps]");
    const steps = cur.steps || [];
    box.hidden = !steps.length;
    $("[data-cur-overall]").hidden = !steps.length;
    const active = steps.find((s) => s.state === "active");
    const short = $("[data-cur-step-short]");
    short.hidden = !active;
    short.textContent = active ? `${cur.step_short} · ${Math.round(active.pct)} % · ` : "";
    box.replaceChildren(...steps.map((s) => {
      const row = document.createElement("div");
      row.className = "now-step " + s.state;
      const label = document.createElement("span");
      label.className = "step-label";
      label.textContent = s.label;
      const meter = document.createElement("span");
      meter.className = "meter";
      const fill = document.createElement("span");
      fill.style.width = s.pct + "%";
      meter.append(fill);
      const info = document.createElement("span");
      info.className = "step-info";
      info.textContent = s.state === "done" ? "done" + (s.took ? " · " + s.took : "")
        : s.state === "active" ? `${Math.round(s.pct)} %` + (s.eta ? ` · ${s.eta} left` : "") : "next";
      row.append(label, meter, info);
      return row;
    }));
  }

  let lastCounts = null;                        // tab counts of the previous poll: a kept tab whose count changed is dropped
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
      setText("[data-paused-held]", s.paused_held);
      if (cur) {
        setText("[data-cur-i]", cur.i);
        setText("[data-cur-n]", cur.n);
        setText("[data-cur-where]", cur.where);
        setText("[data-cur-name]", cur.file);
        setText("[data-cur-pct]", cur.pct || "0");
        setText("[data-cur-fps]", cur.fps || "–");
        setText("[data-cur-eta]", cur.eta || "–");
        $("[data-cur-about]").hidden = !cur.eta_about;
        $("[data-cur-bar]").style.width = (cur.pct || 0) + "%";
        renderSteps(cur);
      }
      document.querySelectorAll("[data-count]").forEach((el) => {
        el.textContent = (s.counts[el.dataset.count] || 0).toLocaleString();
      });
      if (lastCounts) Object.keys(s.counts).forEach((v) => { if (s.counts[v] !== lastCounts[v]) forget(v); });
      lastCounts = s.counts;
      const removeAll = $("[data-remove-all]");
      if (removeAll) removeAll.disabled = !s.counts.waiting;
      window.renderScanProgress(s.scan, !!s.scan, s.scan_result);
      const key = cur ? cur.key : "";
      if (key !== currentKey) forget("queue");   // the next file started: the queue's order changed
      const count = s.counts[T.view] || 0;
      const changed = T.viewCount !== null && count !== T.viewCount;
      T.viewCount = count;
      if (force || key !== currentKey || changed) {   // next file started, or an entry came or went
        currentKey = key;
        await T.reloadQueue(false);
      }
    } finally {
      running = false;
      if (again) { again = false; poll(true); }
    }
  }

  // ─── Switching tabs in place ───────────────────────────────────────────────
  const KEEP_MS = 120000;                       // how long a fetched tab may be shown without asking again
  const kept = new Map();                       // URL -> { html, at, view } of a tab's section
  const loading = new Map();                    // URL -> promise of a fetch that's running
  // A page address as a key: "/status" alone is the Queue tab
  const keyOf = (href) => {
    const u = new URL(href, location.href);
    if (!u.searchParams.has("view")) u.searchParams.set("view", "queue");
    return u.pathname + u.search;
  };
  let shownUrl = keyOf(location.href), navToken = 0;
  function forget(view) {
    for (const [k, e] of kept) if (e.view === view) kept.delete(k);
  }

  // The section of the page at url, as HTML (null: not a Status page, e.g. the sign-in page)
  function fetchTab(url) {
    if (loading.has(url)) return loading.get(url);
    const p = (async () => {
      const r = await fetch(url, { headers: { Accept: "text/html" } });
      if (!r.ok || r.redirected) return null;
      const doc = new DOMParser().parseFromString(await r.text(), "text/html");
      const sec = doc.querySelector("[data-status-tab]");
      const form = sec && sec.querySelector("[data-queue-filter]");
      if (!form) return null;
      const entry = { html: sec.outerHTML, at: Date.now(), view: form.dataset.view };
      kept.set(url, entry);
      return entry;
    })().catch(() => null).finally(() => loading.delete(url));
    loading.set(url, p);
    return p;
  }
  function fresh(url) {
    const e = kept.get(url);
    return e && Date.now() - e.at < KEEP_MS ? e : null;
  }
  const tabUrls = () => [...document.querySelectorAll(".view-tabs a")].map((a) => keyOf(a.href));
  function prefetch(url) {
    if (url !== shownUrl && !fresh(url) && !document.hidden) fetchTab(url);
  }

  function swap(html, focusView) {
    const tpl = document.createElement("template");
    tpl.innerHTML = html;
    const sec = tpl.content.querySelector("[data-status-tab]");
    T.destroy();
    T.section.replaceWith(sec);
    if (window.localizeTimes) window.localizeTimes(sec);
    T = initTab(sec);
    if (focusView) {                             // keyboard: stay on the tab strip
      const a = sec.querySelector(`.view-tabs [data-count="${focusView}"]`);
      if (a) a.closest("a").focus({ preventScroll: true });
    }
  }

  // Show the page at href in place. cache: a kept copy may be used (tab links; not for sorting)
  async function go(href, { push = true, cache = true, focus = false } = {}) {
    const url = keyOf(href);
    const token = ++navToken;
    const clicked = [...T.section.querySelectorAll(".view-tabs a")].find((a) => keyOf(a.href) === url);
    let entry = cache ? fresh(url) : null;
    const fromKept = !!entry;
    if (!entry) {
      if (clicked) {                             // at once: the chosen tab is marked, the list dims
        T.section.querySelectorAll(".view-tabs a").forEach((a) => {
          if (a === clicked) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
        });
      }
      const dim = setTimeout(() => T.section.classList.add("tab-loading"), 120);
      if (!cache) forget(T.view);                // sorting: the server remembers it, kept copies of this tab are outdated
      entry = await fetchTab(url);
      clearTimeout(dim);
      if (token !== navToken) return;            // another tab was chosen meanwhile
      if (!entry) { location.href = href; return; }
    }
    if (push && url !== shownUrl) history.pushState({ statusTab: url }, "", url);
    shownUrl = url;
    const focusView = focus && clicked ? entry.view : null;
    swap(entry.html, focusView);
    if (fromKept) T.reloadQueue(false);          // a kept copy: its list may be a little old
    const left = tabUrls().filter((u) => u !== url);   // the tab just left, and the others, ready for next time
    setTimeout(() => left.forEach(prefetch), 300);
  }

  document.addEventListener("click", (e) => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const a = e.target.closest && e.target.closest("[data-status-tab] .view-tabs a, [data-status-tab] a.sort-link, [data-status-tab] a.sort-chip");
    if (!a || new URL(a.href).origin !== location.origin) return;
    e.preventDefault();
    const tab = !!a.closest(".view-tabs");
    go(a.href, { cache: tab, focus: tab && e.detail === 0 });   // detail 0: Enter on the keyboard
  });
  // Pointing at a tab (or touching it) fetches it ahead
  const ahead = (e) => {
    const a = e.target.closest && e.target.closest("[data-status-tab] .view-tabs a");
    if (a) prefetch(keyOf(a.href));
  };
  document.addEventListener("pointerover", ahead);
  document.addEventListener("touchstart", ahead, { passive: true });
  document.addEventListener("focusin", ahead);
  window.addEventListener("popstate", () => {
    const url = keyOf(location.href);
    if (url !== shownUrl) go(url, { push: false });      // (a change of the #hash alone keeps the tab)
  });
  history.replaceState({ statusTab: shownUrl }, "", location.href);

  // "Replace…" on finished encodes (static/replace.js). The lists refresh whenever a replace
  // moves on (event of static/replaceprogress.js), up to Plex's check, and when all are done.
  const msg = $("[data-replace-msg]");
  const say = (text, kind) => { msg.hidden = false; msg.className = "scan-msg " + kind; msg.textContent = text; };
  window.ReplaceDialog.attach(document, { say, done: () => poll(true) });
  document.addEventListener("replaceprogress", () => poll(true));
  const bulk = { say, done: () => poll(true) };   // "Replace … selected / all finished" (initTab)

  T = initTab($("[data-status-tab]"));
  window.pullRefresh = () => poll(true);       // installed app: pull down at the top (static/phone.js)
  // Phones: the current encode is one line that stays at the top; tapping it shows everything
  const curToggle = $("[data-cur-toggle]");
  curToggle.addEventListener("click", () => {
    const open = panel.classList.toggle("open");
    curToggle.setAttribute("aria-expanded", String(open));
  });
  setInterval(() => { if (liveBox.checked && !document.hidden) poll(false); }, 3000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && liveBox.checked) poll(false); });
  $("[data-refresh]").addEventListener("click", () => poll(true));
  // The other tabs, fetched once the page has settled
  const idle = window.requestIdleCallback || ((f) => setTimeout(f, 1500));
  setTimeout(() => idle(() => tabUrls().forEach(prefetch)), 1000);
})();
