// Sorting for small tables that are fully on the page (<table data-sortable>): a click
// on a column header cycles ascending -> descending -> off (original order). Cells sort
// by data-sort if set (e.g. raw numbers behind "1.2 TB"), otherwise by their text;
// numbers numerically, text naturally; empty cells ("–") always last. Columns with
// data-nosort on their header aren't sortable. Big, paged lists sort on the server.
// The sort of each table is remembered in the browser (per page and table) and restored
// when the page is opened again.
(function () {
  const value = (cell) => {
    const raw = (cell.dataset.sort ?? cell.textContent).trim();
    if (raw === "" || raw === "–") return null;
    return raw !== "" && !isNaN(Number(raw)) ? Number(raw) : raw.toLowerCase();
  };
  const compare = (x, y) => (typeof x === "number" && typeof y === "number")
    ? x - y : String(x).localeCompare(String(y), undefined, { numeric: true });

  document.querySelectorAll("table[data-sortable]").forEach((table, index) => {
    const storeKey = `tablesort:${location.pathname}:${table.closest("[id]")?.id || index}`;
    const tbody = table.tBodies[0];
    const headers = [...table.tHead.rows[0].cells];
    const original = [...tbody.rows];
    let col = -1, dir = null;
    const remember = () => {
      try { localStorage.setItem(storeKey, JSON.stringify({ col, dir })); } catch (e) { /* storage blocked */ }
    };

    function render() {
      let rows = original.slice();
      if (dir) {
        const have = rows.filter((r) => value(r.cells[col]) !== null);
        const missing = rows.filter((r) => value(r.cells[col]) === null);
        have.sort((a, b) => compare(value(a.cells[col]), value(b.cells[col])) * (dir === "asc" ? 1 : -1));
        rows = have.concat(missing);
      }
      tbody.append(...rows);
      headers.forEach((th, i) => {
        if (!th.classList.contains("sortable-th")) return;
        const active = i === col && dir;
        th.setAttribute("aria-sort", active ? (dir === "asc" ? "ascending" : "descending") : "none");
        th.querySelector(".sort-arrow").textContent = active ? (dir === "asc" ? " ▲" : " ▼") : "";
      });
    }

    headers.forEach((th, i) => {
      if (th.hasAttribute("data-nosort")) return;
      th.classList.add("sortable-th");
      th.title = "Sort: ascending → descending → off";
      th.setAttribute("aria-sort", "none");
      th.insertAdjacentHTML("beforeend", '<span class="sort-arrow"></span>');
      th.addEventListener("click", () => {
        if (col !== i) { col = i; dir = "asc"; }
        else if (dir === "asc") dir = "desc";
        else { col = -1; dir = null; }
        render();
        remember();
      });
    });
    try {                                           // restore the remembered sort
      const saved = JSON.parse(localStorage.getItem(storeKey));
      if (saved && saved.dir && headers[saved.col]?.classList.contains("sortable-th")) {
        col = saved.col; dir = saved.dir; render();
      }
    } catch (e) { /* nothing remembered */ }
  });
})();
