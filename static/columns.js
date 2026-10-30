// Columns of <table data-resizable="name"> (the Status tabs), set per table name and kept in
// localStorage, so every browser remembers its own choice:
// - Width: every header cell with data-col gets a handle at its right edge. Dragging it (or
//   focusing it and pressing ←/→, Shift for bigger steps) switches the table to fixed column
//   widths, measured from how it looks at that moment; the column th.col-rest takes the rest. No
//   column gets narrower than its CSS min-width, and the table never gets narrower than all its
//   columns together (the frame around it scrolls instead). A double-click on a handle, or
//   "Reset widths" in the menu, goes back to the automatic layout. Phones stack the rows and
//   ignore the widths.
// - Shown columns: the Columns menu (in th.col-rest; on phones in [data-col-menu-slot]) hides
//   columns. data-col="file" always stays. Hiding is a generated stylesheet (nth-child), so rows
//   loaded later follow it too.
(function () {
  const desktop = window.matchMedia("(min-width: 721px)");
  const load = (key, empty) => { try { return JSON.parse(localStorage.getItem(key)) || empty; } catch (e) { return empty; } };
  const store = (key, value) => {
    try { value === null ? localStorage.removeItem(key) : localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* storage blocked */ }
  };

  function setup(table, n) {
    const name = table.dataset.resizable;
    const widthKey = `colwidths:${name}`, hideKey = `colhidden:${name}`;
    const cols = [...table.querySelectorAll("thead th[data-col]")];
    if (!cols.length) return;
    const minOf = (th) => parseFloat(getComputedStyle(th).minWidth) || 40;
    let widths = load(widthKey, {});
    const hidden = new Set(load(hideKey, []).filter((c) => c !== "file" && cols.some((th) => th.dataset.col === c)));
    table.dataset.colTable = n;
    const sheet = document.createElement("style");
    document.head.append(sheet);

    function showColumns() {
      const headCells = [...table.querySelector("thead tr").children];
      sheet.textContent = [...hidden].map((c) => {
        const i = headCells.findIndex((th) => th.dataset.col === c) + 1;
        return i ? `table[data-col-table="${n}"] tr > :nth-child(${i}):not([colspan]) { display: none; }` : "";
      }).join("\n");
      if (table.classList.contains("fixed-cols")) updateTotal();
    }

    // The table must stay at least as wide as its columns: all fixed ones plus the filler's minimum
    function updateTotal() {
      let sum = 0;
      table.querySelectorAll("thead th").forEach((th) => {
        if (hidden.has(th.dataset.col)) return;
        sum += th.classList.contains("col-rest") ? minOf(th)
          : th.dataset.col ? widths[th.dataset.col] || th.getBoundingClientRect().width
          : th.getBoundingClientRect().width;
      });
      table.style.setProperty("--cols-w", `${Math.ceil(sum)}px`);
    }
    function apply() {
      cols.forEach((th) => {
        const w = widths[th.dataset.col];
        if (w) widths[th.dataset.col] = Math.max(w, minOf(th));
        th.style.width = w ? `${widths[th.dataset.col]}px` : "";
      });
      table.classList.add("fixed-cols");
      updateTotal();
    }
    function freeze() {                         // take over the automatic widths as they are now
      if (table.classList.contains("fixed-cols")) return;
      cols.forEach((th) => {
        if (!widths[th.dataset.col] && !hidden.has(th.dataset.col)) widths[th.dataset.col] = th.getBoundingClientRect().width;
      });
      apply();
    }
    function reset() {
      widths = {};
      store(widthKey, null);
      cols.forEach((th) => { th.style.width = ""; });
      table.classList.remove("fixed-cols");
      table.style.removeProperty("--cols-w");
    }
    function resize(th, w) {
      widths[th.dataset.col] = Math.round(Math.max(w, minOf(th)));
      th.style.width = `${widths[th.dataset.col]}px`;
      updateTotal();
    }

    cols.forEach((th) => {
      const handle = document.createElement("span");
      handle.className = "col-resize";
      handle.tabIndex = 0;
      handle.setAttribute("role", "separator");
      handle.setAttribute("aria-orientation", "vertical");
      handle.setAttribute("aria-label", `Resize the column ${th.dataset.label || th.dataset.col}`);
      handle.title = "Drag to change the width · double-click: automatic widths";
      th.append(handle);

      handle.addEventListener("pointerdown", (e) => {
        if (!desktop.matches || e.button !== 0) return;
        e.preventDefault();
        freeze();
        const x0 = e.clientX, w0 = widths[th.dataset.col] || th.getBoundingClientRect().width;
        handle.setPointerCapture(e.pointerId);
        table.classList.add("resizing");
        const move = (ev) => resize(th, w0 + ev.clientX - x0);
        const up = () => {
          handle.removeEventListener("pointermove", move);
          table.classList.remove("resizing");
          store(widthKey, widths);
        };
        handle.addEventListener("pointermove", move);
        handle.addEventListener("pointerup", up, { once: true });
        handle.addEventListener("pointercancel", up, { once: true });
      });
      handle.addEventListener("click", (e) => e.preventDefault());
      handle.addEventListener("dblclick", (e) => { e.preventDefault(); reset(); });
      handle.addEventListener("keydown", (e) => {
        if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
        e.preventDefault();
        freeze();
        const step = (e.shiftKey ? 64 : 16) * (e.key === "ArrowLeft" ? -1 : 1);
        resize(th, (widths[th.dataset.col] || th.getBoundingClientRect().width) + step);
        store(widthKey, widths);
      });
    });

    // The Columns menu
    const menu = document.createElement("details");
    menu.className = "col-menu";
    const summary = document.createElement("summary");
    summary.className = "icon-btn";
    summary.setAttribute("aria-label", "Columns");
    summary.dataset.tip = "Choose the columns to show";
    summary.innerHTML = '<svg class="i" aria-hidden="true"><use href="#i-columns"/></svg><span class="col-menu-label">Columns</span>';
    const panel = document.createElement("div");
    panel.className = "col-menu-panel";
    const title = document.createElement("p");
    title.className = "col-menu-title";
    title.textContent = "Show columns";
    panel.append(title);
    cols.forEach((th) => {
      const label = document.createElement("label");
      label.className = "check";
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = !hidden.has(th.dataset.col);
      box.disabled = th.dataset.col === "file";
      box.addEventListener("change", () => {
        if (box.checked) hidden.delete(th.dataset.col); else hidden.add(th.dataset.col);
        store(hideKey, [...hidden]);
        showColumns();
      });
      label.append(box, ` ${th.dataset.label || th.dataset.col}`);
      panel.append(label);
    });
    const resetBtn = document.createElement("button");
    resetBtn.type = "button";
    resetBtn.className = "secondary col-menu-reset";
    resetBtn.textContent = "Reset widths";
    resetBtn.title = "Back to the automatic column widths";
    resetBtn.addEventListener("click", () => { reset(); menu.open = false; });
    panel.append(resetBtn);
    menu.append(summary, panel);
    document.addEventListener("click", (e) => { if (menu.open && !menu.contains(e.target)) menu.open = false; });
    menu.addEventListener("keydown", (e) => { if (e.key === "Escape" && menu.open) { menu.open = false; summary.focus(); } });

    const headSlot = table.querySelector("thead th.col-rest");
    const phoneSlot = document.querySelector("[data-col-menu-slot]");
    const place = () => {                       // phones hide the table header: the menu goes to the sort bar
      const slot = desktop.matches || !phoneSlot ? headSlot : phoneSlot;
      if (slot && menu.parentElement !== slot) slot.append(menu);
    };
    place();
    desktop.addEventListener("change", place);

    showColumns();
    if (Object.keys(widths).length && desktop.matches) {
      cols.forEach((th) => {           // columns without a stored width keep their current one
        if (!widths[th.dataset.col] && !hidden.has(th.dataset.col)) widths[th.dataset.col] = th.getBoundingClientRect().width;
      });
      apply();
    }
  }

  document.querySelectorAll("table[data-resizable]").forEach((t, i) => setup(t, i + 1));
})();
