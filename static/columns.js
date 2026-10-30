// Columns of the tables <table data-resizable="name">: the Status tabs ("status:<view>") and
// every file table of the Files page ("files", one setting for all folder levels, also the ones
// loaded later). Kept per name in localStorage, so every browser remembers its own choice.
// - Width: every header cell with data-col gets a handle at its right edge. Dragging it (or
//   focusing it and pressing ←/→, Shift for bigger steps) switches the tables to fixed column
//   widths, measured from how they look at that moment; the column th.col-rest takes the rest
//   (Status: the buttons, Files: the file name). No column gets narrower than its CSS --col-min
//   (rem), and a table never gets narrower than its columns together (its frame scrolls
//   instead). Double-click on a handle or "Reset widths" goes back to automatic widths.
//   Phones stack the rows and ignore the widths.
// - Shown columns: the Columns menu (rendered into [data-col-menu="name"]) hides columns;
//   th[data-hidden-default] are hidden until the user chooses, data-col="file" always stays.
// Everything is applied by one generated stylesheet per name, so rows and tables loaded later
// follow it without any work. The frame of a table (.table-wrap) only scrolls sideways while the
// table is wider than it (.scroll-x), so the sticky table header works the rest of the time.
(function () {
  const load = (key, empty) => { try { return JSON.parse(localStorage.getItem(key)) || empty; } catch (e) { return empty; } };
  const store = (key, value) => {
    try { value === null ? localStorage.removeItem(key) : localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* storage blocked */ }
  };
  const groups = {};
  const desktop = window.matchMedia("(min-width: 721px)");

  function group(name, table) {
    if (groups[name]) return groups[name];
    const heads = [...table.querySelectorAll("thead th")];
    const cols = heads.filter((th) => th.dataset.col && !th.classList.contains("col-rest"));
    const all = heads.filter((th) => th.dataset.col);
    const g = {
      name, sel: `table[data-resizable="${name}"]`,
      widthKey: `colwidths:${name}`, hideKey: `colhidden:${name}`,
      cols: cols.map((th) => th.dataset.col),
      labels: Object.fromEntries(all.map((th) => [th.dataset.col, th.dataset.label || th.dataset.col])),
      index: Object.fromEntries(heads.map((th, i) => [th.dataset.col, i + 1]).filter(([c]) => c)),
      sheet: document.createElement("style"),
    };
    g.widths = load(g.widthKey, null) || {};
    const stored = load(g.hideKey, null);
    g.hidden = new Set((stored || all.filter((th) => th.hasAttribute("data-hidden-default")).map((th) => th.dataset.col))
      .filter((c) => c !== "file" && c in g.index));
    document.head.append(g.sheet);
    groups[name] = g;
    return g;
  }

  // A table of the group that is shown (the Files page also has a hidden one with the column list)
  const shownTable = (g) => [...document.querySelectorAll(g.sel)].find((t) => t.offsetParent) || null;
  const remPx = () => parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
  const colMin = (th) => (parseFloat(getComputedStyle(th).getPropertyValue("--col-min")) || 2.5) * remPx();
  function minOf(g, col) {
    const table = shownTable(g);
    const th = table && table.querySelector(`th[data-col="${col}"]`);
    return th ? colMin(th) : 40;
  }

  // The generated rules: hidden columns everywhere, fixed widths on wide screens
  function render(g) {
    const rules = [...g.hidden].map((c) => `${g.sel} tr > :nth-child(${g.index[c]}):not([colspan]) { display: none; }`);
    const fixed = Object.keys(g.widths).length > 0;
    if (fixed) {
      const T = g.sel;
      const table = shownTable(g);
      let sum = 0;
      if (table) {
        table.querySelectorAll("thead th").forEach((th) => {
          const c = th.dataset.col;
          if (g.hidden.has(c)) return;
          sum += th.classList.contains("col-rest") ? colMin(th)
            : c && g.widths[c] ? g.widths[c] : th.getBoundingClientRect().width;
        });
      }
      const inner = [`${T} { table-layout: fixed; min-width: ${Math.ceil(sum)}px; }`,
        `${T} td.name { min-width: 0; overflow-wrap: anywhere; }`,
        `${T} th.actions:not(.col-rest) { width: 5.2rem; }`,       // room for two buttons; only th.col-rest takes the rest
        `${T} td:not(.sel):not(.check):not(.grip):not(.actions):not(.move) { overflow: hidden; text-overflow: ellipsis; }`,
        ...Object.entries(g.widths).map(([c, w]) => `${T} th[data-col="${c}"] { width: ${w}px; }`)];
      rules.push(`@media (min-width: 721px) {\n  ${inner.join("\n  ")}\n}`);
    }
    g.sheet.textContent = rules.join("\n");
    document.querySelectorAll(g.sel).forEach(checkScroll);
  }

  function freeze(g, table) {                   // take over the automatic widths as they are now
    if (Object.keys(g.widths).length) return;
    g.cols.forEach((c) => {
      const th = table.querySelector(`thead th[data-col="${c}"]`);
      if (th && !g.hidden.has(c)) g.widths[c] = Math.round(th.getBoundingClientRect().width);
    });
  }
  function setWidth(g, col, w) {
    g.widths[col] = Math.round(Math.max(w, minOf(g, col)));
    render(g);
  }
  function reset(g) {
    g.widths = {};
    store(g.widthKey, null);
    render(g);
  }

  // Sideways scrolling only while needed (an element that scrolls breaks the sticky header)
  const watched = new WeakSet();
  const resizeObs = new ResizeObserver((entries) => entries.forEach((e) => checkScroll(e.target.querySelector("table") || e.target)));
  function checkScroll(table) {
    const wrap = table && table.closest(".table-wrap");
    if (!wrap) return;
    wrap.classList.toggle("scroll-x", table.offsetWidth > wrap.clientWidth + 1);   // (not scrollWidth: a handle may stick out)
  }

  function decorate(table) {
    if (table.dataset.colsReady) return;
    table.dataset.colsReady = "1";
    const g = group(table.dataset.resizable, table);
    table.querySelectorAll("thead th[data-col]:not(.col-rest)").forEach((th) => {
      const handle = document.createElement("span");
      handle.className = "col-resize";
      handle.tabIndex = 0;
      handle.setAttribute("role", "separator");
      handle.setAttribute("aria-orientation", "vertical");
      handle.setAttribute("aria-label", `Resize the column ${g.labels[th.dataset.col]}`);
      handle.title = "Drag to change the width · double-click: automatic widths";
      th.append(handle);
    });
    const wrap = table.closest(".table-wrap");
    if (wrap && !watched.has(wrap)) { watched.add(wrap); resizeObs.observe(wrap); }
    if (!g.rendered) { g.rendered = true; render(g); } else checkScroll(table);
    document.querySelectorAll(`[data-col-menu="${g.name}"]`).forEach((slot) => menu(g, slot));
  }

  function menu(g, slot) {
    if (slot.firstChild) return;
    const details = document.createElement("details");
    details.className = "col-menu";
    const summary = document.createElement("summary");
    summary.className = "col-menu-btn";
    summary.setAttribute("aria-label", "Columns");
    summary.dataset.tip = "Choose the columns to show, reset their widths";
    summary.innerHTML = '<svg class="i" aria-hidden="true"><use href="#i-columns"/></svg><span>Columns</span>';
    const panel = document.createElement("div");
    panel.className = "col-menu-panel";
    const title = document.createElement("p");
    title.className = "col-menu-title";
    title.textContent = "Show columns";
    panel.append(title);
    Object.entries(g.labels).forEach(([c, text]) => {
      const label = document.createElement("label");
      label.className = "check";
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = !g.hidden.has(c);
      box.disabled = c === "file";
      box.addEventListener("change", () => {
        if (box.checked) g.hidden.delete(c); else g.hidden.add(c);
        store(g.hideKey, [...g.hidden]);
        render(g);
      });
      label.append(box, ` ${text}`);
      panel.append(label);
    });
    const resetBtn = document.createElement("button");
    resetBtn.type = "button";
    resetBtn.className = "secondary col-menu-reset";
    resetBtn.textContent = "Reset widths";
    resetBtn.title = "Back to the automatic column widths";
    resetBtn.addEventListener("click", () => { reset(g); details.open = false; });
    panel.append(resetBtn);
    details.append(summary, panel);
    slot.append(details);
    details.addEventListener("toggle", () => {            // another menu of the same table may have changed it
      if (details.open) panel.querySelectorAll("input").forEach((box, i) => { box.checked = !g.hidden.has(Object.keys(g.labels)[i]); });
    });
    document.addEventListener("click", (e) => { if (details.open && !details.contains(e.target)) details.open = false; });
    details.addEventListener("keydown", (e) => { if (e.key === "Escape" && details.open) { details.open = false; summary.focus(); } });
  }

  // Handles work by event delegation, so tables loaded later need no listeners of their own
  const handleAt = (e) => e.target.closest && e.target.closest(".col-resize");
  const context = (handle) => {
    const table = handle.closest("table[data-resizable]");
    return { table, g: groups[table.dataset.resizable], col: handle.closest("th").dataset.col };
  };
  document.addEventListener("pointerdown", (e) => {
    const handle = handleAt(e);
    if (!handle || !desktop.matches || e.button !== 0) return;
    e.preventDefault();
    const { table, g, col } = context(handle);
    freeze(g, table);
    const x0 = e.clientX, w0 = g.widths[col] || handle.closest("th").getBoundingClientRect().width;
    handle.setPointerCapture(e.pointerId);
    document.documentElement.classList.add("col-resizing");
    const move = (ev) => setWidth(g, col, w0 + ev.clientX - x0);
    const up = () => {
      handle.removeEventListener("pointermove", move);
      document.documentElement.classList.remove("col-resizing");
      store(g.widthKey, g.widths);
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up, { once: true });
    handle.addEventListener("pointercancel", up, { once: true });
  });
  document.addEventListener("click", (e) => { if (handleAt(e)) e.preventDefault(); }, true);
  document.addEventListener("dblclick", (e) => {
    const handle = handleAt(e);
    if (handle) { e.preventDefault(); reset(context(handle).g); }
  });
  document.addEventListener("keydown", (e) => {
    const handle = handleAt(e);
    if (!handle || (e.key !== "ArrowLeft" && e.key !== "ArrowRight")) return;
    e.preventDefault();
    const { table, g, col } = context(handle);
    freeze(g, table);
    const step = (e.shiftKey ? 64 : 16) * (e.key === "ArrowLeft" ? -1 : 1);
    setWidth(g, col, (g.widths[col] || handle.closest("th").getBoundingClientRect().width) + step);
    store(g.widthKey, g.widths);
  });

  document.querySelectorAll("table[data-resizable]").forEach(decorate);
  new MutationObserver((records) => {
    for (const r of records) {
      r.addedNodes.forEach((n) => {
        if (n.nodeType !== 1) return;
        if (n.matches("table[data-resizable]")) decorate(n);
        n.querySelectorAll && n.querySelectorAll("table[data-resizable]").forEach(decorate);
      });
    }
  }).observe(document.body, { childList: true, subtree: true });
})();
