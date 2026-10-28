// Shows every <time datetime="…"> in the viewer's time zone. The server stores times in
// its own local time (a Docker container usually runs in UTC) and sends them with the
// UTC offset, so the browser can convert them. The date order comes from the DATE_FORMAT
// setting (html[data-date-format], e.g. "DD.MM.YYYY").
(function () {
  const pad = (n) => String(n).padStart(2, "0");
  const FORMAT = /^(YYYY|DD|MM)([-./])(YYYY|DD|MM)\2(YYYY|DD|MM)$/.exec(document.documentElement.dataset.dateFormat || "")
    || [null, "YYYY", "-", "MM", "DD"];
  window.formatLocalTime = function (iso) {
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    const part = { YYYY: d.getFullYear(), MM: pad(d.getMonth() + 1), DD: pad(d.getDate()) };
    const date = [FORMAT[1], FORMAT[3], FORMAT[4]].map((p) => part[p]).join(FORMAT[2]);
    return `${date} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };
  window.localizeTimes = function (root) {
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    (root || document).querySelectorAll("time[datetime]").forEach((t) => {
      const iso = t.getAttribute("datetime");
      t.textContent = window.formatLocalTime(iso);
      t.title = `Your time zone (${zone})`;
    });
  };
  window.localizeTimes();
})();
