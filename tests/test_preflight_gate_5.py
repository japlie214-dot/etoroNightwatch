"""Database verdicts and fail-fast persistence against a temporary repo."""

import sqlite3

import pytest

from nightwatch.db import migrations
from nightwatch.db.repo import Intent, Repo
from nightwatch.engine.preflight import GateResult, GateSpec, gate_5_database, run_preflight
from nightwatch.host import DesktopHost


@pytest.fixture
def session_id(repo: Repo) -> int:
    return repo.create_session(
        "2026-10-02", mode="dry_run", environment="demo", armed=False,
    ).id


def test_current_session_only_passes(repo: Repo, session_id: int) -> None:
    assert gate_5_database(repo=repo, session_id=session_id) == GateResult(
        5, "database", True, "database ok",
        {"schema_version": migrations.discover()[-1].version},
    )


def test_unfinished_prior_session_fails(repo: Repo, session_id: int) -> None:
    prior = repo.create_session(
        "2026-10-01", mode="dry_run", environment="demo", armed=False,
    )
    result = gate_5_database(repo=repo, session_id=session_id)
    assert result.passed is False
    assert result.reason == (
        f"1 unfinished prior session(s): 2026-10-01 (id {prior.id}, state started)"
    )
    assert result.details["unfinished_ids"] == [prior.id]


@pytest.mark.parametrize("state", ["finished", "aborted", "failed"])
def test_terminal_prior_session_passes(repo: Repo, session_id: int, state: str) -> None:
    prior = repo.create_session(
        "2026-10-01", mode="dry_run", environment="demo", armed=False,
    )
    assert gate_5_database(repo=repo, session_id=session_id).passed is False
    repo.finish_session(prior.id, state=state)
    assert gate_5_database(repo=repo, session_id=session_id).passed is True


def test_schema_mismatch_fails(repo: Repo, session_id: int) -> None:
    repo.conn.execute(
        "INSERT INTO schema_version (version, name, applied_at) VALUES (999, 'future', 'x')"
    )
    repo.conn.commit()
    assert gate_5_database(repo=repo, session_id=session_id) == GateResult(
        5, "database", False,
        f"schema version 999, code expects {migrations.discover()[-1].version}",
    )


def test_quick_check_failure(repo: Repo, session_id: int, monkeypatch) -> None:
    real = repo.conn

    class BadQuickCheck:
        def execute(self, sql, *args):
            if sql == "PRAGMA quick_check":
                return self
            return real.execute(sql, *args)

        def fetchall(self):
            return [("*** in database main ***",)]

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr(repo, "conn", BadQuickCheck())
    assert gate_5_database(repo=repo, session_id=session_id) == GateResult(
        5, "database", False, "database quick_check failed: *** in database main ***",
        {"quick_check": ["*** in database main ***"]},
    )


def test_closed_connection_exception_stops_and_persists(
    host: DesktopHost, repo: Repo, session_id: int,
) -> None:
    bad = Repo.open(host.data_dir() / "nightwatch.db", host=host)
    bad.close()
    with pytest.raises(sqlite3.ProgrammingError):
        gate_5_database(repo=bad, session_id=session_id)
    gates = [
        GateSpec(number, f"gate {number}", lambda number=number: GateResult(
            number, f"gate {number}", True, "passed",
        ))
        for number in range(1, 13)
    ]
    gates[4] = GateSpec(5, "database", lambda: gate_5_database(repo=bad, session_id=session_id))
    outcome = run_preflight(gates, repo=repo, session_id=session_id)
    assert outcome.passed is False
    assert outcome.failed is not None
    assert outcome.failed.gate == 5
    assert outcome.failed.reason.startswith("exception: ProgrammingError")
    rows = repo.get_preflight_checks(session_id)
    assert [row.gate for row in rows] == [1, 2, 3, 4, 5]
    assert [row.passed for row in rows] == [True, True, True, True, False]


@pytest.mark.parametrize("status, sent", [
    ("abandoned", False), ("abandoned", True), ("pending", False),
    ("sent", True), ("filled_unannotated", True),
])
def test_gate_5_ignores_only_abandoned_unsent_and_persists_visibility(repo, session_id, status, sent):
    ignored_id = repo.insert_intent(Intent(
        session_id, "ignored-unsent", 1, "UPRO", "buy", amount_usd=50.0,
        status="abandoned",
    ))
    intent_id = repo.insert_intent(Intent(
        session_id, "candidate", 2, "UPRO", "buy", amount_usd=50.0,
    ))
    if sent:
        repo.set_intent_status(intent_id, "sent")
    repo.set_intent_status(intent_id, status, resolved=status == "abandoned")
    ignored = [ignored_id, intent_id] if status == "abandoned" and not sent else [ignored_id]
    gates = [GateSpec(n, f"gate {n}", lambda n=n: GateResult(n, f"gate {n}", True, "ok"))
             for n in range(1, 13)]
    gates[4] = GateSpec(5, "database", lambda: gate_5_database(repo=repo, session_id=session_id))
    outcome = run_preflight(gates, repo=repo, session_id=session_id)
    result = outcome.results[4]
    assert result.passed == (len(ignored) == 2)
    assert result.details["ignored_abandoned_unsent_intent_ids"] == ignored
    if not result.passed:
        assert result.details["unresolved_intent_ids"] == [intent_id]
        assert outcome.failed.gate == 5
    else:
        assert [r.gate for r in outcome.results] == list(range(1, 13))
    import json

    persisted = repo.get_preflight_checks(session_id)[4]
    assert json.loads(persisted.detail)["details"]["ignored_abandoned_unsent_intent_ids"] == ignored
    # Gate 5 does not change crash-recovery classification or the daily cap.
    assert [i.id for i in repo.unresolved_intents(session_id)] == [ignored_id, intent_id]
    assert repo.reserved_buy_usd("2026-10-02") == 100.0


def test_gate_writes_nothing(repo: Repo, session_id: int) -> None:
    before = repo.conn.total_changes
    gate_5_database(repo=repo, session_id=session_id)
    assert repo.conn.total_changes == before
