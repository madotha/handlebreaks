// Preset editor: tabs (with a dot when a tab has unsaved changes), choices that depend on
// another field (e.g. tunes per encoder), fields shown only for some values (custom filter
// parameters, …), descriptions (the (i) buttons and the text under a chosen option), quick
// size buttons, and audio tracks that can be added and removed.
(function () {
  const form = document.querySelector("[data-preset-editor]");
  if (!form) return;

  // Tabs; the open tab is kept in the URL (#Video), so reloads and saves come back to it
  const tabs = [...form.querySelectorAll("[data-tab]")];
  const panels = [...form.querySelectorAll("[data-tab-panel]")];
  function openTab(name) {
    if (!panels.some((p) => p.dataset.tabPanel === name)) name = panels[0].dataset.tabPanel;
    panels.forEach((p) => { p.hidden = p.dataset.tabPanel !== name; });
    tabs.forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    history.replaceState(null, "", "#" + encodeURIComponent(name));
    form.action = location.pathname + location.hash;
  }
  tabs.forEach((t) => t.addEventListener("click", () => openTab(t.dataset.tab)));
  openTab(decodeURIComponent(location.hash.slice(1)));

  // The value of the field with data-field=key, looked up in the same audio track first
  function valueOf(scope, key) {
    const el = (scope.closest("[data-track]") || form).querySelector(`[data-field="${key}"]`) || form.querySelector(`[data-field="${key}"]`);
    if (!el) return "";
    return el.type === "checkbox" ? (el.checked ? "1" : "") : el.value;
  }

  function refillDependent(sel) {
    const map = JSON.parse(sel.dataset.map);
    const list = map[valueOf(sel, sel.dataset.depends)] || [];
    const current = sel.value || sel.dataset.current;
    sel.innerHTML = "";
    const values = list.map((o) => o[0]);
    if (current && !values.includes(current) && !list.length) sel.add(new Option(current, current, true, true));
    list.forEach(([v, l]) => sel.add(new Option(l, v, false, v === current)));
    if (list.length && !values.includes(current)) {   // e.g. another encoder: pick its default
      sel.value = values.includes("medium") ? "medium" : values.includes("auto") ? "auto" : values.includes("") ? "" : values[Math.floor(values.length / 2)];
    }
    showOptionTip(sel);
  }

  // The description of the chosen option under a dropdown (and as the hover text of each option)
  function showOptionTip(sel) {
    if (!sel.dataset.optionTips) return;
    const tips = JSON.parse(sel.dataset.optionTips);
    const p = sel.closest(".field").querySelector("[data-option-tip]");
    const text = tips[sel.value] || "";
    p.textContent = text;
    p.hidden = !text;
    [...sel.options].forEach((o) => { if (tips[o.value]) o.title = tips[o.value]; else o.removeAttribute("title"); });
  }

  function syncShown() {
    form.querySelectorAll("[data-show]").forEach((box) => {
      const rules = JSON.parse(box.dataset.show);
      box.hidden = !Object.entries(rules).every(([key, wanted]) => {
        const v = valueOf(box, key);
        const yes = wanted.filter((w) => !w.startsWith("!")), no = wanted.filter((w) => w.startsWith("!")).map((w) => w.slice(1));
        return (!yes.length || yes.includes(v)) && !no.includes(v);
      });
    });
    form.querySelectorAll("[data-section]").forEach((sec) => {        // a heading only shows while one of its fields does
      sec.hidden = ![...sec.querySelectorAll(".field")].some((f) => !f.hidden);
    });
  }

  function init(root) {
    root.querySelectorAll("select[data-depends]").forEach(refillDependent);
    root.querySelectorAll("select[data-option-tips]").forEach(showOptionTip);
    root.querySelectorAll("[data-size]").forEach((b) => b.addEventListener("click", () => {
      const [w, h] = b.dataset.size.split("x");
      form.querySelector('[data-field="PictureWidth"]').value = w;
      form.querySelector('[data-field="PictureHeight"]').value = h;
    }));
  }
  form.addEventListener("change", (e) => {
    const key = e.target.dataset && e.target.dataset.field;
    if (key) {
      const scope = e.target.closest("[data-track]") || form;
      scope.querySelectorAll(`select[data-depends="${key}"]`).forEach(refillDependent);
    }
    if (e.target.matches("select[data-option-tips]")) showOptionTip(e.target);
    syncShown();
  });

  // The (i) button shows the description of a setting below it (the only way on phones)
  form.addEventListener("click", (e) => {
    const b = e.target.closest("[data-info]");
    if (!b) return;
    const text = b.closest(".field").querySelector("[data-tip-text]");
    text.hidden = !text.hidden;
    b.setAttribute("aria-expanded", String(!text.hidden));
  });

  // Audio tracks
  const list = form.querySelector("[data-tracks]");
  const tpl = form.querySelector("[data-track-template]");
  function renumber() {
    [...list.children].forEach((t, n) => {
      t.querySelector("[data-track-no]").textContent = n + 1;
      t.querySelectorAll("[name]").forEach((el) => { el.name = el.name.replace(/^a(\d+|__)-/, `a${n}-`); });
      t.querySelectorAll("[id]").forEach((el) => { el.id = el.id.replace(/^f-a(\d+|__)-/, `f-a${n}-`); });
      t.querySelectorAll("label[for]").forEach((el) => { el.htmlFor = el.htmlFor.replace(/^f-a(\d+|__)-/, `f-a${n}-`); });
    });
    form.querySelector("[data-track-count]").value = list.children.length;
  }
  if (list) {
    form.querySelector("[data-track-add]").addEventListener("click", () => {
      list.append(tpl.content.cloneNode(true));
      init(list.lastElementChild);
      renumber();
      syncShown();
      markChanges();
    });
    list.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-track-remove]");
      if (btn) { btn.closest("[data-track]").remove(); renumber(); markChanges(); }
    });
    form.addEventListener("submit", renumber);
  }

  init(form);
  syncShown();

  // A dot on each tab whose fields differ from what was loaded (i.e. from the saved preset)
  const state = (panel) => [...panel.querySelectorAll("input, select, textarea")]
    .map((el) => `${el.name}=${el.type === "checkbox" ? (el.checked ? 1 : 0) : el.value}`).join("&");
  const saved = new Map(panels.map((p) => [p, state(p)]));
  function markChanges() {
    panels.forEach((p, n) => { tabs[n].querySelector("[data-tab-dot]").hidden = state(p) === saved.get(p); });
  }
  form.addEventListener("input", markChanges);
  form.addEventListener("change", markChanges);
})();
