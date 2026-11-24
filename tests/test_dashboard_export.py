"""Synthetic-only proof for the bounded, read-only DEMO cloud export."""

import json
import sqlite3
from pathlib import Path

import pytest

from nightwatch.db.repo import Decision, Intent, Order, PortfolioSnapshot, Repo, SnapshotPosition
from nightwatch.notify.dashboard_export import dashboard_snapshot, export_json

MARKER = "PRIVATE_RAW_MARKER"


def seed_demo(home, *, count=2, sessions=1, environment="demo", complete=True):
    repo = Repo.open(home / "data" / "nightwatch.db")
    for day in range(1, sessions + 1):
        session = repo.create_session(f"2026-10-{day:02d}", "dry_run", environment, False)
        repo.update_session(session.id, state="finished", preflight_verdict="passed",
                            next_run_at="2026-11-01T19:30:00Z")
        repo.insert_decision(Decision(session.id, session.trading_date, reason="Hold: token=SECRET " + "日 " * 400,
                                     intents=[{"order_id": MARKER}], cone_z=-1.5, drawdown=-0.1,
                                     budget=1200, order_size=50, tier=1))
    repo.insert_snapshot(PortfolioSnapshot(session.id, "2026-10-30T19:59:00Z", equity=15000,
        cash=2500, available_cash=2300, raw={"complete": complete, "broker": MARKER,
            "sleeve": {"cash_reserve_usd": 1000, "sleeve_cash": 1300,
                       "sleeve_equity": 5000, "credential": MARKER}},
        positions=[SnapshotPosition(position_id=f"{MARKER}_{i}", symbol="SCHG" if i % 2 else "UPRO",
            units=2, entry_price=50, amount=100, unrealized_pnl=10 if complete else None,
            open_date="2026-01-02", instrument_id=1234) for i in range(count)] + [
            SnapshotPosition(position_id=MARKER, symbol="AAPL", amount=999)]))
    intent = repo.insert_intent(Intent(session.id, MARKER, 1, "SCHG", "buy"))
    repo.insert_order(Order(intent, session.id, MARKER, "SCHG", "buy", order_id=MARKER))
    return repo


@pytest.fixture
def demo(tmp_path):
    home = tmp_path / "nightwatch_demo"
    repo = seed_demo(home)
    yield home, repo
    repo.close()


def test_allowlist_values_and_reasons(demo):
    home, repo = demo
    value = dashboard_snapshot(repo)
    assert value["environment"] == "demo"
    assert value["account"] == {"total_equity_usd": 15000, "total_cash_usd": 2500,
                                "broker_available_cash_usd": 2300}
    assert value["sleeve"]["cash_reserve_usd"] == 1000
    assert value["sleeve"]["sleeve_unrealized_pnl_usd"] == 20
    assert value["last_session"]["snapshot_complete"] is True
    assert value["last_session"]["snapshot_taken_at"] == "2026-10-30T19:59:00Z"
    assert value["last_decision"]["intents"] == 1
    assert value["recent_sessions"][0]["orders"] == 1
    assert [p["symbol"] for p in value["positions"]] == ["SCHG", "UPRO"]
    assert value["positions"][0]["market_value_usd"] == 110
    for reason in [value["last_decision"]["reason"], value["recent_sessions"][0]["reason"]]:
        assert len(reason) == 300 and "SECRET" not in reason
        assert "***REDACTED***" in reason
    text = export_json(str(home))
    assert MARKER not in text and "credential" not in text and "position_id" not in text
    assert "sleeve_realized_pnl_usd" not in text


def test_no_session_and_non_demo_are_empty(repo, tmp_path):
    assert dashboard_snapshot(repo) == {}
    repo.create_session("2026-10-01", "live", "real", True)
    assert dashboard_snapshot(repo) == {}
    home = tmp_path / "nightwatch_demo"
    other = seed_demo(home, environment="real")
    try:
        assert export_json(str(home)) == "{}"
    finally:
        other.close()
    assert export_json(str(tmp_path / "nightwatch_config")) == "{}"


