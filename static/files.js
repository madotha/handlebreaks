// Files page: folder checkboxes select all files below them, and the queue
// button shows how many files are selected. Without JavaScript, single file
// checkboxes and the queue button still work.
(function () {
  const form = document.getElementById("queue-form");
  if (!form) return;
  const count = form.querySelector("[data-selected-count]");
  const button = form.querySelector("[data-queue-button]");
  const selectable = (root) => root.querySelectorAll('input[name="key"]:not(:disabled)');

  function update() {
    const n = form.querySelectorAll('input[name="key"]:checked').length;
    count.textContent = n;
    button.disabled = n === 0;
    // A folder checkbox is checked when all its selectable files are, mixed when some are
    form.querySelectorAll(".dir-toggle").forEach((toggle) => {
      const boxes = selectable(toggle.closest("details"));
      const checked = [...boxes].filter((b) => b.checked).length;
      toggle.checked = boxes.length > 0 && checked === boxes.length;
      toggle.indeterminate = checked > 0 && checked < boxes.length;
      toggle.disabled = boxes.length === 0;
    });
  }

  form.querySelectorAll(".dir-toggle").forEach((toggle) => {
    // The checkbox sits inside <summary>: cancel the click so the folder doesn't
    // open or close, then apply the new state ourselves.
    toggle.addEventListener("click", (e) => {
      const wanted = toggle.checked;
      e.preventDefault();
      setTimeout(() => {
        selectable(toggle.closest("details")).forEach((b) => { b.checked = wanted; });
        update();
      });
    });
  });
  form.addEventListener("change", update);
  update();
})();
