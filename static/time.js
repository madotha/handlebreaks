// Shows every <time datetime="…"> in the viewer's time zone. The server stores times in
// its own local time (a Docker container usually runs in UTC) and sends them with the
// UTC offset, so the browser can convert them.
(function () {
  const pad = (n) => String(n).padStart(2, "0");
  window.formatLocalTime = function (iso) {
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };
  window.localizeTimes = function (root) {
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    (root || document).querySelectorAll("time[datetime]").forEach((t) => {
      t.textContent = window.formatLocalTime(t.getAttribute("datetime"));
      t.title = `Your time zone (${zone})`;
    });
  };
  window.localizeTimes();
})();
