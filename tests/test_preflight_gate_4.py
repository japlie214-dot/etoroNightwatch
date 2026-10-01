"""Clock skew verdicts and fail-fast persistence against a temporary repo."""

from datetime import datetime, timedelta, timezone

import pytest

from nightwatch.db.repo import Repo
from nightwatch.engine.preflight import (
    GateResult,
    GateSpec,
    gate_4_clock,
    run_preflight,
    unimplemented_gate,
)

SERVER_DATE = "Fri, 02 Jan 2026 12:00:00 GMT"
SERVER_NOW = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize("skew", [0, 59, -59, 60, 61, -61])
def test_clock_skew(skew: int) -> None:
    local_now = SERVER_NOW + timedelta(seconds=skew)
    passed = abs(skew) <= 60
    result = gate_4_clock(local_now=local_now, server_date=SERVER_DATE)
    assert result == GateResult(
        4, "Clock", passed,
        f"clock skew {skew:+.1f}s {'within' if passed else 'exceeds'} 60s",
        {"skew_seconds": skew, "server_date": SERVER_DATE, "local_now": local_now.isoformat()},
    )


def test_missing_server_date_fails() -> None:
    assert gate_4_clock(local_now=SERVER_NOW, server_date=None) == GateResult(
        4, "Clock", False, "no server Date header observed",
        {"local_now": SERVER_NOW.isoformat()},
    )


@pytest.mark.parametrize("server_date", ["garbage", "Fri, 02 Jan 2026 12:00:00"])
def test_unparseable_server_date_fails(server_date: str) -> None:
    assert gate_4_clock(local_now=SERVER_NOW, server_date=server_date) == GateResult(
        4, "Clock", False, f"unparseable server Date header: {server_date!r}",
        {"server_date": server_date, "local_now": SERVER_NOW.isoformat()},
    )


def test_naive_local_now_raises() -> None:
    with pytest.raises(ValueError, match="^local_now must be timezone-aware$"):
        gate_4_clock(local_now=SERVER_NOW.replace(tzinfo=None), server_date=SERVER_DATE)


def test_non_utc_local_now_same_instant_passes() -> None:
    local_now = SERVER_NOW.astimezone(timezone(timedelta(hours=7)))
    assert gate_4_clock(local_now=local_now, server_date=SERVER_DATE) == GateResult(
        4, "Clock", True, "clock skew +0.0s within 60s",
        {"skew_seconds": 0, "server_date": SERVER_DATE, "local_now": local_now.isoformat()},
    )


def test_clock_skew_stops_preflight_and_persists_four_rows(repo: Repo) -> None:
    session = repo.create_session(
        "2026-01-02", mode="dry_run", environment="demo", armed=False,
    )
    gates = [
        GateSpec(number, f"gate {number}", lambda number=number: GateResult(
            number, f"gate {number}", True, "passed",
        ))
        for number in range(1, 4)
    ]
    gates.append(GateSpec(4, "Clock", lambda: gate_4_clock(
        local_now=SERVER_NOW + timedelta(seconds=120), server_date=SERVER_DATE,
    )))
    gates.extend(unimplemented_gate(number, f"gate {number}") for number in range(5, 13))
    outcome = run_preflight(gates, repo=repo, session_id=session.id)
    assert outcome.passed is False
    assert outcome.failed is not None
    assert outcome.failed.gate == 4
    assert outcome.failed.reason == "clock skew +120.0s exceeds 60s"
    rows = repo.get_preflight_checks(session.id)
    assert len(rows) == 4
    assert [row.gate for row in rows] == [1, 2, 3, 4]
    assert [row.passed for row in rows] == [True, True, True, False]
