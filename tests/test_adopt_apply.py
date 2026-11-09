"""T068 device entry tests: stub clients, real temporary sidecar, no network."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from nightwatch.config import EtoroConfig, EtoroEnvironment, SymbolConfig
from nightwatch.db.repo import Instrument, Repo, default_db_path
from nightwatch.etoro.models import Position
from nightwatch.host import DesktopHost, get_host
from nightwatch.market.source import EodBar

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)


def position(pid="SCHG-1", cid=1234, instrument_id=1084, open_date="2024-07-06T12:00:00Z"):
    return Position(pid, instrument_id, 2.0, 25.0, 50.0, 1, True,
                    open_date=open_date, raw={"CID": cid})


@pytest.fixture
def device(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "android-probe/app/src/main/python"))
    module = importlib.import_module("probe.adopt_apply")
    state = SimpleNamespace(
        positions=[position(), position("UPRO-1", instrument_id=3244)],
        bars=[EodBar("2024-07-03", 100.0, 80.0, 1.0, 0.0),
              EodBar("2024-07-05", 85.0, 70.0, 1.0, 0.0)],
        environment=EtoroEnvironment.DEMO, clients=0, closed=0, calls=[], now=NOW,
    )

    def load(*, host):
        assert get_host() is host
        assert host.root == tmp_path
        return SimpleNamespace(etoro=EtoroConfig(state.environment),
                               symbols=SymbolConfig(), tiingo=object())

    class Client:
        def __init__(self, config, *, role, repo):
            assert role == "read"
            assert get_host().root == tmp_path
            assert repo is None  # Broker resolution must not mutate the sidecar cache.
            state.clients += 1

        def get_portfolio_strict(self):
            state.calls.append("portfolio")
            return state.positions

        def resolve_instrument_id(self, symbol):
            return {"SCHG": 1084, "UPRO": 3244}[symbol]

        def get_instruments(self, ids):
            assert ids == [999]
            return [{"instrumentID": 999, "symbolFull": "AAPL"}]

        def close(self):
            state.closed += 1

    class Tiingo:
        def __init__(self, config):
            pass

        def get_eod_history(self, symbol, *, start_date):
            assert symbol == "SPY"
            assert start_date == "2010-01-01"
            state.calls.append("history")
            return state.bars

        def close(self):
            state.closed += 1

    monkeypatch.setattr(module, "load_config", load)
    monkeypatch.setattr(module, "EtoroClient", Client)
    monkeypatch.setattr(module, "TiingoClient", Tiingo)
    monkeypatch.setattr(module, "_now", lambda: state.now)
    state.module = module
    state.home = str(tmp_path)
    state.path = tmp_path / module.PLAN_FILE
    state.plan = lambda: json.loads(module.plan(state.home))
    state.apply = lambda pid: json.loads(module.apply(state.home, pid))
    state.read = lambda: json.loads(state.path.read_text(encoding="utf-8"))
    state.write = lambda saved: state.path.write_text(json.dumps(saved), encoding="utf-8")
    return state


def annotations(device):
    host = DesktopHost(Path(device.home))
    with Repo.open(default_db_path(host), host=host) as repo:
        return repo.list_lot_annotations()


def database_dump(home):
    path = Path(home) / "data/nightwatch.db"
    if not path.exists():
        return None
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        return list(db.iterdump())


def test_plan_apply_and_explicit_idempotent_rerun(device, capsys):
    previous = get_host()
    planned = device.plan()
    assert planned["status"] == "planned"
    assert "to adopt          : 2" in capsys.readouterr().out
    assert annotations(device) == []
    saved = device.read()
    assert saved["account_marker"] == "1234"
    assert saved["environment"] == "demo"
    assert saved["created_at"] == NOW.isoformat()
    assert saved["home_path"] == str(Path(device.home).resolve())
    assert saved["db_path"] == str((Path(device.home) / "data/nightwatch.db").resolve())
    host = DesktopHost(Path(device.home))
    with Repo.open(default_db_path(host), host=host) as repo:
        assert repo.list_instruments() == []  # Planning stages even managed IDs without writes.
    assert "raw" not in saved
    assert "SCHG-1" in saved["report"]
    assert "2024-07-06 -> preceding NYSE trading date 2024-07-05" in saved["report"]
    assert len(saved["positions_fingerprint"]) == 64
    assert get_host() is previous
    applied = device.apply(planned["plan_id"])
    assert applied["status"] == "applied"
    rows = annotations(device)
    assert len(rows) == 2
    assert all(row.entry_spy_close == 85.0 for row in rows)
    assert all(row.entry_date == "2024-07-06T12:00:00Z" for row in rows)
    assert all(row.entry_reference_spy == 85.0 for row in rows)
    assert all(row.book == "catapult" and row.origin == "adopted" for row in rows)
    # A consumed plan cannot silently approve changed sidecar state.
    assert device.apply(planned["plan_id"])["status"] == "refused"
    again = device.plan()
    assert "already annotated : 2" in again["report"]
    assert device.apply(again["plan_id"])["status"] == "applied"
    assert annotations(device) == rows
    assert get_host() is previous
    assert device.closed == device.clients * 2


@pytest.mark.parametrize("change,reason", [
    ("account", "account or environment mismatch"),
    ("environment", "account or environment mismatch"),
    ("position", "portfolio changed"),
    ("new_position", "portfolio changed"),
    ("history", "adoption inputs or annotations changed"),
    ("annotations", "adoption inputs or annotations changed"),
])
def test_changed_inputs_refuse_without_adopting(device, change, reason):
    planned = device.plan()
    if change == "account":
        device.positions = [replace(p, raw={"CID": 5678}) for p in device.positions]
    elif change == "environment":
        device.environment = EtoroEnvironment.REAL
    elif change == "position":
        device.positions[0] = replace(device.positions[0], amount=60.0)
    elif change == "new_position":
        device.positions.append(position("SCHG-2"))
    elif change == "history":
        device.bars[1] = replace(device.bars[1], close=84.0)
    else:
        saved = device.read()
        from nightwatch.db.repo import LotAnnotation
        host = DesktopHost(Path(device.home))
        with Repo.open(default_db_path(host), host=host) as repo:
            repo.insert_lot_annotations([LotAnnotation(**saved["adoption"]["to_adopt"][0])])
    before = database_dump(device.home)
    result = device.apply(planned["plan_id"])
    assert result["status"] == "refused"
    assert reason in result["report"]
    assert database_dump(device.home) == before


def test_portfolio_order_is_not_a_state_change(device):
    planned = device.plan()
    device.positions.reverse()
    assert device.apply(planned["plan_id"])["status"] == "applied"
    assert len(annotations(device)) == 2


def test_edit_to_an_already_annotated_lot_invalidates_a_noop_plan(device):
    planned = device.plan()
    assert device.apply(planned["plan_id"])["status"] == "applied"
    noop = device.plan()
    host = DesktopHost(Path(device.home))
    with Repo.open(default_db_path(host), host=host) as repo:
        row = repo.get_lot_annotation("SCHG-1")
        repo.update_lot_annotation(replace(row, entry_tier=5))
    before = database_dump(device.home)
    result = device.apply(noop["plan_id"])
    assert result["status"] == "refused"
    assert "annotations changed" in result["report"]
    assert database_dump(device.home) == before


@pytest.mark.parametrize("age", [1801, -1])
def test_stale_or_future_plan_refuses_before_network(device, age):
    planned = device.plan()
    device.now = NOW + timedelta(seconds=age)
    calls = list(device.calls)
    before = database_dump(device.home)
    result = device.apply(planned["plan_id"])
    assert result["status"] == "refused"
    assert "stale plan" in result["report"]
    assert device.calls == calls
    assert database_dump(device.home) == before
    assert annotations(device) == []


def test_exactly_thirty_minutes_is_fresh(device):
    planned = device.plan()
    device.now = NOW + timedelta(minutes=30)
    assert device.apply(planned["plan_id"])["status"] == "applied"


def test_plan_expiring_during_fetch_refuses(device, monkeypatch):
    planned = device.plan()
    original = device.module.TiingoClient.get_eod_history

    def delayed(self, *args, **kwargs):
        device.now = NOW + timedelta(minutes=31)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(device.module.TiingoClient, "get_eod_history", delayed)
    before = database_dump(device.home)
    assert device.apply(planned["plan_id"])["status"] == "refused"
    assert database_dump(device.home) == before
    assert annotations(device) == []


@pytest.mark.parametrize("kind", ["missing", "corrupt", "wrong_id", "empty_id", "bad_timestamp"])
def test_invalid_saved_plan_refuses_before_network(device, kind):
    pid = "not-a-plan"
    if kind not in ("missing", "empty_id"):
        pid = device.plan()["plan_id"]
        if kind == "corrupt":
            device.path.write_text("not json", encoding="utf-8")
        elif kind == "wrong_id":
            pid = "different-id"
        else:
            saved = device.read()
            saved["created_at"] = "not-a-timestamp"
            device.write(saved)
    calls = list(device.calls)
    before = database_dump(device.home)
    result = device.apply("" if kind == "empty_id" else pid)
    assert result["status"] == "refused"
    assert device.calls == calls
    assert database_dump(device.home) == before
    assert annotations(device) == []


@pytest.mark.parametrize("kind,reason", [
    ("empty", "nothing to adopt"), ("mixed", "disagree"), ("missing", "missing its CID"),
    ("bool", "missing its CID"),
])
def test_account_marker_requires_unanimous_nonempty_portfolio(device, kind, reason):
    if kind == "empty":
        device.positions = []
    elif kind == "mixed":
        device.positions[1] = replace(device.positions[1], raw={"CID": 5678})
    else:
        device.positions[0] = replace(device.positions[0], raw={} if kind == "missing" else {"CID": True})
    result = device.plan()
    assert result["status"] == "refused"
    assert reason in result["report"]
    assert not device.path.exists()
    assert annotations(device) == []


def test_unmanaged_instrument_is_resolved_and_excluded(device):
    device.positions.append(position("AAPL-1", instrument_id=999))
    result = device.plan()
    assert result["status"] == "planned"
    assert "excluded (unmanaged instrument): 1" in result["report"]
    assert device.apply(result["plan_id"])["status"] == "applied"
    assert len(annotations(device)) == 2


def test_metadata_cannot_overwrite_a_managed_instrument(device, monkeypatch):
    host = DesktopHost(Path(device.home))
    with Repo.open(default_db_path(host), host=host) as repo:
        repo.upsert_instrument(Instrument(symbol="SCHG", instrument_id=1084))
    before = database_dump(device.home)
    device.positions.append(position("UNKNOWN-1", instrument_id=999))
    monkeypatch.setattr(device.module.EtoroClient, "get_instruments", lambda self, ids: [
        {"instrumentID": 999, "symbolFull": "SCHG"},
    ])
    result = device.plan()
    assert result["status"] == "refused"
    assert "metadata conflicts" in result["report"]
    assert database_dump(device.home) == before
    host = DesktopHost(Path(device.home))
    with Repo.open(default_db_path(host), host=host) as repo:
        assert repo.get_instrument("SCHG").instrument_id == 1084
        assert repo.list_lot_annotations() == []


def test_history_gap_refuses_entire_plan(device):
    device.bars = device.bars[:1]  # Friday's exact bar is missing; Wednesday is not a fallback.
    result = device.plan()
    assert result["status"] == "refused"
    assert "SPY series does not cover '2024-07-05'" in result["report"]
    assert not device.path.exists()
    assert annotations(device) == []


def test_demo_home_refuses_real_config_before_client(device, monkeypatch, tmp_path):
    module = device.module
    monkeypatch.setattr(module, "load_config", lambda **kw: SimpleNamespace(
        etoro=EtoroConfig(EtoroEnvironment.REAL)))
    result = json.loads(module.plan(str(tmp_path / "nightwatch_demo")))
    assert result["status"] == "refused"
    assert "demo home requires demo" in result["report"]
    assert device.clients == 0


def test_foreign_errors_are_translated_and_host_restored(device, monkeypatch, capsys):
    previous = get_host()

    def fail(self):
        raise RuntimeError("QA_SYNTHETIC_PRIVATE_BODY")

    monkeypatch.setattr(device.module.EtoroClient, "get_portfolio_strict", fail)
    result = device.plan()
    assert result["status"] == "error"
    assert "portfolio read (RuntimeError)" in result["report"]
    assert "QA_SYNTHETIC_PRIVATE_BODY" not in capsys.readouterr().out
    assert get_host() is previous
    assert device.closed == 1


def test_host_restored_on_interrupt(device, monkeypatch):
    previous = get_host()

    def interrupt(**kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(device.module, "load_config", interrupt)
    with pytest.raises(KeyboardInterrupt):
        device.plan()
    assert get_host() is previous


def test_same_home_session_lock_refuses(device):
    planned = device.plan()
    before = database_dump(device.home)
    clients = device.clients
    data = DesktopHost(Path(device.home)).data_dir()
    with device.module._session_lock(data / "session.lock") as acquired:
        assert acquired
        result = device.apply(planned["plan_id"])
        assert result["status"] == "refused"
        assert "already running" in result["report"]
    assert device.clients == clients
    assert database_dump(device.home) == before


@pytest.mark.parametrize("initialized", [False, True], ids=["missing-db", "existing-db"])
def test_copied_plan_cannot_authorize_another_home(device, monkeypatch, tmp_path, initialized):
    planned = device.plan()
    other = tmp_path / "other_home"
    other.mkdir()
    if initialized:
        host = DesktopHost(other)
        with Repo.open(default_db_path(host), host=host) as repo:
            repo.upsert_instrument(Instrument(symbol="AAPL", instrument_id=999))
    (other / device.module.PLAN_FILE).write_text(device.path.read_text(encoding="utf-8"), encoding="utf-8")
    before = database_dump(other)

    def must_not_open(path):
        pytest.fail("home binding must be checked before opening a DB")

    monkeypatch.setattr(device.module, "_open_for_apply", must_not_open)
    result = json.loads(device.module.apply(str(other), planned["plan_id"]))
    assert result["status"] == "refused"
    assert "home or database path mismatch" in result["report"]
    assert database_dump(other) == before


def test_database_path_binding_is_checked_before_open(device, monkeypatch, tmp_path):
    planned = device.plan()
    saved = device.read()
    saved["db_path"] = str((tmp_path / "another.db").resolve())
    device.write(saved)
    before = database_dump(device.home)

    def must_not_open(path):
        pytest.fail("DB binding must be checked before opening a DB")

    monkeypatch.setattr(device.module, "_open_for_apply", must_not_open)
    result = device.apply(planned["plan_id"])
    assert result["status"] == "refused"
    assert "home or database path mismatch" in result["report"]
    assert database_dump(device.home) == before


@pytest.mark.parametrize("reason", ["unresolved_history", "stale_portfolio", "account", "environment"])
def test_refused_apply_leaves_every_database_table_unchanged(device, reason):
    device.positions.append(position("AAPL-1", instrument_id=999))
    planned = device.plan()
    # All three instrument rows are pending; none may escape a failed apply.
    if reason == "unresolved_history":
        device.bars = device.bars[:1]
    elif reason == "stale_portfolio":
        device.positions[0] = replace(device.positions[0], amount=60.0)
    elif reason == "account":
        device.positions = [replace(p, raw={"CID": 5678}) for p in device.positions]
    else:
        device.environment = EtoroEnvironment.REAL
    before = database_dump(device.home)
    result = device.apply(planned["plan_id"])
    assert result["status"] == "refused"
    assert database_dump(device.home) == before


@pytest.mark.parametrize("kind", ["missing", "old", "unversioned"])
def test_apply_requires_an_existing_current_schema_without_migrating(device, kind):
    planned = device.plan()
    path = Path(device.home) / "data/nightwatch.db"
    if kind == "missing":
        path.rename(path.with_suffix(".saved"))
    else:
        with sqlite3.connect(path) as db:
            if kind == "old":
                db.execute("DELETE FROM schema_version WHERE version = (SELECT MAX(version) FROM schema_version)")
            else:
                db.execute("ALTER TABLE schema_version RENAME TO original_schema_version")
    before = database_dump(device.home)
    clients = device.clients
    result = device.apply(planned["plan_id"])
    assert result["status"] == "refused"
    assert ("database is missing" if kind == "missing" else "not at the current schema") in result["report"]
    assert device.clients == clients
    assert database_dump(device.home) == before


def test_instrument_and_annotation_inserts_share_one_commit(device, monkeypatch):
    device.positions.append(position("AAPL-1", instrument_id=999))
    planned = device.plan()
    statements = []
    real_open = device.module._open_for_apply

    def traced_open(path):
        repo = real_open(path)
        repo.conn.set_trace_callback(statements.append)
        return repo

    monkeypatch.setattr(device.module, "_open_for_apply", traced_open)
    assert device.apply(planned["plan_id"])["status"] == "applied"
    assert statements.count("BEGIN IMMEDIATE") == 1
    assert statements.count("COMMIT") == 1
    assert "ROLLBACK" not in statements
    assert len([s for s in statements if s.startswith("INSERT INTO instruments")]) == 3
    assert len([s for s in statements if "INSERT OR IGNORE INTO lot_annotations" in s]) == 2
    host = DesktopHost(Path(device.home))
    with Repo.open(default_db_path(host), host=host) as repo:
        assert len(repo.list_instruments()) == 3
        assert len(repo.list_lot_annotations()) == 2


def test_annotation_failure_rolls_back_instruments_and_the_first_annotation(device, monkeypatch):
    device.positions.append(position("AAPL-1", instrument_id=999))
    planned = device.plan()
    path = Path(device.home) / "data/nightwatch.db"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TRIGGER fail_second_annotation BEFORE INSERT ON lot_annotations
                      WHEN NEW.position_id = 'UPRO-1'
                      BEGIN SELECT RAISE(ABORT, 'synthetic annotation failure'); END""")
    before = database_dump(device.home)
    statements = []
    real_open = device.module._open_for_apply

    def traced_open(path):
        repo = real_open(path)
        repo.conn.set_trace_callback(statements.append)
        return repo

    monkeypatch.setattr(device.module, "_open_for_apply", traced_open)
    result = device.apply(planned["plan_id"])
    assert result["status"] == "error"
    assert "annotation write (IntegrityError)" in result["report"]
    assert any(s.startswith("INSERT INTO instruments") for s in statements)
    assert any("INSERT OR IGNORE INTO lot_annotations" in s for s in statements)
    assert statements.count("BEGIN IMMEDIATE") == 1
    assert "COMMIT" not in statements
    assert "ROLLBACK" in statements
    assert database_dump(device.home) == before


