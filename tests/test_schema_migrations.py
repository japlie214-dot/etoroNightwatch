"""Schema creation and the forward-only migration runner (DESIGN §3)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nightwatch.db import migrations
from nightwatch.db.repo import Repo, connect

#: Every table DESIGN §3 names, plus `commands` (task brief §1) and the two
#: implementation-detail tables (`schema_version`, `portfolio_snapshot_positions`).
EXPECTED_TABLES = {
    "bars",
    "instruments",
    "sessions",
    "preflight_checks",
    "decisions",
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
