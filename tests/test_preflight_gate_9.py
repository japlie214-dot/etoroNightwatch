"""XNYS session authority, resolved closes, and fail-closed orchestration."""

import inspect
import json
from datetime import date

import exchange_calendars
import pandas as pd
import pytest

from nightwatch.db.repo import Repo
from nightwatch.engine.preflight import GateResult, GateSpec, gate_9_calendar, run_preflight


@pytest.fixture(scope="module")
def calendar():
    return exchange_calendars.get_calendar("XNYS")


def test_regular_session(calendar) -> None:
    assert gate_9_calendar(trading_date=date(2026, 10, 1), calendar=calendar) == GateResult(
        9, "calendar", True, "XNYS session; close 16:00 ET",
        {
            "close_utc": "2026-10-01T20:00:00+00:00",
            "close_et": "16:00",
            "early_close": False,
            "crosscheck_is_trading_day": True,
            "crosscheck_close_et": "16:00",
            "crosscheck_agrees": True,
        },
    )


def test_default_calendar_matches_injected(calendar) -> None:
    trading_date = date(2026, 10, 1)
    assert gate_9_calendar(trading_date=trading_date) == gate_9_calendar(
        trading_date=trading_date, calendar=calendar,
    )


def test_early_close(calendar) -> None:
    result = gate_9_calendar(trading_date=date(2026, 11, 27), calendar=calendar)
    assert result.passed is True
    assert result.reason == "XNYS session; close 13:00 ET"
    assert result.details["close_et"] == "13:00"
    assert result.details["early_close"] is True
    assert result.details["close_utc"].startswith("2026-11-27T18:00")
    assert result.details["crosscheck_close_et"] == "13:00"
    assert result.details["crosscheck_agrees"] is True


@pytest.mark.parametrize("trading_date, crosscheck", [
    (date(2026, 10, 3), False),
    (date(2026, 12, 25), False),
    (date(2025, 1, 9), True),
    (date(2018, 12, 5), True),
])
def test_non_session(calendar, trading_date: date, crosscheck: bool) -> None:
    assert gate_9_calendar(trading_date=trading_date, calendar=calendar) == GateResult(
        9, "calendar", False, f"{trading_date.isoformat()} is not an XNYS session",
        {"crosscheck_is_trading_day": crosscheck},
    )


@pytest.mark.parametrize("trading_date", [date(2030, 1, 2), date(1900, 1, 2)])
def test_outside_calendar_range(calendar, trading_date: date) -> None:
    assert gate_9_calendar(trading_date=trading_date, calendar=calendar) == GateResult(
        9, "calendar", False,
        f"trading date {trading_date.isoformat()} outside calendar range "
        f"{calendar.first_session.date()}..{calendar.last_session.date()}",
    )


class FakeCalendar:
    sessions = pd.DatetimeIndex(["2026-10-01"])
    first_session = pd.Timestamp("2026-01-02")
    last_session = pd.Timestamp("2026-12-31")

    def session_close(self, ts):
        assert ts == pd.Timestamp("2026-10-01")
        assert ts.tzinfo is None
        return pd.Timestamp("2026-10-01T21:00:00+00:00")


def test_crosscheck_disagreement_does_not_fail() -> None:
    result = gate_9_calendar(trading_date=date(2026, 10, 1), calendar=FakeCalendar())
    assert result.passed is True
    assert result.reason == "XNYS session; close 17:00 ET"
    assert result.details["close_et"] == "17:00"
    assert result.details["early_close"] is True
    assert result.details["crosscheck_is_trading_day"] is True
    assert result.details["crosscheck_close_et"] == "16:00"
    assert result.details["crosscheck_agrees"] is False


def test_close_exception_stops_preflight(repo: Repo) -> None:
    class BrokenCalendar(FakeCalendar):
        def session_close(self, ts):
            raise RuntimeError("session close unavailable")

    session = repo.create_session(
        "2026-10-01", mode="dry_run", environment="demo", armed=False,
    )
    called = []

    def check(number: int) -> GateResult:
        called.append(number)
        return GateResult(number, f"gate {number}", True, "passed")

    gates = [
        GateSpec(number, f"gate {number}", lambda number=number: check(number))
        for number in range(1, 13)
    ]
    gates[8] = GateSpec(9, "calendar", lambda: gate_9_calendar(
        trading_date=date(2026, 10, 1), calendar=BrokenCalendar(),
    ))
    outcome = run_preflight(gates, repo=repo, session_id=session.id)
    assert outcome.passed is False
    assert outcome.failed == GateResult(
        9, "calendar", False, "exception: RuntimeError: session close unavailable",
    )
    assert called == list(range(1, 9))
    rows = repo.get_preflight_checks(session.id)
    assert [row.gate for row in rows] == list(range(1, 10))
    assert rows[-1].passed is False
    assert json.loads(rows[-1].detail)["reason"].startswith("exception:")


def test_does_not_call_is_session() -> None:
    assert "is_session(" not in inspect.getsource(gate_9_calendar)
