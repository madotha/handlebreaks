// Replace several originals at once (Files and Status pages): asks the server for the
// files with a finished encode and their suggested names, lets you edit the names, then
// starts one replace job per file and waits for all of them.
(function () {
  const dialog = document.querySelector("[data-bulk-dialog]");
  if (!dialog) return;
  const $ = (sel) => dialog.querySelector(sel);
  const rows = $("[data-b-rows]");
  const okBtn = $("[data-b-ok]");
  let opts = null;

  function row(item) {
    const dot = item.suggested.lastIndexOf(".");
    const tr = document.createElement("tr");
    tr.dataset.key = item.key;
    tr.innerHTML = '<td class="name"></td><td><div class="name-input"><textarea rows="1" required spellcheck="false" autocomplete="off" autocapitalize="off"></textarea>' +
      '<span></span></div><p class="small b-row-error" hidden></p></td>';
    tr.cells[0].textContent = item.short;
    tr.cells[0].title = item.key;
    const field = tr.querySelector("textarea");
    field.value = item.suggested.slice(0, dot);
    tr.fit = window.nameField(field, () => okBtn.click());
    tr.querySelector("span").textContent = item.suggested.slice(dot);
    return tr;
  }

  function showError(text) {
    $("[data-b-error]").textContent = text;
    $("[data-b-error]").hidden = !text;
  }

  // body: FormData with key/folder values (and filters), or all=1; o: {say(text, kind), done()}
  async function open(body, o) {
    opts = o;
    rows.innerHTML = "";
    $(".bulk-list").hidden = true;
    okBtn.disabled = true;
    showError("");
    $("[data-b-keep]").checked = $("[data-b-keep]").defaultChecked;   // KEEP_ORIGINALS
    $("[data-b-summary]").textContent = "Looking for finished encodes …";
    body.set("csrf", dialog.dataset.csrf);
    if (window.returnFocusOnClose) window.returnFocusOnClose(dialog);
    dialog.showModal();
    let res;
    try {
      res = await (await fetch(dialog.dataset.previewUrl, { method: "POST", body })).json();
    } catch (e) {
      $("[data-b-summary]").textContent = "Couldn't load the list. Reload the page and try again.";
      return;
    }
    const n = res.items.length;
    let text = n ? `${n.toLocaleString()} file${n === 1 ? "" : "s"} with a finished encode.`
                 : "None of these files has a finished encode waiting in the output folder.";
    if (res.left_out) text += ` ${res.left_out.toLocaleString()} without a finished encode (or already being replaced) are left out.`;
    if (n >= res.limit) text += ` At most ${res.limit} at a time.`;
    $("[data-b-summary]").textContent = text;
    res.items.forEach((item) => rows.append(row(item)));
    $(".bulk-list").hidden = !n;
    [...rows.children].forEach((tr) => tr.fit());      // heights need the rows to be visible
    okBtn.disabled = !n;
  }

  async function submit(e) {
    e.preventDefault();
    const body = new FormData();
    body.append("csrf", dialog.dataset.csrf);
    const trs = [...rows.children];
    trs.forEach((tr) => {
      body.append("key", tr.dataset.key);
      body.append("new_name", tr.querySelector("textarea").value.trim() + tr.querySelector("span").textContent);
      tr.querySelector(".b-row-error").hidden = true;
    });
    if ($("[data-b-keep]").checked) body.append("keep_original", "1");
    okBtn.disabled = true;
    const r = await fetch(dialog.dataset.submitUrl, { method: "POST", body, headers: { Accept: "application/json" } });
    const res = await r.json();
    okBtn.disabled = false;
    if (res.errors || res.error) {
      showError(res.error || "Some names need a change (marked below). Nothing was replaced yet.");
      trs.forEach((tr) => {
        const msg = res.errors && res.errors[tr.dataset.key];
        const p = tr.querySelector(".b-row-error");
        p.textContent = msg || "";
        p.hidden = !msg;
      });
      return;
    }
    dialog.close();
    await wait(res.jobs);
  }

  async function wait(jobs) {
    const left = new Map(jobs.map((j) => [j.status_url, j]));
    let ok = 0;
    const failed = [];
    if (window.ReplaceProgress) window.ReplaceProgress.kick();   // the progress bar shows how it goes
    for (let i = 0; i < 900 && left.size; i++) {    // up to ~30 minutes
      await new Promise((r) => setTimeout(r, 2000));
      for (const url of [...left.keys()]) {
        const st = await (await fetch(url)).json();
        if (st.state === "pending") continue;
        left.delete(url);
        if (st.state === "done") ok++; else failed.push(st.message);
      }
    }
    if (left.size) {
      opts.say(`${ok} replaced; ${left.size} still waiting for the worker. Is it running? See the Status page.`, "error");
      return;
    }
    if (failed.length) opts.say(`${failed.length} of ${jobs.length} replaces failed: ${failed.join(" ")}`, "error");
    if (ok) setTimeout(() => opts.done(), 2500);
  }

  okBtn.addEventListener("click", submit);
  window.BulkReplace = { open };
})();
