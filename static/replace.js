// "Replace…" buttons (Files and Status pages): a dialog to choose the new file name and
// whether to keep the original, then a replace job for the worker; waits for its result.
// Buttons: <button data-replace data-key=… data-original=… data-encoded=…>.
(function () {
  const dialog = document.querySelector("[data-replace-dialog]");
  let current = null;                               // {btn, opts} of the open dialog
  const $ = (sel) => dialog.querySelector(sel);

  function open(btn, opts) {
    const encoded = btn.dataset.encoded;
    const dot = encoded.lastIndexOf(".");
    current = { btn, opts };
    $("[data-r-encoded]").textContent = encoded;
    $("[data-r-original]").textContent = btn.dataset.original;
    $("[data-r-name]").value = encoded.slice(0, dot);
    $("[data-r-ext]").textContent = encoded.slice(dot);
    $("[data-r-keep]").checked = true;
    $("[data-r-error]").hidden = true;
    dialog.showModal();
  }

  async function submit(e) {
    e.preventDefault();
    const { btn, opts } = current;
    const body = new FormData();
    body.append("csrf", dialog.dataset.csrf);
    body.append("key", btn.dataset.key);
    body.append("new_name", $("[data-r-name]").value.trim() + $("[data-r-ext]").textContent);
    if ($("[data-r-keep]").checked) body.append("keep_original", "1");
    const res = await (await fetch(dialog.dataset.url, { method: "POST", body, headers: { Accept: "application/json" } })).json();
    if (res.error) {
      $("[data-r-error]").textContent = res.error;
      $("[data-r-error]").hidden = false;
      return;
    }
    dialog.close();
    btn.disabled = true;
    opts.say(`Replacing ${btn.dataset.original} … waiting for the worker`, "busy");
    for (let i = 0; i < 600; i++) {                 // up to ~20 minutes (e.g. a copy across disks)
      await new Promise((ok) => setTimeout(ok, 2000));
      const st = await (await fetch(res.status_url)).json();
      if (st.state === "pending") continue;
      opts.say(st.message, st.state === "done" ? "ok" : "error");
      if (st.state === "done") await opts.done(btn); else btn.disabled = false;
      return;
    }
    opts.say("The worker hasn't picked up the replace job yet. Is it running? See the Status page.", "error");
    btn.disabled = false;
  }

  window.ReplaceDialog = {
    // opts: {say(text, kind), done(btn)}; buttons are found by event delegation in root
    attach(root, opts) {
      if (!dialog) return;
      root.addEventListener("click", (e) => {
        const btn = e.target.closest("[data-replace]");
        if (!btn) return;
        e.preventDefault();
        open(btn, opts);
      });
    },
  };
  if (dialog) $("[data-r-ok]").addEventListener("click", submit);
})();
