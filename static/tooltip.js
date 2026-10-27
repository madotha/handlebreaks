// Tooltips for icon buttons: any element with data-tip shows its text right away on hover
// and on keyboard focus. The tooltip lives in <body>, so scrolling tables don't cut it off.
(function () {
  const tip = document.createElement("div");
  tip.className = "tooltip";
  tip.setAttribute("role", "tooltip");
  tip.hidden = true;
  document.body.append(tip);
  let target = null;

  function show(el) {
    target = el;
    tip.textContent = el.dataset.tip;
    tip.classList.toggle("wide", el.hasAttribute("data-info"));      // descriptions in the preset editor are longer
    tip.hidden = false;
    const r = el.getBoundingClientRect(), t = tip.getBoundingClientRect();
    const left = Math.min(Math.max(8, r.left + r.width / 2 - t.width / 2), window.innerWidth - t.width - 8);
    const above = r.top - t.height - 8;
    tip.style.left = `${left}px`;
    tip.style.top = `${above >= 8 ? above : r.bottom + 8}px`;
  }
  function hide() { target = null; tip.hidden = true; }

  const tipped = (e) => e.target.closest && e.target.closest("[data-tip]");
  document.addEventListener("mouseover", (e) => { const el = tipped(e); if (el && el !== target) show(el); else if (!el && target) hide(); });
  document.addEventListener("focusin", (e) => { const el = tipped(e); if (el && el.matches(":focus-visible")) show(el); });
  document.addEventListener("focusout", hide);
  document.addEventListener("scroll", hide, true);
  document.addEventListener("click", hide, true);
})();
