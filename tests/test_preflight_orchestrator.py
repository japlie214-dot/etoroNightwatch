"""Ordered, fail-closed preflight execution against a real temporary repo."""

import json
from datetime import date

import pytest

from nightwatch.db.repo import Repo
from nightwatch.engine.preflight import (
    GateResult,
    GateSpec,
    gate_11_reconciliation,
    run_preflight,
    unimplemented_gate,
)


@pytest.fixture
def session_id(repo: Repo) -> int:
    return repo.create_session(
        "2026-10-01", mode="dry_run", environment="demo", armed=False,
    ).id


def _gates(called: list[int]) -> list[GateSpec]:
    def check(number: int) -> GateResult:
        called.append(number)
        return GateResult(number, f"gate {number}", True, "ok")

    return [
        GateSpec(number, f"gate {number}", lambda number=number: check(number))
        for number in range(1, 13)
    ]


def test_all_twelve_pass_and_persist_in_gate_order(repo: Repo, session_id: int) -> None:
    called = []
    outcome = run_preflight(_gates(called), repo=repo, session_id=session_id)
    assert outcome.passed is True
    assert outcome.failed is None
    assert isinstance(outcome.results, tuple)
    assert called == list(range(1, 13))
    checks = repo.get_preflight_checks(session_id)
    assert [r.gate for r in outcome.results] == called
    assert [r.gate for r in checks] == called
    assert all(r.passed for r in checks)
    assert [r.name for r in checks] == [r.name for r in outcome.results]


def test_gate_three_failure_stops_later_checks(repo: Repo, session_id: int) -> None:
    called = []
    gates = _gates(called)

    def fail() -> GateResult:
        called.append(3)
        return GateResult(3, "network", False, "offline")

    gates[2] = GateSpec(3, "network", fail)
    outcome = run_preflight(gates, repo=repo, session_id=session_id)
    assert outcome.passed is False
    assert outcome.failed == GateResult(3, "network", False, "offline")
    assert outcome.failed is outcome.results[-1]
    assert called == [1, 2, 3]
    checks = repo.get_preflight_checks(session_id)
    assert [r.gate for r in checks] == called
    assert checks[-1].passed is False
    assert checks[-1].name == "network"


def test_gate_five_exception_is_persisted_and_stops_run(repo: Repo, session_id: int) -> None:
    called = []
    gates = _gates(called)

    def explode() -> GateResult:
        called.append(5)
        raise RuntimeError("boom")

    gates[4] = GateSpec(5, "database", explode)
    outcome = run_preflight(gates, repo=repo, session_id=session_id)
    assert outcome.passed is False
    assert outcome.failed == GateResult(5, "database", False, "exception: RuntimeError: boom")
    assert called == [1, 2, 3, 4, 5]
    checks = repo.get_preflight_checks(session_id)
    assert [r.gate for r in checks] == called
    assert json.loads(checks[-1].detail)["reason"] == "exception: RuntimeError: boom"
    row = repo.conn.execute(
        "SELECT passed FROM preflight_checks WHERE session_id = ? AND gate = 5",
        (session_id,),
    ).fetchone()
    assert row["passed"] == 0


@pytest.mark.parametrize("invalid", ["missing", "duplicate", "extra"])
def test_invalid_gate_list_runs_nothing_and_writes_no_rows(
    repo: Repo, session_id: int, invalid: str,
) -> None:
    called = []
    gates = _gates(called)
    if invalid == "missing":
        gates.pop()
    elif invalid == "duplicate":
        gates[-1] = gates[0]
    else:
        gates.append(unimplemented_gate(13, "extra"))
    with pytest.raises(ValueError, match="gates 1..12"):
        run_preflight(gates, repo=repo, session_id=session_id)
    assert called == []
    assert repo.get_preflight_checks(session_id) == []


def test_out_of_order_gates_run_in_ascending_order(repo: Repo, session_id: int) -> None:
    called = []
    gates = _gates(called)
    outcome = run_preflight(gates[::-1], repo=repo, session_id=session_id)
    assert outcome.passed is True
    assert called == list(range(1, 13))
    assert [r.gate for r in outcome.results] == called