def test_live_snapshot_is_rechecked_under_the_write_lock(device, monkeypatch):
    planned = device.plan()
    history = device.module.TiingoClient.get_eod_history
    fingerprint = device.module._database_fingerprint
    captured = []
    transaction_states = []

    def changed_during_fetch(self, *args, **kwargs):
        bars = history(self, *args, **kwargs)
        with sqlite3.connect(Path(device.home) / "data/nightwatch.db") as db:
            db.execute("INSERT INTO instruments (symbol, instrument_id, updated_at) VALUES ('AAPL',999,'test')")
        captured.append(database_dump(device.home))
        return bars

    def checked(repo):
        transaction_states.append(repo.conn.in_transaction)
        return fingerprint(repo)

    monkeypatch.setattr(device.module.TiingoClient, "get_eod_history", changed_during_fetch)
    monkeypatch.setattr(device.module, "_database_fingerprint", checked)
    result = device.apply(planned["plan_id"])
    assert result["status"] == "refused"
    assert "database changed during validation" in result["report"]
    assert transaction_states == [False, True]
    # Preserve the external change, but no adoption write may join it.
    assert database_dump(device.home) == captured[0]


def test_duplicate_position_ids_are_refused_before_any_apply_write(device):
    planned = device.plan()
    device.positions.append(device.positions[0])
    before = database_dump(device.home)
    result = device.apply(planned["plan_id"])
    assert result["status"] == "refused"
    assert "duplicate position IDs" in result["report"]
    assert database_dump(device.home) == before


def test_gate11_before_after_and_negative_control_with_resolved_managed_ids(device):
    from nightwatch.engine.preflight import gate_11_reconciliation

    host = DesktopHost(Path(device.home))
    # A session resolves managed IDs before gate 11; planning no longer writes that cache.
    with Repo.open(default_db_path(host), host=host) as repo:
        for symbol, iid in (("SCHG", 1084), ("UPRO", 3244)):
            repo.upsert_instrument(Instrument(symbol=symbol, instrument_id=iid))

    def gate(positions):
        with Repo.open(default_db_path(host), host=host) as repo:
            return gate_11_reconciliation(
                positions, repo.list_lot_annotations(), managed_symbols=SymbolConfig().traded,
                instrument_symbols={i.instrument_id: i.symbol for i in repo.list_instruments()},
                repo=repo,
            )

    planned = device.plan()
    assert not gate(device.positions).passed
    assert device.apply(planned["plan_id"])["status"] == "applied"
    assert gate(device.positions).passed
    assert not gate(device.positions[:-1]).passed
