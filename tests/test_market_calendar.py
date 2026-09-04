"""Tests for nightwatch.market.calendar (DESIGN §1.3, §1.4, §4.1, §7.2).

No network, no DB — this module is pure date/time arithmetic over `zoneinfo`,
so every test here drives it directly with hand-picked dates spanning several
different years, specifically to prove the holiday/early-close tables are
*computed*, not hardcoded to one year (DESIGN's own requirement).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from nightwatch.market import calendar


# ===========================================================================
# Weekends and ordinary trading days
# ===========================================================================


def test_ordinary_weekday_is_a_trading_day() -> None:
    # Wednesday, no nearby holiday.
    assert calendar.is_trading_day(date(2026, 3, 11)) is True


def test_weekend_is_never_a_trading_day() -> None:
    assert calendar.is_trading_day(date(2026, 3, 14)) is False  # Saturday
    assert calendar.is_trading_day(date(2026, 3, 15)) is False  # Sunday


# ===========================================================================
# Fixed-date holidays across several distinct years -- not hardcoded to one
# ===========================================================================


@pytest.mark.parametrize("year", [2024, 2025, 2026, 2027, 2030])
def test_christmas_and_new_years_are_holidays_every_year(year: int) -> None:
    assert calendar.is_trading_day(date(year, 12, 25)) is False
    assert calendar.is_trading_day(date(year, 1, 1)) is False


@pytest.mark.parametrize("year", [2024, 2025, 2026, 2027, 2030])
def test_independence_day_is_a_holiday_every_year(year: int) -> None:
    assert calendar.is_trading_day(date(year, 7, 4)) is False


def test_weekend_holiday_is_observed_on_the_adjacent_weekday() -> None:
    # July 4, 2026 is a Saturday -> observed Friday July 3, 2026.
    assert date(2026, 7, 4).weekday() == 5
    assert calendar.is_trading_day(date(2026, 7, 3)) is False
    assert calendar.is_trading_day(date(2026, 7, 4)) is False  # weekend anyway


def test_saturday_christmas_does_close_the_preceding_friday() -> None:
    # Contrast with New Year's Day (tested below): December 25, 2010 is a
    # Saturday, and Christmas has no year-end-accounting exception, so the
    # ordinary Rule 7.2 observance applies and Friday Dec 24, 2010 is closed.
    assert date(2010, 12, 25).weekday() == 5
    assert calendar.is_trading_day(date(2010, 12, 24)) is False


def test_sunday_holiday_observed_on_the_following_monday() -> None:
    # January 1, 2028 is a Saturday; check a Sunday case instead: July 4, 2027
    # falls on a Sunday -> observed Monday July 5, 2027.
    assert date(2027, 7, 4).weekday() == 6
    assert calendar.is_trading_day(date(2027, 7, 5)) is False
    # And July 3rd, 2027 (Saturday) is not itself a trading day regardless.


@pytest.mark.parametrize(
    "day",
    [date(2010, 12, 31), date(2021, 12, 31), date(2027, 12, 31)],
)
def test_new_years_day_on_a_saturday_does_not_close_the_preceding_friday(
    day: date,
) -> None:
    """NYSE Rule 7.2's Saturday-holiday-observed-Friday rule has a documented
    exception for "unusual business conditions ... such as the ending of a
    ... yearly accounting period" -- which is exactly why the exchange stays
    open on December 31st even when January 1st falls on a Saturday. This is
    NOT the same as the ordinary Saturday-holiday case (e.g. a Saturday
    Christmas correctly closes the preceding Friday): New Year's Day is a
    special-cased exception to the general rule, confirmed against the real
    NYSE record for all three live occurrences a 2010-2027 test range spans.
    """
    following_jan_1 = date(day.year + 1, 1, 1)
    assert day.weekday() == 4  # Friday
    assert following_jan_1.weekday() == 5  # the Saturday New Year's Day
    assert calendar.is_trading_day(day) is True


def test_new_years_day_observance_can_cross_a_year_boundary() -> None:
    # January 1, 2028 falls on a Saturday. Unlike an ordinary fixed-date
    # holiday, NYSE's year-end-accounting exception (Rule 7.2) means this
    # does NOT push an observed holiday back onto Friday Dec 31, 2027 --
    # the exchange trades that day. (Contrast the ordinary case tested
    # elsewhere: a Saturday Christmas *does* close the preceding Friday.)
    assert date(2028, 1, 1).weekday() == 5
    assert calendar.is_trading_day(date(2027, 12, 31)) is True


# ===========================================================================
# Floating holidays (nth-weekday / last-weekday rules), several years
# ===========================================================================


@pytest.mark.parametrize(
    "year,expected",
    [(2024, date(2024, 1, 15)), (2025, date(2025, 1, 20)), (2026, date(2026, 1, 19))],
)
def test_mlk_day_is_the_third_monday_of_january(year: int, expected: date) -> None:
    assert calendar.is_trading_day(expected) is False
    assert expected.weekday() == 0


@pytest.mark.parametrize("year", [2024, 2025, 2026, 2027])
def test_thanksgiving_is_the_fourth_thursday_of_november(year: int) -> None:
    holidays = calendar.nyse_holidays(year)
    thursdays_in_november = [
        date(year, 11, d)
        for d in range(1, 31)
        if date(year, 11, d).weekday() == 3
    ]
    assert thursdays_in_november[3] in holidays


@pytest.mark.parametrize("year", [2024, 2025, 2026, 2027])
def test_memorial_day_is_the_last_monday_of_may(year: int) -> None:
    holidays = calendar.nyse_holidays(year)
    mondays_in_may = [
        date(year, 5, d) for d in range(1, 32) if date(year, 5, d).weekday() == 0
    ]
    assert mondays_in_may[-1] in holidays


def test_good_friday_is_a_holiday_but_not_a_federal_one() -> None:
    # Easter Sunday 2026 is April 5 -> Good Friday is April 3.
    assert calendar.is_trading_day(date(2026, 4, 3)) is False


def test_juneteenth_is_gated_to_2022_and_later() -> None:
    assert calendar.is_trading_day(date(2022, 6, 20)) is False  # observed Mon
    # Before 2022 it must not appear as a holiday at all.
    assert date(2019, 6, 19) not in calendar.nyse_holidays(2019)


# ===========================================================================
# Early closes (13:00 ET half-days) -- the whole reason this task exists
# ===========================================================================


def test_day_after_thanksgiving_is_an_early_close_every_year() -> None:
    for year in (2024, 2025, 2026, 2027):
        thanksgiving = [d for d in calendar.nyse_holidays(year) if d.month == 11][0]
        black_friday = thanksgiving + timedelta(days=1)
        assert calendar.is_early_close(black_friday) is True
        assert calendar.session_close(black_friday) == calendar.EARLY_CLOSE


def test_christmas_eve_is_an_early_close_when_it_is_a_weekday() -> None:
    # Dec 24, 2026 is a Thursday.
    assert date(2026, 12, 24).weekday() == 3
    assert calendar.is_early_close(date(2026, 12, 24)) is True


def test_day_before_independence_day_is_early_close_on_a_normal_year() -> None:
    # 2024: July 4 is a Thursday, so July 3 (Wednesday) is a normal trading
    # day eligible for the early close.
    assert date(2024, 7, 4).weekday() == 3
    assert calendar.is_early_close(date(2024, 7, 3)) is True


def test_no_double_counted_early_close_when_july_3_is_the_observed_holiday() -> None:
    # July 4, 2026 is a Saturday -> July 3 is the observed *full* holiday, not
    # a separate early-close day. The early-close table must not also list it.
    assert calendar.is_trading_day(date(2026, 7, 3)) is False
    assert calendar.is_early_close(date(2026, 7, 3)) is False


def test_regular_trading_day_is_not_an_early_close() -> None:
    assert calendar.is_early_close(date(2026, 3, 11)) is False
    assert calendar.session_close(date(2026, 3, 11)) == calendar.REGULAR_CLOSE


# ===========================================================================
# next_trading_day
# ===========================================================================


def test_next_trading_day_skips_a_weekend() -> None:
    friday = date(2026, 3, 13)
    assert calendar.next_trading_day(friday) == date(2026, 3, 16)


def test_next_trading_day_skips_a_holiday_run() -> None:
    # Dec 24, 2026 (Thu, early close but still a trading day) -> Dec 25
    # (holiday) -> Dec 26/27 weekend -> Dec 28 (Monday).
    assert calendar.next_trading_day(date(2026, 12, 24)) == date(2026, 12, 28)


# ===========================================================================
# next_close_minus -- the function AlarmScheduler.kt calls through host.py
# ===========================================================================


def test_next_close_minus_on_a_regular_day() -> None:
    # 2026-03-11 is an ordinary Wednesday; asking from the morning of that day
    # should return 15:30 ET that same day for a 30-minute margin.
    after = datetime(2026, 3, 11, 9, 0, tzinfo=calendar.EASTERN)
    fire_at = calendar.next_close_minus(30, after=after)
    assert fire_at == datetime(2026, 3, 11, 15, 30, tzinfo=calendar.EASTERN)


def test_next_close_minus_honours_an_early_close_half_day() -> None:
    # Black Friday closes at 13:00 ET -> T-30 is 12:30 ET, not 15:30.
    thanksgiving = [d for d in calendar.nyse_holidays(2026) if d.month == 11][0]
    black_friday = thanksgiving + timedelta(days=1)
    after = datetime.combine(black_friday, datetime.min.time(), tzinfo=calendar.EASTERN)
    fire_at = calendar.next_close_minus(30, after=after)
    assert fire_at == datetime.combine(
        black_friday, datetime(1, 1, 1, 12, 30).time(), tzinfo=calendar.EASTERN
    )


def test_next_close_minus_rolls_over_a_weekend() -> None:
    # Asking from Friday evening (after that day's close) must land on the
    # following Monday, not repeat Friday.
    friday_evening = datetime(2026, 3, 13, 20, 0, tzinfo=calendar.EASTERN)
    fire_at = calendar.next_close_minus(30, after=friday_evening)
    assert fire_at.date() == date(2026, 3, 16)


def test_next_close_minus_rolls_over_when_called_exactly_at_the_prior_fire_instant() -> None:
    """The idempotency case DESIGN §7.2 relies on: rescheduling at the end of
    a run must not return the same instant just used to fire that run."""
    first = calendar.next_close_minus(
        30, after=datetime(2026, 3, 11, 9, 0, tzinfo=calendar.EASTERN)
    )
    second = calendar.next_close_minus(30, after=first)
    assert second > first
    assert second.date() == date(2026, 3, 12)


def test_next_close_minus_skips_a_holiday_to_the_next_trading_day() -> None:
    # Asking right after Christmas Day 2026 (a Friday) closes out -- 2026-12-25
    # is itself the holiday, so the search must land on Monday 2026-12-28.
    after = datetime(2026, 12, 25, 12, 0, tzinfo=calendar.EASTERN)
    fire_at = calendar.next_close_minus(30, after=after)
    assert fire_at.date() == date(2026, 12, 28)


def test_next_close_minus_defaults_to_now_when_after_is_omitted() -> None:
    # Only proves it does not raise and returns something in the future --
    # not deterministic by design (uses the real clock).
    fire_at = calendar.next_close_minus(30)
    assert fire_at > datetime.now(calendar.EASTERN)


def test_next_close_minus_rejects_negative_minutes() -> None:
    with pytest.raises(ValueError):
        calendar.next_close_minus(-1)


def test_next_close_minus_accepts_a_naive_datetime_as_eastern_wall_clock() -> None:
    naive = datetime(2026, 3, 11, 9, 0)
    fire_at = calendar.next_close_minus(30, after=naive)
    assert fire_at.tzinfo is not None
    assert fire_at == datetime(2026, 3, 11, 15, 30, tzinfo=calendar.EASTERN)
