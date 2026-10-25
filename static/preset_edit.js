// Preset editor: tabs, choices that depend on another field (e.g. tunes per encoder),
// fields shown only for some values (custom filter parameters, …), quick size buttons,
// and audio tracks that can be added and removed.
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
  }

  function init(root) {
    root.querySelectorAll("select[data-depends]").forEach(refillDependent);
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
    syncShown();
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
    });
    list.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-track-remove]");
      if (btn) { btn.closest("[data-track]").remove(); renumber(); }
    });
    form.addEventListener("submit", renumber);
  }

  init(form);
  syncShown();
})();