def test_new_session_never_borrows_previous_snapshot(demo):
    _, repo = demo
    latest = repo.create_session("2026-10-31", "dry_run", "demo", False)
    value = dashboard_snapshot(repo)
    assert value["last_session"]["trading_date"] == latest.trading_date
    assert value["last_session"]["snapshot_taken_at"] is None
    assert value["last_session"]["snapshot_complete"] is None
    assert {"account", "sleeve", "positions"}.isdisjoint(value)


def test_highest_matching_snapshot_not_latest_global(demo):
    _, repo = demo
    session = repo.latest_session()
    repo.insert_snapshot(PortfolioSnapshot(session.id, "", cash=None, raw={}))
    repo.insert_snapshot(PortfolioSnapshot(None, "2026-12-31", cash=999999))
    value = dashboard_snapshot(repo)
    assert value["account"]["total_cash_usd"] is None
    assert value["last_session"]["snapshot_taken_at"] is None
    assert value["last_session"]["snapshot_complete"] is None


def test_incomplete_positions_remain_unknown(tmp_path):
    home = tmp_path / "nightwatch_demo"
    repo = seed_demo(home, complete=False)
    try:
        value = dashboard_snapshot(repo)
        assert value["last_session"]["snapshot_complete"] is False
        assert value["sleeve"]["sleeve_unrealized_pnl_usd"] is None
        assert all(p["market_value_usd"] is None for p in value["positions"])
        assert all(p["unrealized_pnl_usd"] is None for p in value["positions"])
    finally:
        repo.close()


def test_history_position_caps_and_utf8_size(tmp_path):
    home = tmp_path / "nightwatch_demo"
    repo = seed_demo(home, count=231, sessions=31)
    try:
        value = dashboard_snapshot(repo, history=999)
        assert len(value["recent_sessions"]) == 30
        assert value["recent_sessions"][0]["trading_date"] == "2026-10-31"
        assert value["recent_sessions"][-1]["trading_date"] == "2026-10-02"
        assert len(value["positions"]) == 230
        assert value["last_session"]["omitted_managed_positions"] == 1
        assert len(dashboard_snapshot(repo, history=0)["recent_sessions"]) == 0
        rich = json.dumps(value, ensure_ascii=False).encode("utf-8")
        merged = json.dumps({"probe": "nightwatch-android-probe-v2", "device": {}, "halt": False,
                             "recent_events": [], "latest_checks": [], **value}, ensure_ascii=False).encode("utf-8")
        print(f"230 positions / 30 sessions: rich={len(rich)} UTF-8 bytes, merged={len(merged)} bytes")
        assert len(merged) < 262144
    finally:
        repo.close()


def test_seed_status_only_precedence_and_corruption(demo):
    home, _ = demo
    (home / "SEED_DEMO.done").write_text(json.dumps({"status": "seeded", "token": MARKER}))
    assert json.loads(export_json(str(home)))["seed"] == {"status": "seeded"}
    adopt = home / "SEED_DEMO.adopt.done"
    adopt.write_text(json.dumps({"status": "adopted", "details": MARKER}))
    assert json.loads(export_json(str(home)))["seed"] == {"status": "adopted"}
    for content in ["broken", "[]", '{"status":"INVALID"}', '{"status":"' + "a" * 41 + '"}']:
        adopt.write_text(content)
        assert "seed" not in json.loads(export_json(str(home)))


def test_missing_and_outdated_database_no_creation_or_mutation(tmp_path):
    home = tmp_path / "nightwatch_demo"
    assert export_json(str(home)) == "{}"
    assert not home.exists()
    path = home / "data" / "nightwatch.db"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sessions (id INTEGER)")
    conn.commit()
    conn.close()
    before = (path.read_bytes(), path.stat().st_mtime_ns, sorted(path.parent.iterdir()))
    assert export_json(str(home)) == "{}"
    assert (path.read_bytes(), path.stat().st_mtime_ns, sorted(path.parent.iterdir())) == before


