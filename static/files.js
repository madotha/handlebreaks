// Files page: folder tree that loads each folder when it's opened, "Show more" for
// long file lists, and selection for queueing. A ticked folder is sent as a whole
// (the server expands it), so files inside it don't need to be loaded.
(function () {
  const form = document.getElementById("queue-form");
  if (!form) return;
  const count = form.querySelector("[data-selected-count]");
  const button = form.querySelector("[data-queue-button]");
  const toggleOf = (details) => details && details.querySelector(":scope > summary .dir-toggle");

  // Inputs below a ticked folder are ticked and disabled: the folder covers them,
  // and disabled inputs aren't submitted twice. data-locked marks inputs that are
  // disabled for good (already queued, or a folder with nothing to select).
  function cover(root, on) {
    root.querySelectorAll('input[name="key"], .dir-toggle').forEach((i) => {
      if ("locked" in i.dataset) return;
      i.checked = on;
      i.disabled = on;
    });
  }

  function update() {
    let n = 0;
    form.querySelectorAll(".dir-toggle:checked:not(:disabled)").forEach((t) => { n += Number(t.dataset.count); });
    n += form.querySelectorAll('input[name="key"]:checked:not(:disabled)').length;
    count.textContent = n.toLocaleString();
    button.disabled = n === 0;
    button.dataset.count = n;
  }

  async function load(details) {
    const body = details.querySelector(":scope > .dir-body");
    if (body.dataset.loaded) return;
    body.dataset.loaded = "1";
    try {
      const r = await fetch(body.dataset.src);
      if (!r.ok) throw new Error(r.status);
      body.innerHTML = await r.text();
    } catch (e) {
      body.innerHTML = '<p class="flash error">Could not load this folder. Reload the page and try again.</p>';
      delete body.dataset.loaded;
      return;
    }
    const parent = toggleOf(details);
    if (parent && parent.checked) cover(body, true);
    init(body);
  }

  async function more(btn) {
    btn.disabled = true;
    const r = await fetch(btn.dataset.more);
    if (!r.ok) { btn.disabled = false; return; }
    const tbody = btn.closest(".files-level").querySelector("tbody");
    const tmp = document.createElement("tbody");
    tmp.innerHTML = await r.text();
    const parent = toggleOf(btn.closest("details"));
    if (parent && parent.checked) cover(tmp, true);
    tbody.append(...tmp.children);
    const next = r.headers.get("X-Next");
    if (next) {
      btn.dataset.more = next;
      btn.disabled = false;
      const shown = tbody.children.length;
      const label = btn.querySelector("[data-more-label]");
      label.textContent = label.textContent.replace(/^\(\S+/, "(" + shown.toLocaleString());
    } else {
      btn.remove();
    }
    update();
  }

  function init(root) {
    root.querySelectorAll("details.dir").forEach((d) => {
      d.addEventListener("toggle", () => { if (d.open) load(d); });
      if (d.open) load(d);
    });
    // The folder checkbox sits inside <summary>: cancel the click so the folder
    // doesn't open or close, then apply the new state ourselves.
    root.querySelectorAll(".dir-toggle").forEach((t) => {
      t.addEventListener("click", (e) => {
        const wanted = t.checked;
        e.preventDefault();
        setTimeout(() => {
          t.checked = wanted;
          cover(t.closest("details").querySelector(":scope > .dir-body"), wanted);
          update();
        });
      });
    });
    root.querySelectorAll("[data-more]").forEach((b) => b.addEventListener("click", () => more(b)));
    update();
  }

  form.addEventListener("change", update);
  form.addEventListener("submit", (e) => {
    const n = Number(button.dataset.count || 0);
    if (n > 200 && !confirm(`Queue ${n.toLocaleString()} files for encoding?`)) e.preventDefault();
  });
  init(form);
})();
