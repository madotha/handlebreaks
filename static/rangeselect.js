// Selecting several rows at once in every list with checkboxes (Files, Status):
// - Shift+click ticks (or unticks) everything between the last clicked row and this one.
// - Press on a row's checkbox cell, drag over other rows and release: all rows in between
//   take the state the first one is getting. Mouse only; on touch screens dragging scrolls.
// Works on the visible, enabled checkboxes in `td.sel` / `td.check` cells in page order, so
// rows loaded later (folders, "Show more") need no setup.
(() => {
  const CELL = "td.sel, td.check";
  const BOX = "td.sel > input[type=checkbox], td.check > input[type=checkbox]";
  const visible = () => [...document.querySelectorAll(BOX)].filter((b) => !b.disabled && b.offsetParent !== null);
  const boxOf = (el) => {
    const cell = el && el.closest && el.closest(CELL);
    return cell ? cell.querySelector("input[type=checkbox]") : null;
  };
  const rowBox = (el) => {
    const tr = el && el.closest && el.closest("tr");
    return tr ? tr.querySelector(BOX) : null;
  };

  // Set the boxes between a and b (inclusive) to `on`; every other box in `base` keeps its
  // saved state. One bubbling change event afterwards lets the page recount.
  function apply(list, a, b, on, base) {
    const i = list.indexOf(a), j = list.indexOf(b);
    if (i < 0 || j < 0) return;
    const [lo, hi] = i < j ? [i, j] : [j, i];
    let last = null;
    list.forEach((box, k) => {
      const want = k >= lo && k <= hi ? on : (base ? base.get(box) : box.checked);
      if (box.checked !== want) { box.checked = want; last = box; }
    });
    if (last) last.dispatchEvent(new Event("change", { bubbles: true }));
  }

  let anchor = null;      // last box clicked, the start of a shift range
  let drag = null;        // { start, on, base, list, moved } while the mouse button is down

  document.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    const box = boxOf(e.target);
    if (!box || box.disabled) return;
    if (e.shiftKey) e.preventDefault();           // no text selection while shift-clicking
    const list = visible();
    drag = { start: box, on: !box.checked, list, base: new Map(list.map((b) => [b, b.checked])), moved: false };
  });

  document.addEventListener("mouseover", (e) => {
    if (!drag || !(e.buttons & 1)) return;
    const box = rowBox(e.target);
    if (!box || box.disabled) return;
    if (!drag.moved && box === drag.start) return;
    if (!drag.moved) {
      drag.moved = true;
      document.body.classList.add("range-dragging");
      getSelection().removeAllRanges();
    }
    apply(drag.list, drag.start, box, drag.on, drag.base);
  });

  document.addEventListener("selectstart", (e) => { if (drag && drag.moved) e.preventDefault(); });

  document.addEventListener("mouseup", () => {
    if (!drag) return;
    const d = drag;
    drag = null;
    document.body.classList.remove("range-dragging");
    if (!d.moved) return;
    anchor = d.start;
    // The click that follows a drag would toggle the first box again
    const eat = (e) => { e.preventDefault(); e.stopPropagation(); };
    document.addEventListener("click", eat, { capture: true, once: true });
    setTimeout(() => document.removeEventListener("click", eat, true), 0);
  });

  document.addEventListener("click", (e) => {
    const box = e.target.matches && e.target.matches(BOX) ? e.target : null;
    if (!box) return;
    if (e.shiftKey && anchor && anchor !== box && document.contains(anchor)) {
      apply(visible(), anchor, box, box.checked, null);
    }
    anchor = box;
  });
})();
