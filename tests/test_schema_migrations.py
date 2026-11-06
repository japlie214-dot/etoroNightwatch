"""Schema creation and the forward-only migration runner (DESIGN §3)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nightwatch.db import migrations
from nightwatch.db.repo import Intent, Repo, connect

#: Every table DESIGN §3 names, plus `commands` (task brief §1) and the two
#: implementation-detail tables (`schema_version`, `portfolio_snapshot_positions`).
EXPECTED_TABLES = {
    "bars",
    "instruments",
    "sessions",
    "preflight_checks",
    "decisions",
    "close_observations",
    "intents",
    "orders",
    "lots",
    "lot_annotations",
    "buckets",
    "strategy_state",
    "schg_daily_values",
    "budget_history",
    "portfolio_snapshots",
    "portfolio_snapshot_positions",
    "commands",
    "notifications",
    "schema_version",
    "partial_close_effects",
}


def test_migrate_creates_every_design_table(tmp_path: Path) -> None:
    conn = connect(tmp_path / "nightwatch.db")
    try:
        version = migrations.migrate(conn)
        # Not BASELINE_VERSION (1) any more: migration 002 (§4.2/§10.7's
        # portfolio_snapshot_positions.invested -> amount rename) always runs
        # after the baseline, even against a fresh database. Compare against
        # the newest discovered migration rather than hardcoding a number, so
        # this test doesn't need editing again the next time one is added.
        assert version == migrations.discover()[-1].version
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        tables = {r[0] for r in rows}
        missing = EXPECTED_TABLES - tables
        assert not missing, f"missing tables: {missing}"
    finally:
        conn.close()


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    conn = connect(tmp_path / "nightwatch.db")
    try:
        first = migrations.migrate(conn)
        second = migrations.migrate(conn)
        assert first == second == migrations.discover()[-1].version
    finally:
        conn.close()


def test_failed_version_record_rolls_back_ddl_and_can_retry(tmp_path: Path) -> None:
    conn = connect(tmp_path / "nightwatch.db")
    try:
        baseline = migrations.discover()[0]
        migrations.current_version(conn)  # create schema_version
        conn.executescript(f"BEGIN;\n{baseline.sql}\nCOMMIT;")
        conn.execute(
            "INSERT INTO schema_version (version, name, applied_at) "
            "VALUES (?, ?, ?)",
            (baseline.version, baseline.name, "test"),
        )
        conn.commit()
        conn.execute(
            "CREATE TRIGGER reject_v2 BEFORE INSERT ON schema_version "
            "WHEN NEW.version = 2 BEGIN SELECT RAISE(ABORT, 'test failure'); END"
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError, match="test failure"):
            migrations.migrate(conn)

        columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(portfolio_snapshot_positions)"
        )}
        assert "invested" in columns
        assert "amount" not in columns
        assert migrations.current_version(conn) == baseline.version

        conn.execute("DROP TRIGGER reject_v2")
        conn.commit()
        assert migrations.migrate(conn) == migrations.discover()[-1].version
        columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(portfolio_snapshot_positions)"
        )}
        assert "amount" in columns
        assert "invested" not in columns
    finally:
        conn.close()


def schg_leg(repo, request="schg-request"):
    sid = repo.create_session("2026-10-30", "armed", "demo", True).id
    intent = Intent(sid, request, 1, "SCHG", "close", instrument_id=42, units=2, position_id="100")
    return intent


def test_migration_011_upgrades_version_10(tmp_path, monkeypatch):
    all_migrations = migrations.discover()
    conn = connect(tmp_path / "upgrade.db")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(migrations, "discover", lambda: [m for m in all_migrations if m.version <= 10])
            assert migrations.migrate(conn) == 10
        assert migrations.migrate(conn) == migrations.discover()[-1].version
        assert "partial_close_effects" in Repo(conn).table_names()
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


@pytest.mark.parametrize("upgrade", [False, True])
def test_migration_012_accepts_unannotated_fill_and_preserves_references(tmp_path, monkeypatch, upgrade):
    from nightwatch.db.repo import Order

    all_migrations = migrations.discover()
    conn = connect(tmp_path / "fills.db")
    try:
        if upgrade:
            with monkeypatch.context() as patch:
                patch.setattr(migrations, "discover", lambda: [m for m in all_migrations if m.version <= 11])
                assert migrations.migrate(conn) == 11
            repo = Repo(conn)
            intent = schg_leg(repo)
            iid, lid = repo.begin_schg_close_leg(intent, position_id="100", units_before=4, units_deducted=2)
            oid = repo.insert_order(Order(iid, intent.session_id, intent.request_id, "SCHG", "close"))
            repo.record_close_observation(intent_id=iid, order_row_id=oid, order_id="broker-order",
                                          position_id="100", observed_status="unresolved")
            repo.conn.execute("UPDATE sqlite_sequence SET seq = 99 WHERE name = 'intents'")
            repo.conn.commit()
            original = dict(conn.execute("SELECT * FROM intents WHERE id = ?", (iid,)).fetchone())
            with pytest.raises(sqlite3.IntegrityError):
                repo.set_intent_status(iid, "filled_unannotated")
            conn.rollback()
        assert migrations.migrate(conn) == 12
        repo = Repo(conn)
        if upgrade:
            assert dict(conn.execute("SELECT * FROM intents WHERE id = ?", (iid,)).fetchone()) == original
            assert repo.get_order(oid).intent_id == iid
            assert repo.pending_partial_closes()[0]["id"] == lid
            assert repo.list_close_observations()[0]["intent_id"] == iid
            sid = intent.session_id
        else:
            sid = repo.create_session("2026-10-30", "live", "demo", True).id
        new = repo.insert_intent(Intent(sid, "unannotated", 2, "SCHG", "buy",
                                        amount_usd=100, status="filled_unannotated"))
        assert new == (100 if upgrade else 1)
        assert repo.reserved_buy_usd("2026-10-30") == 100
        assert repo.unresolved_intents(sid)[-1].id == new
        assert repo.unresolved_buy_intents()[0][0].id == new
        with pytest.raises(sqlite3.IntegrityError):
            repo.set_intent_status(new, "invalid")
        conn.rollback()
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_migration_012_failure_rolls_back_and_can_retry(tmp_path, monkeypatch):
    all_migrations = migrations.discover()
    conn = connect(tmp_path / "rollback-fill.db")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(migrations, "discover", lambda: [m for m in all_migrations if m.version <= 11])
            assert migrations.migrate(conn) == 11
        conn.execute("CREATE TRIGGER reject_v12 BEFORE INSERT ON schema_version "
                     "WHEN NEW.version = 12 BEGIN SELECT RAISE(ABORT, 'reject 012'); END")
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="reject 012"):
            migrations.migrate(conn)
        assert migrations.current_version(conn) == 11
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'intents_new'").fetchall() == []
        assert "filled_unannotated" not in conn.execute("SELECT sql FROM sqlite_master WHERE name = 'intents'").fetchone()[0]
        conn.execute("DROP TRIGGER reject_v12")
        conn.commit()
        assert migrations.migrate(conn) == 12
    finally:
        conn.close()


def test_begin_schg_leg_rolls_back_intent_on_ledger_failure(repo):
    intent = schg_leg(repo)
    repo.conn.execute("CREATE TRIGGER fail_leg BEFORE INSERT ON partial_close_effects "
                      "BEGIN SELECT RAISE(ABORT, 'simulated crash'); END")
    repo.conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="simulated crash"):
        repo.begin_schg_close_leg(intent, position_id="100", units_before=4, units_deducted=2)
    assert repo.get_intents(intent.session_id) == []
    assert repo.pending_partial_closes() == []
    assert not repo.conn.in_transaction


@pytest.mark.parametrize("status,intent_status", [("confirmed", "filled"), ("rejected", "rejected")])
def test_finish_schg_leg_atomic_and_final_state_guard(repo, status, intent_status):
    intent = schg_leg(repo)
    iid, lid = repo.begin_schg_close_leg(intent, position_id="100", units_before=4, units_deducted=2)
    repo.conn.execute("CREATE TRIGGER fail_finish BEFORE UPDATE ON intents "
                      "BEGIN SELECT RAISE(ABORT, 'simulated finish crash'); END")
    repo.conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="simulated finish crash"):
        repo.finish_schg_close_leg(lid, status=status, intent_status=intent_status, resolved=True)
    assert repo.pending_partial_closes()[0]["status"] == "pending"
    assert repo.get_intent_by_request_id(intent.request_id).status == "sent"
    repo.conn.execute("DROP TRIGGER fail_finish")
    repo.conn.commit()
    repo.finish_schg_close_leg(lid, status=status, intent_status=intent_status, resolved=True,
                               units_after=2 if status == "confirmed" else None)
    saved = repo.get_intent_by_request_id(intent.request_id)
    assert saved.id == iid and saved.status == intent_status and saved.resolved_at
    with pytest.raises(ValueError, match="final"):
        repo.update_partial_close(lid, status="unresolved")
    with pytest.raises(ValueError, match="final"):
        repo.finish_schg_close_leg(lid, status="unresolved", intent_status="sent", resolved=False)
    assert repo.get_intent_by_request_id(intent.request_id) == saved
    assert not repo.pending_partial_closes()


def test_partial_close_unique_check_and_unverified_selection(repo):
    intent = schg_leg(repo)
    iid, lid = repo.begin_schg_close_leg(intent, position_id="100", units_before=4, units_deducted=2)
    with pytest.raises(sqlite3.IntegrityError):
        repo.insert_partial_close(iid, position_id="100", units_before=4, units_deducted=2)
    with pytest.raises(sqlite3.IntegrityError):
        repo.update_partial_close(lid, status="made_up")
    assert repo.pending_partial_closes()[0]["status"] == "pending"
    for status in ("unresolved", "unverified"):
        repo.finish_schg_close_leg(lid, status=status, intent_status="sent", resolved=False)
        assert repo.pending_partial_closes()[0]["status"] == status
        assert repo.confirmed_partial_closes(["100"]) == []
    with pytest.raises(ValueError, match="inconsistent"):
        repo.finish_schg_close_leg(lid, status="unverified", intent_status="filled", resolved=True)


def test_pragmas_are_set(tmp_path: Path) -> None:
    conn = connect(tmp_path / "nightwatch.db")
    try:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert journal_mode.lower() == "wal"
        assert foreign_keys == 1
    finally:
        conn.close()


def test_repo_open_reports_schema_version(tmp_path: Path) -> None:
    with Repo.open(tmp_path / "nightwatch.db") as repo:
        assert repo.schema_version() == migrations.discover()[-1].version
        assert set(repo.table_names()) >= EXPECTED_TABLES


def test_migration_008_adds_tp_clears_to_a_version_7_database(tmp_path: Path, monkeypatch) -> None:
    all_migrations = migrations.discover()
    upto_7 = [m for m in all_migrations if m.version <= 7]
    assert upto_7[-1].version == 7
    conn = connect(tmp_path / "nightwatch.db")
    try:
        monkeypatch.setattr(migrations, "discover", lambda: upto_7)
        assert migrations.migrate(conn) == 7
        has_table = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'tp_clears'"
        ).fetchone()[0]
        assert has_table == 0
        monkeypatch.setattr(migrations, "discover", lambda: [m for m in all_migrations if m.version <= 8])
        assert migrations.migrate(conn) == 8
        columns = {r[1] for r in conn.execute("PRAGMA table_info(tp_clears)").fetchall()}
        assert columns == {
            "id", "order_id", "position_id", "request_id", "attempt",
            "status", "detail", "created_at", "resolved_at",
        }
    finally:
        conn.close()


def test_fresh_database_has_tp_clears(tmp_path: Path) -> None:
    conn = connect(tmp_path / "nightwatch.db")
    try:
        migrations.migrate(conn)
        names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
            ).fetchall()
        }
        assert "tp_clears" in names
        assert "idx_tp_clears_position" in names
    finally:
        conn.close()


@pytest.mark.parametrize("keep_row", [True, False])
def test_migration_009_preserves_decisions_and_sequence(tmp_path: Path, monkeypatch, keep_row) -> None:
    all_migrations = migrations.discover()
    with connect(tmp_path / "upgraded.db") as conn:
        with monkeypatch.context() as patch:
            patch.setattr(migrations, "discover", lambda: [m for m in all_migrations if m.version <= 8])
            assert migrations.migrate(conn) == 8
        repo = Repo(conn)
        session = repo.create_session("2026-10-23", "dry_run", "demo", False)
        conn.execute(
            "INSERT INTO decisions (id, session_id, trading_date, created_at, spy_close) "
            "VALUES (42, ?, '2026-10-23', 'original-time', 600.25)", (session.id,),
        )
        conn.commit()
        # Before: the old UNIQUE(session_id) prevents a second decision.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO decisions (session_id, trading_date, created_at) VALUES (?, 'date', 'time')",
                (session.id,),
            )
        conn.rollback()
        original = dict(conn.execute("SELECT * FROM decisions").fetchone())
        conn.execute("UPDATE sqlite_sequence SET seq = 99 WHERE name = 'decisions'")
        if not keep_row:
            conn.execute("DELETE FROM decisions")
        conn.commit()
        assert migrations.migrate(conn) == migrations.discover()[-1].version
        if keep_row:
            copied = dict(conn.execute("SELECT * FROM decisions WHERE id = 42").fetchone())
            assert copied.pop("phase") == "single"
            assert copied == original
        assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'decisions'").fetchone()[0] == 99
        for phase, expected_id in [("A", 100), ("B", 101)]:
            cur = conn.execute(
                "INSERT INTO decisions (session_id, trading_date, created_at, phase) VALUES (?, 'date', 'time', ?)",
                (session.id, phase),
            )
            assert cur.lastrowid == expected_id
        conn.commit()
        for phase in ("A", "invalid"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO decisions (session_id, trading_date, created_at, phase) VALUES (?, 'date', 'time', ?)",
                    (session.id, phase),
                )
            conn.rollback()
        assert conn.execute("SELECT intents_json FROM decisions WHERE phase = 'A'").fetchone()[0] == "[]"
        conn.execute("DELETE FROM sessions WHERE id = ?", (session.id,))
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        fresh = connect(tmp_path / "fresh.db")
        try:
            assert migrations.migrate(fresh) == migrations.discover()[-1].version
            ddl = "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            assert [tuple(r) for r in conn.execute(ddl)] == [tuple(r) for r in fresh.execute(ddl)]
        finally:
            fresh.close()
    conn.close()


def test_migration_009_rebuild_rolls_back_on_version_failure(tmp_path: Path, monkeypatch) -> None:
    all_migrations = migrations.discover()
    conn = connect(tmp_path / "rollback.db")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(migrations, "discover", lambda: [m for m in all_migrations if m.version <= 8])
            assert migrations.migrate(conn) == 8
        conn.execute(
            "CREATE TRIGGER reject_v9 BEFORE INSERT ON schema_version "
            "WHEN NEW.version = 9 BEGIN SELECT RAISE(ABORT, 'reject 009'); END"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="reject 009"):
            migrations.migrate(conn)
        assert migrations.current_version(conn) == 8
        assert "phase" not in {r[1] for r in conn.execute("PRAGMA table_info(decisions)")}
        assert conn.execute("SELECT name FROM sqlite_master WHERE name IN ('decisions_new', 'close_observations')").fetchall() == []
        conn.execute("DROP TRIGGER reject_v9")
        conn.commit()
        assert migrations.migrate(conn) == migrations.discover()[-1].version
    finally:
        conn.close()
