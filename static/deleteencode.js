// "Delete encode" (bin in a row) and "Delete selected…" in Status → Awaiting review: a dialog
// (templates/_delete_encode_dialog.html) to delete the finished encodes, optionally queueing the
// files again with another preset. The form posts to /encodes/delete and the page shows the result;
// with data-json on the dialog (Files page) it is sent in the background instead, and
// window.onEncodeDeleted(result, button that opened the dialog) shows it (the page reloads the folder).
(function () {
  const dialog = document.querySelector("[data-delete-dialog]");
  if (!dialog) return;
  const $ = (sel) => dialog.querySelector(sel);
  const preset = $("[data-de-preset]"), ok = $("[data-de-ok]");
  let opener = null;

  function update() {
    const again = $("[data-de-requeue]").checked;
    preset.disabled = !again;
    $("[data-de-preset-field]").classList.toggle("muted", !again);
    ok.textContent = again ? "Delete and encode again" : "Delete";
  }

  // keys: the files whose encodes are deleted; one: {encoded, preset} of a single row
  function open(keys, one) {
    if (!keys.length) return;
    $("[data-de-keys]").replaceChildren(...keys.map((k) => {
      const input = document.createElement("input");
      input.type = "hidden";
      input.name = "key";
      input.value = k;
      return input;
    }));
    const n = keys.length;
    $("[data-de-title]").textContent = n === 1 ? "Delete the encode" : `Delete ${n.toLocaleString()} encodes`;
    const what = $("[data-de-what]");
    if (one) {
      const code = document.createElement("code");
      code.textContent = one.encoded;
      what.replaceChildren("Finished encode: ", code);
    } else {
      what.textContent = `The finished encodes of the ${n.toLocaleString()} selected files.`;
    }
    const used = $("[data-de-used]");
    used.hidden = !(one && one.preset);
    used.textContent = one && one.preset ? `Encoded with the preset "${one.preset}".` : "";
    dialog.querySelector('input[name="requeue"][value="0"]').checked = true;
    preset.value = "";
    update();
    window.returnFocusOnClose(dialog);
    dialog.showModal();
  }

  dialog.querySelectorAll('input[name="requeue"]').forEach((r) => r.addEventListener("change", update));
  $("[data-de-cancel]").addEventListener("click", () => dialog.close());
  $("[data-de-form]").addEventListener("submit", async (e) => {
    ok.disabled = true;
    if (!("json" in dialog.dataset)) return;              // an ordinary form post (Status page)
    e.preventDefault();
    const body = new FormData(e.target);
    body.append("json", "1");
    let result;
    try {
      const r = await fetch(e.target.action, { method: "POST", body, headers: { Accept: "application/json" } });
      result = await r.json();
    } catch (err) {
      result = { ok: false, kind: "error", message: "The encode could not be deleted. Reload the page and try again." };
    }
    ok.disabled = false;
    dialog.close();
    if (window.onEncodeDeleted) window.onEncodeDeleted(result, opener);
  });

  document.addEventListener("click", (e) => {
    const btn = e.target.closest && e.target.closest("[data-delete-encode]");
    if (!btn) return;
    e.preventDefault();
    opener = btn;
    open([btn.dataset.key], { encoded: btn.dataset.encoded, preset: btn.dataset.preset });
  });
  const selected = document.querySelector("[data-delete-encodes-selected]");
  if (selected) selected.addEventListener("click", () => {
    opener = selected;
    const keys = window.selectedStatusKeys ? window.selectedStatusKeys()
      : [...document.querySelectorAll('[data-queue-rows] input[name="key"]:checked')].map((c) => c.value);
    open(keys, null);
  });
})();