def test_export_reads_one_transaction_and_does_not_write(demo):
    home, repo = demo
    path = home / "data" / "nightwatch.db"
    before = (path.read_bytes(), path.stat().st_mtime_ns, sorted(path.parent.iterdir()))
    statements = []
    repo.conn.set_trace_callback(statements.append)
    value = dashboard_snapshot(repo)
    assert value["positions"]
    assert statements[0] == "BEGIN" and statements[-1] == "COMMIT"
    assert not any(s.startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER")) for s in statements)
    assert json.loads(export_json(str(home)))["account"]["total_equity_usd"] == 15000
    assert (path.read_bytes(), path.stat().st_mtime_ns, sorted(path.parent.iterdir())) == before


def test_closed_database_export_preserves_main_db_allows_sqlite_sidecars(tmp_path):
    home = tmp_path / "nightwatch_demo"
    repo = seed_demo(home)
    repo.close()
    path = home / "data" / "nightwatch.db"
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    files_before = {p.relative_to(home) for p in home.rglob("*")}
    assert json.loads(export_json(str(home)))["account"]["total_equity_usd"] == 15000
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    files_after = {p.relative_to(home) for p in home.rglob("*")}
    assert files_before <= files_after
    assert files_after - files_before <= {
        Path("data/nightwatch.db-wal"), Path("data/nightwatch.db-shm"),
    }


def test_export_reads_uncheckpointed_commit_without_changing_main_db(tmp_path, monkeypatch):
    home = tmp_path / "nightwatch_demo"
    repo = seed_demo(home)
    path = home / "data" / "nightwatch.db"
    try:
        assert repo.conn.execute("PRAGMA wal_autocheckpoint=0").fetchone()[0] == 0
        assert tuple(repo.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()) == (0, 0, 0)
        assert json.loads(export_json(str(home)))["account"]["total_equity_usd"] == 15000
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        files_before = {p.relative_to(home) for p in home.rglob("*")}
        # Fixture writer stays open so this committed change remains in WAL.
        repo.conn.execute("UPDATE portfolio_snapshots SET equity = ?", (15432.5,))
        repo.conn.commit()
        assert not repo.conn.in_transaction
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before
        wal = path.with_name("nightwatch.db-wal")
        assert wal.stat().st_size > 32
        statements = []
        connect = sqlite3.connect

        def read_only_connection(database, *args, **kwargs):
            assert database.endswith("?mode=ro") and kwargs["uri"] is True
            conn = connect(database, *args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        monkeypatch.setattr(sqlite3, "connect", read_only_connection)
        value = json.loads(export_json(str(home)))
        assert value["account"]["total_equity_usd"] == 15432.5
        assert statements[0] == "BEGIN" and statements[-1] == "COMMIT"
        assert all(s.startswith(("SELECT", "BEGIN", "COMMIT")) for s in statements)
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before
        assert wal.stat().st_size > 32
        assert {p.relative_to(home) for p in home.rglob("*")} == files_before
    finally:
        repo.close()


def test_consistent_read_when_writer_adds_a_session(demo):
    _, repo = demo
    writer = sqlite3.connect(repo.conn.execute("PRAGMA database_list").fetchone()[2])
    fired = False

    def concurrent_write(sql):
        nonlocal fired
        if not fired and "FROM decisions" in sql:
            fired = True
            writer.execute("INSERT INTO sessions (trading_date, state, mode, environment, armed, started_at, created_at) "
                           "VALUES ('2026-10-31', 'started', 'dry_run', 'demo', 0, '', '')")
            writer.commit()

    repo.conn.set_trace_callback(concurrent_write)
    try:
        value = dashboard_snapshot(repo)
        assert fired
        assert value["last_session"]["trading_date"] == "2026-10-01"
        assert len(value["recent_sessions"]) == 1
    finally:
        repo.conn.set_trace_callback(None)
        writer.close()
    assert dashboard_snapshot(repo)["last_session"]["trading_date"] == "2026-10-31"


def test_post_session_push_wiring():
    root = Path(__file__).resolve().parents[1]
    source = (root / "android-probe/app/src/main/java/com/nightwatch/etoro/probe/WakeService.kt").read_text()
    work = source.split("// 5b.")[1].split('store.log("WAKE_END"')[0]
    assert source.count("SnapshotPusher.pushNow(this, checkResults)") == 2
    assert work.index('trigger == "alarm"') < work.index("runSessionDry()")
    assert work.index("runSessionDry()") < work.index("sessionRunning.set(false)") < work.index("SnapshotPusher.pushNow")
    assert "catch (e: Exception)" in work.split("SnapshotPusher.pushNow")[1]
    assert work.rstrip().endswith("}\n            }")
