// US equity market (NYSE/NASDAQ) holiday and early-close calendar.
//
// *** EXPIRES 2029: this table only covers 2026-2028. Extend it before
// trading into 2029 or the cron trigger will silently stop firing on any
// day not covered (isTradingDay() below treats an unlisted date as an
// ordinary trading day, which is safe for holidays it *knows* about but
// gives no protection past the table's range). ***
//
// Sourced from NYSE's official announcement:
// https://www.nyse.com/publicdocs/nyse/regulation/nyse/NYSE_Group_2026-2027-2028_Holiday_Calendar.pdf
// (also reported via
//  https://ir.theice.com/press/news-details/2025/NYSE-Group-Announces-2026-2027-and-2028-Holiday-and-Early-Closings-Calendar/default.aspx)
//
// Full-day closures (market not open at all):
const FULL_HOLIDAYS = new Set([
  // 2026
  "2026-01-01", // New Year's Day
  "2026-01-19", // Martin Luther King, Jr. Day
  "2026-02-16", // Washington's Birthday
  "2026-04-03", // Good Friday
  "2026-05-25", // Memorial Day
  "2026-06-19", // Juneteenth
  "2026-07-03", // Independence Day (observed, July 4 is a Saturday)
  "2026-09-07", // Labor Day
  "2026-11-26", // Thanksgiving Day
  "2026-12-25", // Christmas Day
  // 2027
  "2027-01-01", // New Year's Day
  "2027-01-18", // Martin Luther King, Jr. Day
  "2027-02-15", // Washington's Birthday
  "2027-03-26", // Good Friday
  "2027-05-31", // Memorial Day
  "2027-06-18", // Juneteenth (observed, June 19 is a Saturday)
  "2027-07-05", // Independence Day (observed, July 4 is a Sunday)
  "2027-09-06", // Labor Day
  "2027-11-25", // Thanksgiving Day
  "2027-12-24", // Christmas Day (observed, Dec 25 is a Saturday)
  // 2028 (2028-01-01 falls on a Saturday - already not a trading day)
  "2028-01-17", // Martin Luther King, Jr. Day
  "2028-02-21", // Washington's Birthday
  "2028-04-14", // Good Friday
  "2028-05-29", // Memorial Day
  "2028-06-19", // Juneteenth
  "2028-07-04", // Independence Day
  "2028-09-04", // Labor Day
  "2028-11-23", // Thanksgiving Day
  "2028-12-25", // Christmas Day
]);

// Early-close (1:00 p.m. ET) days.
const HALF_DAYS = new Set([
  "2026-11-27", // day after Thanksgiving
  "2026-12-24", // Christmas Eve
  "2027-11-26", // day after Thanksgiving
  "2028-07-03", // day before Independence Day
  "2028-11-24", // day after Thanksgiving
]);

export const CALENDAR_TABLE_COVERS = { from: "2026-01-01", to: "2028-12-31" };

/** "YYYY-MM-DD" for a Date, read in a specific IANA timezone. */
function isoDateInTimeZone(date, timeZone) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(date);
  const get = (type) => parts.find((p) => p.type === type).value;
  return `${get("year")}-${get("month")}-${get("day")}`;
}

/** "HH:MM" 24h wall-clock time for a Date, read in a specific IANA timezone. */
function hhmmInTimeZone(date, timeZone) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
  }).formatToParts(date);
  const get = (type) => parts.find((p) => p.type === type).value;
  // Intl can render midnight as "24:00" with hour12:false in some engines;
  // normalize that to "00:00" so string comparisons behave.
  const hour = get("hour") === "24" ? "00" : get("hour");
  return `${hour}:${get("minute")}`;
}

/** 0=Sunday..6=Saturday, as observed in America/New_York. */
function weekdayInTimeZone(date, timeZone) {
  const weekday = new Intl.DateTimeFormat("en-US", { timeZone, weekday: "short" }).format(date);
  return ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].indexOf(weekday);
}

const ET_ZONE = "America/New_York";

export function isTradingDay(date) {
  const isoDate = isoDateInTimeZone(date, ET_ZONE);
  const weekday = weekdayInTimeZone(date, ET_ZONE);
  if (weekday === 0 || weekday === 6) return false; // weekend
  if (FULL_HOLIDAYS.has(isoDate)) return false;
  return true;
}

export function isHalfDay(date) {
  const isoDate = isoDateInTimeZone(date, ET_ZONE);
  return HALF_DAYS.has(isoDate);
}

/**
 * The target ET wall-clock fire time for "30 minutes before the close",
 * as an "HH:MM" string: 15:30 on a normal day (close 16:00), 12:30 on a
 * 1:00 p.m. early-close day.
 */
export function targetFireTime(date) {
  return isHalfDay(date) ? "12:30" : "15:30";
}

/**
 * Cron-tick decision function. `now` is a JS Date (always UTC internally —
 * Date has no timezone of its own). We deliberately do NOT try to compute
 * an ET-vs-UTC offset by hand anywhere: `Intl.DateTimeFormat` with
 * `timeZone: "America/New_York"` resolves the correct EST (UTC-5) or EDT
 * (UTC-4) offset for the given instant using the IANA tz database bundled
 * with the runtime, which is exactly the DST trap DESIGN.md §7.2 warns
 * about — the cron trigger fires on a fixed UTC schedule (wrangler.toml),
 * and it is this function, not the cron string, that does the real
 * "is it actually 15:30 America/New_York right now" check.
 *
 * Returns true iff *this* is the tick that should fire the push: today is
 * a trading day, wall-clock ET time has reached (or just passed) the
 * target, and per `alreadyFiredToday` we have not already sent it today.
 */
export function shouldFireSessionTrigger(now, alreadyFiredToday) {
  if (alreadyFiredToday) return false;
  if (!isTradingDay(now)) return false;
  const nowHHMM = hhmmInTimeZone(now, ET_ZONE);
  const target = targetFireTime(now);
  return nowHHMM >= target;
}

/** "YYYY-MM-DD" trading-date key in ET, used as the KV idempotency key. */
export function tradingDateKey(now) {
  return isoDateInTimeZone(now, ET_ZONE);
}
