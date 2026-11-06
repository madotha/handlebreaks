// "Share's preset" options name the preset they stand for: a <select data-share-presets="{prefix:
// [share, preset name, source]}"> with an <option data-share-preset>. window.sharePresetFor(select,
// keys) labels it for the files (or folders) about to be queued; without keys, for every share.
(function () {
  const MAX = 70;
  window.sharePresetFor = function (select, keys) {
    const opt = select && select.querySelector("option[data-share-preset]");
    if (!opt) return;
    let map = {};
    try { map = JSON.parse(select.dataset.sharePresets || "{}"); } catch (e) { return; }
    const prefixes = [...new Set((keys && keys.length ? keys : Object.keys(map)).map((k) => k.split("/")[0]))]
      .filter((p) => map[p]);
    const byName = new Map();
    prefixes.forEach((p) => {
      const [share, name, source] = map[p];
      if (!byName.has(name)) byName.set(name, { shares: [], source });
      byName.get(name).shares.push(share);
    });
    let text = "Share's preset", title = "";
    if (byName.size === 1) {
      const [name, v] = byName.entries().next().value;
      text = `Share's preset: ${name}` + (v.source === "default" ? " (default)" : "");
    } else if (byName.size > 1) {
      const list = [...byName.entries()].map(([name, v]) => `${v.shares.join(", ")}: ${name}`);
      title = list.join("\n");
      text = `Share's preset (${list.join("; ")})`;
      if (text.length > MAX) text = `Each share's preset (${byName.size} different)`;
    }
    opt.textContent = text;
    opt.title = title;
    select.title = select.value === "" ? title : "";
  };
  document.querySelectorAll("select[data-share-presets]").forEach((s) => {
    window.sharePresetFor(s, []);
    s.addEventListener("change", () => { s.title = s.value === "" ? (s.querySelector("option[data-share-preset]") || {}).title || "" : ""; });
  });
})();
