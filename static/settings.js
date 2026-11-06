// Settings pages: the save bar (Save only after a change, Discard, a warning when leaving with
// changes), dots on settings that differ from their default with "Reset to default", live
// folder checks, the write-only token field, the connection tests, the file name example, a live
// preview of the Appearance settings, and the share cards.
(function () {
  const root = document.documentElement;
  const form = document.querySelector("[data-settings-form]");

  // ─── Values ────────────────────────────────────────────────────────────────
  const snapshot = () => (form ? new URLSearchParams(new FormData(form)).toString() : "");

  function fieldValue(el) {
    const inputs = [...el.querySelectorAll("input, select")].filter((i) => i.name === el.dataset.field);
    switch (el.dataset.kind) {
      case "bool": return inputs[0] && inputs[0].checked ? "1" : "0";
      case "event_groups": return inputs.filter((i) => i.checked).map((i) => i.value).sort().join(" ");
      case "choice": case "accent": {
        const radios = inputs.filter((i) => i.type === "radio");
        return radios.length ? (radios.find((i) => i.checked) || {}).value || "" : inputs[0].value;
      }
      case "number": return String(parseInt(inputs[0].value || "0", 10) || 0);
      default: return inputs[0] ? inputs[0].value.trim() : "";
    }
  }

  function setValue(el, value) {
    const inputs = [...el.querySelectorAll("input, select")].filter((i) => i.name === el.dataset.field);
    const kind = el.dataset.kind;
    if (kind === "bool") inputs[0].checked = value === "1";
    else if (kind === "event_groups") { const vals = value.split(" "); inputs.forEach((i) => { i.checked = vals.includes(i.value); }); }
    else if (inputs[0] && inputs[0].type === "radio") inputs.forEach((i) => { i.checked = i.value === value; });
    else if (inputs[0]) inputs[0].value = value;
    inputs.forEach((i) => i.dispatchEvent(new Event("change", { bubbles: true })));
  }

  const normDefault = (el) => (el.dataset.kind === "event_groups" ? el.dataset.default.split(" ").sort().join(" ") : el.dataset.default);

  function markDefaults() {
    document.querySelectorAll(".field[data-default]").forEach((el) => {
      const differs = fieldValue(el) !== normDefault(el);
      el.classList.toggle("non-default", differs);
      const reset = el.querySelector("[data-reset]");
      if (reset) reset.hidden = !differs;
    });
  }

  // ─── Save bar ─────────────────────────────────────────────────────────────
  let saved = snapshot(), submitting = false;
  const bar = form && form.querySelector("[data-save-bar]");
  function update() {
    markDefaults();
    if (!bar) return;
    const dirty = snapshot() !== saved;
    form.classList.toggle("dirty", dirty);
    bar.querySelector("[data-save]").disabled = !dirty;
    bar.querySelector("[data-discard]").hidden = !dirty;
    const state = bar.querySelector("[data-save-state]"), text = dirty ? "Unsaved changes" : "No changes";
    if (state.textContent !== text) state.textContent = text;   // only on a change: the MutationObserver below sees text changes
  }
  if (form) {
    // A page shown again after a failed save holds the submitted values: they count as changes
    if (form.closest(".settings-main").querySelector(".flash.error")) saved = "";
    form.addEventListener("input", update);
    form.addEventListener("change", update);
    const rows = form.querySelector("[data-range-editor]");   // date filter rows added, moved or deleted
    if (rows) new MutationObserver(update).observe(rows, { childList: true });
    form.addEventListener("submit", () => { submitting = true; });
    bar.querySelector("[data-discard]").addEventListener("click", () => { submitting = true; location.reload(); });
    window.addEventListener("beforeunload", (e) => {
      if (!submitting && snapshot() !== saved) { e.preventDefault(); e.returnValue = ""; }
    });
    form.addEventListener("click", (e) => {
      const reset = e.target.closest("[data-reset]");
      if (reset) { const el = reset.closest(".field"); setValue(el, el.dataset.default); update(); }
    });
    update();
  }

  // ─── Folder checks while typing ───────────────────────────────────────────
  const timers = new WeakMap();
  document.addEventListener("input", (e) => {
    const input = e.target.closest("[data-paths]");
    if (!input) return;
    clearTimeout(timers.get(input));
    timers.set(input, setTimeout(async () => {
      const list = input.closest(".field").querySelector("[data-checks]");
      if (!list) return;
      try {
        const r = await fetch(input.dataset.pathsUrl || "/settings/paths?value=" + encodeURIComponent(input.value));
        const checks = await r.json();
        list.replaceChildren(...checks.map(([p, ok]) => {
          const li = document.createElement("li");
          li.className = ok ? "ok" : "missing";
          li.textContent = (ok ? "✓ " : "✗ ") + p;
          return li;
        }));
      } catch (err) { /* offline: keep the last result */ }
    }, 400));
  });

  // ─── Write-only token ─────────────────────────────────────────────────────
  document.querySelectorAll("[data-secret]").forEach((box) => {
    const input = box.querySelector("[data-secret-input]");
    const replace = box.querySelector("[data-secret-replace]");
    const remove = box.querySelector("[data-secret-remove]");
    if (replace) replace.addEventListener("click", () => { replace.hidden = true; input.hidden = false; input.focus(); });
    if (remove) remove.addEventListener("change", () => {
      input.disabled = remove.checked;
      if (remove.checked) input.value = "";
    });
  });

  // ─── Recovery codes: copy all at once ─────────────────────────────────────
  document.querySelectorAll("[data-copy-codes]").forEach((btn) => btn.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(btn.dataset.copyCodes); btn.textContent = "Copied"; }
    catch (e) { btn.textContent = "Select and copy them by hand"; }
  }));

  // ─── New API token: bundles tick their scopes ──────────────────────────────
  document.querySelectorAll("[data-bundle]").forEach((btn) => btn.addEventListener("click", () => {
    const want = new Set(btn.dataset.bundle.split(" "));
    btn.closest("form").querySelectorAll('input[name="scope"]').forEach((box) => { box.checked = want.has(box.value); });
  }));

  // Scopes that change the library: show the warning and require its confirmation
  document.querySelectorAll("[data-api-new]").forEach((f) => {
    const warn = f.querySelector("[data-opt-in-warning]"), confirm = f.querySelector("[data-opt-in-confirm]");
    const sync = () => {
      const on = [...f.querySelectorAll("input[data-opt-in]")].some((b) => b.checked);
      warn.hidden = !on;
      confirm.required = on;
      if (!on) confirm.checked = false;
    };
    f.addEventListener("change", sync);
    f.querySelectorAll("[data-bundle]").forEach((b) => b.addEventListener("click", sync));
    sync();
  });

  // ─── Connection tests (Plex, single sign-on) ───────────────────────────────
  // A button [data-conn-test="<url>"] posts the form as it is (not saved) and shows the answer
  // in the [data-conn-result] next to it.
  document.querySelectorAll("[data-conn-test]").forEach((test) => {
    const out = test.parentElement.querySelector("[data-conn-result]");
    async function run() {
      const data = new FormData(form);
      test.disabled = true;
      out.hidden = false; out.className = "flash info"; out.textContent = "Testing…";
      try {
        const r = await fetch(test.dataset.connTest, { method: "POST", body: data });
        const res = await r.json();
        out.className = "flash " + (res.ok ? "ok" : "error");
        out.textContent = res.message;
      } catch (err) {
        out.className = "flash error"; out.textContent = "The web UI didn't answer.";
      }
      test.disabled = false;
    }
    test.addEventListener("click", run);
    const url = form.querySelector('[name="PLEX_URL"]');
    if ("autoTest" in test.dataset && url && url.value.trim() && form.querySelector(".secret-state")) run();   // saved settings: test right away
  });

  // ─── File name example ────────────────────────────────────────────────────
  const example = document.querySelector("[data-name-example]");
  if (example) {
    let t;
    const refresh = () => {
      clearTimeout(t);
      t = setTimeout(async () => {
        const q = new URLSearchParams();
        ["RENAME_CODEC_TAGS", "RENAME_GROUP"].forEach((k) => q.set(k, form.querySelector(`[name="${k}"]`).value));
        if (form.querySelector('[name="RENAME_QUALITY"]').checked) q.set("RENAME_QUALITY", "1");
        try {
          const r = await fetch("/settings/replace/example?" + q);
          example.querySelector("[data-after]").textContent = (await r.json()).after;
        } catch (err) { /* keep */ }
      }, 250);
    };
    ["RENAME_CODEC_TAGS", "RENAME_GROUP", "RENAME_QUALITY"].forEach((k) => {
      const el = form.querySelector(`[name="${k}"]`);
      if (el) { el.addEventListener("input", refresh); el.addEventListener("change", refresh); }
    });
  }

  // ─── Appearance: preview while choosing (Discard reloads the saved look) ──
  const preview = { UI_ACCENT: "accent", UI_TEXT_SIZE: "text", UI_DENSITY: "density", UI_CONTRAST: "contrast", UI_MOTION: "motion" };
  document.addEventListener("change", (e) => {
    const name = e.target.name;
    if (preview[name]) root.dataset[preview[name]] = e.target.value;
    if (name === "UI_THEME") {
      root.dataset.themeDefault = e.target.value;
      if (!deviceTheme()) applyTheme(e.target.value);
    }
  });
  function deviceTheme() {
    try { return localStorage.getItem("theme"); } catch (e) { return null; }
  }
  function applyTheme(mode) {
    if (mode === "light" || mode === "dark") root.dataset.theme = mode; else delete root.dataset.theme;
    if (window.themeColor) window.themeColor(mode);
    if (window.showThemeToggle) window.showThemeToggle();
  }
  const device = document.querySelector("[data-theme-device]");
  if (device) {
    const names = { auto: "follows the system", light: "light", dark: "dark" };
    const show = () => {
      const t = deviceTheme();
      device.hidden = !t;
      if (t) device.querySelector("span").textContent = names[t] || t;
    };
    show();
    window.addEventListener("themechange", show);
    device.querySelector("[data-theme-follow]").addEventListener("click", () => {
      try { localStorage.removeItem("theme"); } catch (e) { /* blocked */ }
      applyTheme(root.dataset.themeDefault || "auto");
      show();
    });
  }

  // ─── Share cards: the edit button opens a share, Cancel closes it, + shows the new share ──
  const list = document.querySelector(".share-list");
  if (list) {
    list.addEventListener("click", (e) => {
      const card = e.target.closest(".share-card");
      if (e.target.closest("[data-share-edit]")) { e.preventDefault(); card.open = !card.open; }
      if (e.target.closest("[data-share-cancel]")) {
        card.open = false;
        card.querySelector("form").reset();
        if (card.classList.contains("share-new")) card.hidden = true;
      }
    });
    document.querySelector("[data-share-add]").addEventListener("click", () => {
      const card = document.getElementById("share-new");
      card.hidden = false; card.open = true;
      card.querySelector('[name="folder"]').focus();
    });
  }
})();
