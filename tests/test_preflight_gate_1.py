"""Halt flag verdicts and fail-fast persistence against a temporary host."""

from pathlib import Path

from nightwatch.cli import clear_halt, set_halt
from nightwatch.db.repo import Repo
from nightwatch.engine.preflight import (
    GateResult,
    GateSpec,
    gate_1_halt,
    run_preflight,
    unimplemented_gate,
)
from nightwatch.host import DesktopHost


def test_no_halt_flag_passes(tmp_path: Path) -> None:
    host = DesktopHost(root=tmp_path)
    assert gate_1_halt(host) == GateResult(
        1, "Halt switch", True, "no HALT flag",
        {"path": str(host.halt_flag_path())},
    )


def test_set_halt_fails(tmp_path: Path) -> None:
    host = DesktopHost(root=tmp_path)
    path = set_halt(host)
    result = gate_1_halt(host)
    assert result.passed is False
    assert "HALT flag present" in result.reason
    assert result == GateResult(
        1, "Halt switch", False, f"HALT flag present: {path}", {"path": str(path)},
    )


def test_clear_halt_passes_again(tmp_path: Path) -> None:
    host = DesktopHost(root=tmp_path)
    assert gate_1_halt(host).passed is True
    set_halt(host)
    assert gate_1_halt(host).passed is False
    assert clear_halt(host) is True
    assert gate_1_halt(host).passed is True


def test_halt_stops_preflight_and_persists_one_failure(tmp_path: Path, repo: Repo) -> None:
    host = DesktopHost(root=tmp_path)
    set_halt(host)
    session = repo.create_session(
        "2026-10-01", mode="dry_run", environment="demo", armed=False,
    )
    gates = [GateSpec(1, "Halt switch", lambda: gate_1_halt(host))]
    gates.extend(unimplemented_gate(number, f"gate {number}") for number in range(2, 13))
    outcome = run_preflight(gates, repo=repo, session_id=session.id)
    assert outcome.passed is False
    assert outcome.failed is not None
    assert outcome.failed.gate == 1
    rows = repo.get_preflight_checks(session.id)
    assert len(rows) == 1
    assert rows[0].gate == 1
    assert rows[0].passed is False
