// Single source of truth for "which day is it" in Pro Tracker. Used by the main
// screen (index.html) and the friends section (friends.js).
// A day runs 02:00–01:59 Israel time (Asia/Jerusalem), all year round including
// daylight saving time, regardless of the device's own time zone.
// Example: a meal at 01:30 on Nov 3 belongs to Nov 2.
(() => {
  const DAY_START_HOUR = 2;
  const TIME_ZONE = 'Asia/Jerusalem';

  // Reads Israel's wall-clock date and hour (not "now minus 2 hours"), so the
  // nights when clocks change are handled correctly.
  function trackerDay(instant = new Date(), timeZone = TIME_ZONE) {
    const parts = {};
    new Intl.DateTimeFormat('en-US', { timeZone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', hourCycle: 'h23' })
      .formatToParts(instant).forEach(p => { parts[p.type] = Number(p.value); });
    const day = new Date(Date.UTC(parts.year, parts.month - 1, parts.day - (parts.hour < DAY_START_HOUR ? 1 : 0)));
    return day.toISOString().slice(0, 10);
  }

  globalThis.trackerDay = trackerDay;
})();