def test_wrong_result_gate_fails_at_the_spec_number(repo: Repo, session_id: int) -> None:
    called = []
    gates = _gates(called)
    gates[2] = GateSpec(3, "network", lambda: GateResult(9, "wrong", True, "ok"))
    outcome = run_preflight(gates, repo=repo, session_id=session_id)
    assert outcome.passed is False
    assert outcome.failed == GateResult(3, "network", False, "gate 3 returned result for gate 9")
    assert called == [1, 2]
    checks = repo.get_preflight_checks(session_id)
    assert [r.gate for r in checks] == [1, 2, 3]
    assert checks[-1].passed is False
    assert json.loads(checks[-1].detail)["reason"] == outcome.failed.reason


def test_unimplemented_environment_gate_fails_closed(repo: Repo, session_id: int) -> None:
    called = []
    gates = _gates(called)
    gates[6] = unimplemented_gate(7, "environment")
    assert gates[6].check() == GateResult(7, "environment", False, "not implemented")
    outcome = run_preflight(gates, repo=repo, session_id=session_id)
    assert outcome.passed is False
    assert outcome.failed == gates[6].check()
    assert called == list(range(1, 7))
    assert [r.gate for r in repo.get_preflight_checks(session_id)] == list(range(1, 8))


def test_real_gate_eleven_runs_and_persists(repo: Repo, session_id: int) -> None:
    gates = _gates([])
    gates[10] = GateSpec(11, "Reconciliation", lambda: gate_11_reconciliation(
        [], [], managed_symbols=("SCHG", "UPRO"), instrument_symbols={},
    ))
    outcome = run_preflight(gates, repo=repo, session_id=session_id)
    assert outcome.passed is True
    result = outcome.results[10]
    check = repo.get_preflight_checks(session_id)[10]
    assert result.gate == check.gate == 11
    assert result.name == check.name == "Reconciliation"
    assert check.passed is True
    assert json.loads(check.detail) == {"reason": result.reason, "details": result.details}
    assert result.details == {"discrepancies": [], "adoptions": [], "resolutions": []}


def test_detail_json_round_trips_and_stringifies_non_json_values(repo: Repo, session_id: int) -> None:
    gates = _gates([])
    details = {"z": [1, True], "a": date(2026, 10, 1)}
    gates[0] = GateSpec(1, "halt", lambda: GateResult(1, "halt", True, "clear", details))
    run_preflight(gates, repo=repo, session_id=session_id)
    detail = repo.get_preflight_checks(session_id)[0].detail
    assert json.loads(detail) == {"reason": "clear", "details": {"z": [1, True], "a": "2026-10-01"}}
    assert detail == json.dumps({"reason": "clear", "details": details}, default=str, sort_keys=True)


def test_running_twice_for_same_session_acceptably_overwrites_rows(repo: Repo, session_id: int) -> None:
    gates = _gates([])
    run_preflight(gates, repo=repo, session_id=session_id)
    gates[11] = unimplemented_gate(12, "risk caps")
    outcome = run_preflight(gates, repo=repo, session_id=session_id)
    checks = repo.get_preflight_checks(session_id)
    assert len(checks) == 12
    assert outcome.failed == GateResult(12, "risk caps", False, "not implemented")
    assert checks[-1].passed is False
    assert checks[-1].name == "risk caps"
    assert json.loads(checks[-1].detail)["reason"] == "not implemented"


def test_keyboard_interrupt_propagates_without_recording_the_interrupted_gate(
    repo: Repo, session_id: int,
) -> None:
    called = []
    gates = _gates(called)

    def interrupt() -> GateResult:
        raise KeyboardInterrupt()

    gates[1] = GateSpec(2, "power", interrupt)
    with pytest.raises(KeyboardInterrupt):
        run_preflight(gates, repo=repo, session_id=session_id)
    assert called == [1]
    assert [r.gate for r in repo.get_preflight_checks(session_id)] == [1]
