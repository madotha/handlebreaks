// ⓘ buttons (templates/_info.html): native popovers, which close on Esc and a click outside.
// On wider screens the popover is placed next to its button (below it, or above if there's no
// room); on phones CSS shows it as a sheet at the bottom. Browsers without popovers toggle .open.
(function () {
  const phone = window.matchMedia("(max-width: 720px)");
  const native = HTMLElement.prototype.hasOwnProperty("popover");

  function place(pop) {
    const btn = document.querySelector(`[data-info-btn][popovertarget="${pop.id}"]`);
    pop.style.removeProperty("top");
    pop.style.removeProperty("left");
    if (!btn || phone.matches) return;
    const r = btn.getBoundingClientRect(), p = pop.getBoundingClientRect();
    const left = Math.min(Math.max(8, r.left - 12), window.innerWidth - p.width - 8);
    const below = r.bottom + 6, above = r.top - p.height - 6;
    const top = below + p.height <= window.innerHeight - 8 || above < 8 ? below : above;
    pop.style.left = `${left}px`;
    pop.style.top = `${Math.max(8, top)}px`;
  }

  // Esc closes an open popover only, not the dialog it was opened in. WebKit closes the popover
  // before the page sees the key and then cancels the dialog too, so a dialog's cancel right
  // after a popover closed (same key press) is refused.
  let popClosed = 0;
  document.addEventListener("beforetoggle", (e) => {   // sync, unlike toggle
    if (e.target.classList && e.target.classList.contains("info-pop") && e.newState === "closed") popClosed = Date.now();
  }, true);
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || native) return;
    const open = document.querySelectorAll(".info-pop.open");
    if (!open.length) return;
    open.forEach((p) => p.classList.remove("open"));
    popClosed = Date.now();
    e.preventDefault();
  }, true);
  document.addEventListener("cancel", (e) => {
    const popOpen = native && document.querySelector(".info-pop:popover-open");
    if (e.target.tagName === "DIALOG" && (popOpen || Date.now() - popClosed < 50)) e.preventDefault();
  }, true);

  if (native) {
    document.addEventListener("toggle", (e) => {
      if (e.target.classList && e.target.classList.contains("info-pop") && e.newState === "open") place(e.target);
    }, true);
    // The page scrolls on: an open popover would float away from its button
    window.addEventListener("scroll", () => {
      if (phone.matches) return;
      document.querySelectorAll(".info-pop:popover-open").forEach((p) => p.hidePopover());
    }, { passive: true });
    return;
  }
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[popovertarget]");
    const openOnes = document.querySelectorAll(".info-pop.open");
    if (!btn) {
      if (!e.target.closest(".info-pop")) openOnes.forEach((p) => p.classList.remove("open"));
      return;
    }
    const pop = document.getElementById(btn.getAttribute("popovertarget"));
    if (!pop || !pop.classList.contains("info-pop")) return;
    const show = btn.getAttribute("popovertargetaction") !== "hide" && !pop.classList.contains("open");
    openOnes.forEach((p) => p.classList.remove("open"));
    if (show) { pop.classList.add("open"); place(pop); }
  });
})();
