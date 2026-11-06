"""US market calendar: holidays, early closes, and the next-fire computation
(DESIGN §1.3, §4.1, §7.2).

Every date/time computation here happens in ``zoneinfo("America/New_York")``
and nowhere does the code compute a manual UTC/ET offset — DESIGN §1.3 is
explicit that DST is handled entirely by ``zoneinfo``, not by hand, and that
Android's APEX tzdata module keeps the underlying rules current independently
of the OS image, so trusting ``ZoneInfo`` here is safe on both runtimes this
package ships on (desktop CPython and Chaquopy).

The holiday and early-close tables are **computed from the documented NYSE
observance rules for any year**, not hardcoded to one calendar year — DESIGN
§1.4/§4.1 both call this out explicitly (a T-30min session depends on getting
early closes right indefinitely, not just for the year this file was written
in). The rules implemented:

* Independence Day, Christmas Day — fixed dates, shifted off a weekend per
  the standard US federal observance rule (Saturday -> observed Friday,
  Sunday -> observed Monday).
* New Year's Day — fixed date, but *not* the standard rule above: NYSE Rule
  7.2 carves out "unusual business conditions ... such as the ending of a
  ... yearly accounting period," so a Saturday New Year's Day observes
  nothing (Dec 31 stays open) while a Sunday New Year's Day still rolls to
  the following Monday. See :func:`_observed_new_years_day`.
* Martin Luther King Jr. Day, Washington's Birthday, Labor Day — nth-weekday
  rules.
* Memorial Day — last Monday of May.
* Good Friday — not a federal holiday, but NYSE has always closed for it;
  computed from Easter Sunday via the Anonymous Gregorian algorithm.
* Juneteenth — added to the NYSE calendar starting 2022; gated by year so this
  table is correct for years before and after that change.
* Early closes (13:00 ET): the day after Thanksgiving, Christmas Eve, and the
  day before Independence Day, each included **only when that day is itself
  otherwise a trading day** — which is what correctly drops, say, a July 3rd
  early close in a year where July 4th's weekend observance already consumes
  July 3rd as the full holiday, with no special-cased weekday arithmetic.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from typing import Final
from zoneinfo import ZoneInfo

__all__ = [
    "EASTERN",
    "REGULAR_CLOSE",
    "EARLY_CLOSE",
    "WAKE_BEFORE_CLOSE_MIN",
    "nyse_holidays",
    "nyse_early_closes",
    "is_trading_day",
    "is_early_close",
    "session_close",
    "session_close_datetime",
    "next_trading_day",
    "next_close_minus",
]

#: The one timezone every computation in this module reasons in.
EASTERN: Final = ZoneInfo("America/New_York")

#: Normal NYSE close.
REGULAR_CLOSE: Final = time(16, 0)

#: Documented NYSE half-day close (DESIGN §4.1's "13:00 ET half-days").
EARLY_CLOSE: Final = time(13, 0)

#: Shared lead for the Android wake and the reported next run.
WAKE_BEFORE_CLOSE_MIN: Final = 55

#: NYSE added Martin Luther King Jr. Day and Washington's Birthday in 1998.
_MLK_AND_PRESIDENTS_FIRST_YEAR: Final = 1998

#: NYSE added Juneteenth National Independence Day starting in 2022.
_JUNETEENTH_FIRST_YEAR: Final = 2022


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The date of the ``n``-th ``weekday`` (Monday=0) in ``month``/``year``."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """The date of the last ``weekday`` (Monday=0) in ``month``/``year``."""
    if month == 12:
        next_month_start = date(year + 1, 1, 1)
    else:
        next_month_start = date(year, month + 1, 1)
    last_day = next_month_start - timedelta(days=1)
    offset = (last_day.weekday() - weekday) % 7
    return last_day - timedelta(days=offset)


def _observed(fixed_date: date) -> date:
    """Shift a fixed-date holiday off a weekend (standard US observance):
    Saturday -> the preceding Friday, Sunday -> the following Monday."""
    if fixed_date.weekday() == 5:
        return fixed_date - timedelta(days=1)
    if fixed_date.weekday() == 6:
        return fixed_date + timedelta(days=1)
    return fixed_date


def _observed_new_years_day(fixed_date: date) -> date:
    """New Year's Day observance -- deliberately *not* :func:`_observed`.

    NYSE Rule 7.2's Saturday-holiday-observed-Friday rule carries its own
    documented exception for "unusual business conditions ... such as the
    ending of a monthly or yearly accounting period." December 31st is
    exactly that: the exchange stays open so the calendar year's final
    trading session is not lost. A Saturday New Year's Day therefore closes
    *nothing* extra -- confirmed against the real NYSE record for 2010-12-31,
    2021-12-31 (Jan 1 2011 and 2022 were Saturdays) and 2027-12-31 (Jan 1 2028
    is a Saturday): the NYSE traded on all three.

    A Sunday New Year's Day is unaffected by this exception and still rolls
    forward to the following Monday, same as every other fixed-date holiday
    (e.g. 2023-01-02 for Jan 1 2023).
    """
    if fixed_date.weekday() == 6:
        return fixed_date + timedelta(days=1)
    return fixed_date


def _easter_sunday(year: int) -> date:
    """Easter Sunday via the Anonymous Gregorian (Meeus/Jones/Butcher)
    algorithm — good for any Gregorian-calendar year, not tabulated."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


@lru_cache(maxsize=None)
def nyse_holidays(year: int) -> frozenset[date]:
    """Full-day NYSE market holidays for ``year``.

    Computed fresh for any year from the documented observance rules — see
    the module docstring. Cached per-year since the same year is asked about
    repeatedly (every ``is_trading_day`` call for a date in it).
    """
    holidays: set[date] = {
        _observed_new_years_day(date(year, 1, 1)),  # New Year's Day (Rule 7.2 exception)
        _easter_sunday(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),  # Memorial Day
        _observed(date(year, 7, 4)),  # Independence Day
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed(date(year, 12, 25)),  # Christmas Day
    }
    if year >= _MLK_AND_PRESIDENTS_FIRST_YEAR:
        holidays.add(_nth_weekday(year, 1, 0, 3))  # MLK Day
        holidays.add(_nth_weekday(year, 2, 0, 3))  # Washington's Birthday
    if year >= _JUNETEENTH_FIRST_YEAR:
        holidays.add(_observed(date(year, 6, 19)))  # Juneteenth
    return frozenset(holidays)


def _holiday_window(year: int) -> frozenset[date]:
    """``year``'s holidays plus the adjacent years'.

    An observed date can cross a year boundary — e.g. New Year's Day falling
    on a Saturday is observed on the preceding Dec 31 — so checking whether a
    late-December date is a holiday requires also having computed next
    year's table, not just this year's.
    """
    return nyse_holidays(year - 1) | nyse_holidays(year) | nyse_holidays(year + 1)


def is_trading_day(d: date) -> bool:
    """True if ``d`` is a NYSE trading day: not a weekend, not a holiday."""
    if d.weekday() >= 5:
        return False
    return d not in _holiday_window(d.year)


@lru_cache(maxsize=None)
def nyse_early_closes(year: int) -> frozenset[date]:
    """1:00pm ET early-close days for ``year``.

    The day after Thanksgiving, Christmas Eve, and the day before Independence
    Day — each included only when it is itself otherwise a trading day. That
    single condition is what correctly handles every weekend-observance edge
    case (e.g. no separate July 3rd early close in a year where July 4th
    falling on a Saturday already makes July 3rd the observed full holiday)
    without hardcoding the weekday arithmetic per case.
    """
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    candidates = {
        thanksgiving + timedelta(days=1),
        date(year, 12, 24),
        date(year, 7, 3),
    }
    return frozenset(d for d in candidates if is_trading_day(d))


def is_early_close(d: date) -> bool:
    """True if ``d`` is a documented 13:00 ET NYSE early-close day."""
    return d in nyse_early_closes(d.year)


def session_close(d: date) -> time:
    """The ET close time for trading date ``d``.

    16:00 normally, 13:00 on a documented early-close day (DESIGN §4.1) — the
    session's T-30min fire time depends on this being right, not assumed.
    """
    return EARLY_CLOSE if is_early_close(d) else REGULAR_CLOSE


def session_close_datetime(d: date) -> datetime:
    """``session_close(d)`` as a tz-aware ``America/New_York`` datetime."""
    return datetime.combine(d, session_close(d), tzinfo=EASTERN)


def next_trading_day(d: date) -> date:
    """The first NYSE trading day strictly after ``d``."""
    candidate = d + timedelta(days=1)
    while not is_trading_day(candidate):
        candidate += timedelta(days=1)
    return candidate


def next_close_minus(minutes: int, after: datetime | None = None) -> datetime:
    """The next US session's close minus ``minutes``, in ``America/New_York``.

    This is the function ``AlarmScheduler.kt`` calls through ``host.py``
    (DESIGN §7.2): "the next US session's close minus N minutes, in
    America/New_York, converted by the caller to device local time." This
    function never performs that device-local conversion itself — the caller
    does, per the design.

    Honors early-close (13:00 ET) days via :func:`session_close_datetime`, so
    a half-day correctly yields an earlier fire instant with no special
    handling at the call site.

    Args:
        minutes: Minutes before the close to fire at. Must not be negative.
        after: The instant to search strictly after. Defaults to now (ET). A
            naive datetime is assumed to already be Eastern wall-clock time;
            an aware one is converted. Passing the *previous* fire instant
            back in (as the end of a session does, DESIGN §7.2 step [11])
            correctly rolls over to the next trading day rather than
            returning the same instant again, since the search is for a
            result strictly greater than ``after``.

    Returns:
        A tz-aware ``America/New_York`` datetime strictly after ``after``.
    """
    if minutes < 0:
        raise ValueError("minutes must not be negative")
    if after is None:
        reference = datetime.now(EASTERN)
    elif after.tzinfo is None:
        reference = after.replace(tzinfo=EASTERN)
    else:
        reference = after.astimezone(EASTERN)

    candidate_date = reference.date()
    while True:
        if is_trading_day(candidate_date):
            fire_at = session_close_datetime(candidate_date) - timedelta(minutes=minutes)
            if fire_at > reference:
                return fire_at
        candidate_date += timedelta(days=1)
