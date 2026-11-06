"""Offline Android entry contract, including real-engine and kernel-lock proofs."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from nightwatch import android_entry as entry
from nightwatch.config import ConfigError
from nightwatch.db.repo import PreflightCheck, Repo, default_db_path
from nightwatch.engine import session
from nightwatch.engine.preflight import GateResult, GateSpec
from nightwatch.host import DesktopHost, get_host, set_host
from .test_session_market import FakeTiingo, MarketClient, assembled_signal


NOW = datetime(2026, 10, 2, 19, 30, tzinfo=timezone.utc)
MARKER = "SECRET_MARKER_123"
KEYS = {"status", "armed", "session_id", "failed_gate", "reason_code", "error_type", "gates", "halt"}


@pytest.fixture(autouse=True, params=[False, True], ids=["halt-absent", "halt-present"])
def halt_file(host, request):
    if request.param:
        (host.root / "HALT").write_text("test halt")
    return request.param


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    previous = get_host()

    def no_network(*args, **kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(httpx.Client, "send", no_network)
    monkeypatch.setattr(httpx, "get", no_network)
    yield
    set_host(previous)


@pytest.fixture
def wired(config_path, host, monkeypatch):
    clients = []
    closed = []

    def client(config, *, role, repo):
        assert role == "read"
        clients.append(role)
        return SimpleNamespace(close=lambda: closed.append("client"))

    monkeypatch.setattr(entry, "EtoroClient", client)
    monkeypatch.setattr(entry, "TelegramNotifier", lambda *a, **k: SimpleNamespace(close=lambda: closed.append("notifier")))
    monkeypatch.setattr(entry, "TiingoClient", lambda *a: SimpleNamespace(close=lambda: closed.append("tiingo")))

    def run(**kwargs):
        assert not kwargs["config"].is_armed and kwargs["wait"] is True
        assert kwargs["trading_date"] == NOW.date()
        repo = kwargs["repo"]
        row = repo.create_session(NOW.date().isoformat(), "dry_run", "demo", False)
        for gate in range(1, 13):
            repo.record_preflight_check(PreflightCheck(row.id, gate, f"gate_{gate}", True, "{}"))
        repo.finish_session(row.id, "finished", "would have done 0 intent(s)")
        return session.SessionResult(row.id, "would_have_done", None, MARKER)

    monkeypatch.setattr(entry, "run_session", run)
    return host, clients, closed


def invoke(host, **kwargs):
    result = json.loads(entry.run_session_dry(str(host.root), now_utc=kwargs.pop("now_utc", NOW), **kwargs))
    assert set(result) == KEYS and result["armed"] is False
    assert result["halt"] is (host.root / "HALT").exists()
    assert MARKER not in json.dumps(result)
    return result


def test_happy_shape_and_persisted_gates(wired):
    host, clients, closed = wired
    result = invoke(host)
    assert result == dict(status="would_have_done", armed=False, session_id=1, failed_gate=None,
                          reason_code="would_have_done", error_type=None, halt=(host.root / "HALT").exists(),
                          gates=[[n, f"gate_{n}", True] for n in range(1, 13)])
    assert clients == ["read"] and set(closed) == {"client", "notifier", "tiingo"}
    assert (host.data_dir() / "session.lock").is_file()
    assert invoke(host)["status"] == "already_ran"
    assert clients == ["read"]


def test_live_env_is_disarmed(wired, config_path, monkeypatch):
    host, clients, _ = wired
    config_path.write_text(config_path.read_text().replace('mode = "dry_run"', 'mode = "live"'))
    monkeypatch.setenv("NIGHTWATCH_ARM", "1")
    assert invoke(host)["status"] == "would_have_done"
    import os
    assert "NIGHTWATCH_ARM" not in os.environ
    assert clients == ["read"]


def test_unexpected_armed_config_refused(wired, monkeypatch):
    host, clients, _ = wired
    monkeypatch.setattr(entry, "load_config", lambda **k: SimpleNamespace(is_armed=True))
    result = invoke(host)
    assert result["status"] == "refused" and result["reason_code"] == "armed_not_allowed"
    assert clients == [] and not (host.root / "data").exists()


@pytest.mark.parametrize("instant", [
    "2026-10-03T19:30:00+00:00", "2026-12-25T20:30:00+00:00",
    "2026-10-02T18:59:59+00:00", "2026-10-02T19:35:01+00:00",
    "2026-11-27T18:00:00+00:00",
])
def test_out_of_window_before_lock_or_db(wired, instant):
    host, clients, _ = wired
    assert invoke(host, now_utc=datetime.fromisoformat(instant))["status"] == "out_of_window"
    assert clients == [] and not (host.root / "data").exists()


@pytest.mark.parametrize("instant", [
    "2026-10-02T19:00:00+00:00", "2026-10-02T19:35:00+00:00",
    "2026-11-27T17:00:00+00:00", "2026-11-27T17:35:00+00:00",
    "2026-11-02T20:30:00+00:00",
])
def test_window_boundaries_early_close_and_dst(wired, monkeypatch, instant):
    host, clients, _ = wired
    monkeypatch.setattr(entry, "run_session", lambda **k: session.SessionResult(None, "would_have_done", None, MARKER))
    assert invoke(host, now_utc=datetime.fromisoformat(instant))["status"] == "would_have_done"
    assert clients == ["read"]


@pytest.mark.parametrize("value", ["2026-10-02", datetime(2026, 10, 2)])
def test_test_clock_requires_aware_datetime(tmp_path, value):
    with pytest.raises(ValueError, match="aware datetime"):
        entry.run_session_dry(str(tmp_path), now_utc=value)


def test_busy_skips_engine_then_close_releases(wired):
    host, clients, _ = wired
    path = host.data_dir() / "session.lock"
    with entry._session_lock(path) as acquired:
        assert acquired
        assert invoke(host)["status"] == "busy"
        assert clients == []
    assert invoke(host)["status"] == "would_have_done"


def test_concurrent_call_is_busy(wired, monkeypatch):
    host, clients, _ = wired
    entered, release = threading.Event(), threading.Event()
    original = entry.run_session

    def run(**kwargs):
        entered.set()
        assert release.wait(10)
        return original(**kwargs)

    monkeypatch.setattr(entry, "run_session", run)
    results = []
    worker = threading.Thread(target=lambda: results.append(invoke(host)))
    worker.start()
    try:
        assert entered.wait(10)
        assert invoke(host)["status"] == "busy"
        assert clients == ["read"]
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive() and results[0]["status"] == "would_have_done"


def test_killed_child_releases_kernel_lock(host):
    path = host.data_dir() / "session.lock"
    code = """import sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from nightwatch.android_entry import _session_lock
with _session_lock(Path(sys.argv[1])) as acquired:
    print('locked' if acquired else 'busy', flush=True)
    sys.stdin.read()
"""
    child = subprocess.Popen([sys.executable, "-c", code, str(path), str(Path(entry.__file__).parents[1])], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "locked"
        with entry._session_lock(path) as acquired:
            assert not acquired
        child.kill()
        child.wait(timeout=10)
        # Windows signals process exit slightly before it tears down the dead process's handles.
        deadline = time.monotonic() + 10
        while True:
            with entry._session_lock(path) as acquired:
                if acquired or time.monotonic() > deadline:
                    break
            time.sleep(0.05)
        assert acquired
        assert path.is_file()
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)


@pytest.mark.parametrize("state,outcome,error,expected", [
    ("preflight", None, None, "previous_unfinished"),
    ("aborted", "preflight failed", None, "previous_unfinished"),
    ("finished", "would have done 0 intent(s)", MARKER, "previous_unfinished"),
    ("finished", "would have done 0 intent(s)", None, "already_ran"),
])
def test_existing_today_never_repaired(wired, state, outcome, error, expected):
    host, clients, _ = wired
    with Repo.open(default_db_path(host), host=host) as repo:
        row = repo.create_session(NOW.date().isoformat(), "dry_run", "demo", False)
        repo.finish_session(row.id, state, outcome, error)
        before = repo.get_session(row.id)
    assert invoke(host)["status"] == expected
    assert clients == []
    with Repo.open(default_db_path(host), host=host) as repo:
        assert repo.get_session(row.id) == before


def test_malformed_config_is_sanitized(config_path, host):
    config_path.write_text(config_path.read_text().replace('mode = "dry_run"', f'mode = "{MARKER}"'))
    result = invoke(host)
    assert result["status"] == "error" and result["error_type"] == "ConfigError"
    assert not (host.root / "data").exists()


@pytest.mark.parametrize("stage", ["config", "session", "cleanup", "setup", "serialization"])
def test_exception_envelopes_and_release(wired, monkeypatch, stage):
    host, _, _ = wired

    def fail(*a, **k):
        raise ConfigError(MARKER)

    if stage == "config":
        monkeypatch.setattr(entry, "load_config", fail)
    elif stage == "session":
        monkeypatch.setattr(entry, "run_session", fail)
    elif stage == "cleanup":
        monkeypatch.setattr(entry, "TiingoClient", lambda *a: SimpleNamespace(close=fail))
    elif stage == "setup":
        monkeypatch.setattr(entry, "DesktopHost", fail)
    else:
        monkeypatch.setattr(entry.json, "dumps", fail)
    # json is shared with entry: restore dumps before inspecting the returned envelope.
    raw = entry.run_session_dry(str(host.root), now_utc=NOW)
    assert MARKER not in raw
    result = json.loads(raw)
    assert set(result) == KEYS and result["armed"] is False
    assert result["status"] == "error" and result["error_type"] == "ConfigError"
    assert result["halt"] is (host.root / "HALT").exists()
    if (host.root / "data").exists():
        with entry._session_lock(host.data_dir() / "session.lock") as acquired:
            assert acquired


@pytest.mark.parametrize("live", [False, True])
def test_real_engine_dry_run_never_writes(config_path, host, monkeypatch, live):
    attempts = []

    class ReadOnlyClient(MarketClient):
        def write(self, *a, **k):
            attempts.append("write")
            raise AssertionError("broker write")

        place_buy_order = close_position = clear_take_profit = write
        post = patch = delete = write

        def _request(self, method, *a, **k):
            if method.upper() in {"POST", "PATCH", "DELETE"}:
                return self.write()
            raise AssertionError("unexpected transport GET")

        def close(self):
            pass

    client = ReadOnlyClient()
    client.rates = {i: replace(r, timestamp=NOW.isoformat()) for i, r in client.rates.items()}
    client.candles = {i: [replace(c, timestamp=NOW.isoformat()) for c in cs] for i, cs in client.candles.items()}
    client.minute_candles = {i: [replace(c, timestamp=NOW.isoformat()) for c in cs] for i, cs in client.minute_candles.items()}

    def make_client(config, *, role, repo):
        assert role == "read"
        return client

    monkeypatch.setattr(entry, "EtoroClient", make_client)
    monkeypatch.setattr(entry, "TiingoClient", lambda c: FakeTiingo())
    monkeypatch.setattr(DesktopHost, "now", lambda self: NOW)
    # Real engine, decision, DB and price rerun; only offline preflight inputs/signal stubbed.
    monkeypatch.setattr(session, "build_preflight_gates", lambda **k: [
        GateSpec(n, f"gate_{n}", lambda n=n: GateResult(n, f"gate_{n}", True, "ok"))
        for n in range(1, 13)
    ])
    monkeypatch.setattr(session, "assemble_spy_signal", lambda *a, **k: assembled_signal())
    if live:
        config_path.write_text(config_path.read_text().replace('mode = "dry_run"', 'mode = "live"'))
        monkeypatch.setenv("NIGHTWATCH_ARM", "1")
    assert entry.run_session is session.run_session
    result = invoke(host)
    assert result["status"] == "would_have_done" and len(result["gates"]) == 12
    assert attempts == []


def test_real_gate_failure_is_sanitized(config_path, host, monkeypatch):
    monkeypatch.setattr(entry, "EtoroClient", lambda *a, **k: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(entry, "TiingoClient", lambda c: FakeTiingo())
    monkeypatch.setattr(session, "build_preflight_gates", lambda **k: [
        GateSpec(n, "halt" if n == 1 else f"gate_{n}",
                 lambda n=n: GateResult(n, "halt" if n == 1 else f"gate_{n}", n != 1, MARKER))
        for n in range(1, 13)
    ])
    result = invoke(host)
    assert result["status"] == "preflight_failed" and result["failed_gate"] == 1
    assert result["gates"] == [[1, "halt", False]]


def test_prior_date_unfinished_reaches_real_gate_five(config_path, host, monkeypatch):
    with Repo.open(default_db_path(host), host=host) as repo:
        previous = repo.create_session("2026-10-01", "dry_run", "demo", False)
    monkeypatch.setattr(entry, "EtoroClient", lambda *a, **k: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(entry, "TiingoClient", lambda c: FakeTiingo())
    from nightwatch.engine.preflight import gate_5_database

    def gates(**kwargs):
        return [GateSpec(n, f"gate_{n}",
                         lambda n=n: gate_5_database(repo=kwargs["repo"], session_id=kwargs["session_id"])
                         if n == 5 else GateResult(n, f"gate_{n}", True, "ok"))
                for n in range(1, 13)]

    monkeypatch.setattr(session, "build_preflight_gates", gates)
    result = invoke(host)
    assert result["status"] == "preflight_failed" and result["failed_gate"] == 5
    with Repo.open(default_db_path(host), host=host) as repo:
        assert repo.get_session(previous.id) == previous


@pytest.mark.parametrize("instant,expected", [
    ("2026-10-01T18:00:00+00:00", "2026-10-01T19:05:00+00:00"),
    ("2026-10-01T19:05:00+00:00", "2026-10-02T19:05:00+00:00"),
    ("2026-10-01T19:05:01+00:00", "2026-10-02T19:05:00+00:00"),
    ("2026-10-02T20:00:00+00:00", "2026-10-05T19:05:00+00:00"),
    ("2026-11-25T21:00:00+00:00", "2026-11-27T17:05:00+00:00"),
    ("2026-11-27T16:00:00+00:00", "2026-11-27T17:05:00+00:00"),
    ("2026-10-30T18:00:00+00:00", "2026-10-30T19:05:00+00:00"),
    ("2026-10-30T20:00:00+00:00", "2026-11-02T20:05:00+00:00"),
    ("2026-03-06T19:00:00+00:00", "2026-03-06T20:05:00+00:00"),
    ("2026-03-06T21:00:00+00:00", "2026-03-09T19:05:00+00:00"),
])
def test_next_market_wake_calendar(instant, expected):
    now = datetime.fromisoformat(instant)
    epoch = entry.next_session_wake_epoch_ms(int(now.timestamp() * 1000))
    assert type(epoch) is int
    wake = datetime.fromtimestamp(epoch / 1000, timezone.utc)
    assert wake == datetime.fromisoformat(expected)
    local = wake.astimezone(entry.EASTERN)
    assert entry.is_trading_day(local.date())
    close = entry.session_close_datetime(local.date())
    assert close - timedelta(minutes=60) <= local <= close - timedelta(minutes=25)


def test_next_market_wake_has_no_io(monkeypatch):
    import builtins
    import socket
    import sqlite3

    def forbidden(*a, **k):
        raise AssertionError("helper must not access host, DB, config, files or network")

    for name in ("DesktopHost", "set_host", "load_config", "default_db_path", "EtoroClient", "TiingoClient"):
        monkeypatch.setattr(entry, name, forbidden)
    monkeypatch.setattr(Repo, "open", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "exists", forbidden)
    now = datetime(2026, 10, 30, 18, tzinfo=timezone.utc)
    assert entry.next_session_wake_epoch_ms(int(now.timestamp() * 1000)) == 1793387100000


def test_next_market_wake_real_clock_and_errors(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 2, 19, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(entry, "datetime", Clock)
    assert entry.next_session_wake_epoch_ms() == int(datetime(2026, 11, 2, 20, 5, tzinfo=timezone.utc).timestamp() * 1000)

    def fail(*a, **k):
        raise RuntimeError("calendar failure")

    monkeypatch.setattr(entry, "next_close_minus", fail)
    with pytest.raises(RuntimeError, match="calendar failure"):
        entry.next_session_wake_epoch_ms()


@pytest.mark.parametrize("status", sorted(entry._STATUSES) + ["unknown"])
def test_engine_status_halt_envelopes(wired, monkeypatch, status):
    host, _, _ = wired
    monkeypatch.setattr(entry, "run_session", lambda **k: session.SessionResult(None, status, None, MARKER))
    assert invoke(host)["status"] == (status if status in entry._STATUSES else "error")


def test_real_halt_gate(config_path, host, halt_file, monkeypatch):
    from nightwatch.engine.preflight import gate_1_halt

    monkeypatch.setattr(entry, "EtoroClient", lambda *a, **k: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(entry, "TiingoClient", lambda c: FakeTiingo())
    monkeypatch.setattr(session, "build_preflight_gates", lambda **k: [
        GateSpec(n, "halt" if n == 1 else f"gate_{n}",
                 lambda n=n: gate_1_halt(host) if n == 1 else GateResult(n, f"gate_{n}", False, "stop offline"))
        for n in range(1, 13)
    ])
    result = invoke(host)
    assert result["status"] == "preflight_failed"
    assert result["failed_gate"] == (1 if halt_file else 2)
    assert result["halt"] is halt_file


def test_halt_created_during_engine_is_reported(wired, monkeypatch):
    host, _, _ = wired

    def run(**kwargs):
        (host.root / "HALT").write_text("engine halt")
        return session.SessionResult(None, "preflight_failed", 1, MARKER)

    monkeypatch.setattr(entry, "run_session", run)
    result = invoke(host)
    assert result["halt"] is True and result["failed_gate"] == 1


def test_halt_existence_error_falls_back_false(wired, monkeypatch):
    host, _, _ = wired
    original = Path.exists

    def exists(path):
        if path == host.root / "HALT":
            raise OSError(MARKER)
        return original(path)

    monkeypatch.setattr(Path, "exists", exists)
    result = json.loads(entry.run_session_dry(str(host.root), now_utc=NOW))
    assert result["halt"] is False and result["armed"] is False
    assert MARKER not in json.dumps(result)


@pytest.mark.parametrize("status", ["market_data_failed", "aborted_before_orders"])
def test_real_engine_preorder_abort_then_previous_unfinished(config_path, host, monkeypatch, status):
    from nightwatch.etoro.errors import EtoroNetworkError
    from nightwatch.notify import poll

    client = MarketClient()
    client.close = lambda: None
    monkeypatch.setattr(entry, "EtoroClient", lambda *a, **k: client)
    monkeypatch.setattr(entry, "TiingoClient", lambda c: FakeTiingo())
    monkeypatch.setattr(DesktopHost, "now", lambda self: NOW)
    monkeypatch.setattr(session, "build_preflight_gates", lambda **k: [
        GateSpec(n, f"gate_{n}", lambda n=n: GateResult(n, f"gate_{n}", True, "ok"))
        for n in range(1, 13)
    ])
    monkeypatch.setattr(session, "gate_10_price_data", lambda **k: GateResult(10, "price_data", True, "ok"))
    monkeypatch.setattr(session, "assemble_spy_signal", lambda *a, **k: assembled_signal())
    monkeypatch.setattr(poll, "process_pending", lambda *a: None)
    if status == "market_data_failed":
        def fail(*a, **k):
            raise EtoroNetworkError("offline")
        monkeypatch.setattr(session, "fetch_market_inputs", fail)
        monkeypatch.setattr(entry, "run_session", lambda **kw: session.run_session(**kw, sleep_until=lambda _: None))
    else:
        def fail(*a, **k):
            raise RuntimeError("offline persistence failure")
        monkeypatch.setattr(Repo, "record_price_window_fingerprint", fail)
    first = invoke(host)
    assert first["status"] == first["reason_code"] == status
    assert first["failed_gate"] == (10 if status == "market_data_failed" else None)
    assert first["error_type"] is None
    with Repo.open(default_db_path(host), host=host) as repo:
        row = repo.get_session(first["session_id"])
        assert row.state == "aborted" and row.finished_at is not None
        assert repo.get_intents(row.id) == []
    second = invoke(host)
    assert second["status"] == "previous_unfinished" and second["session_id"] == first["session_id"]


@pytest.fixture
def demo_home(tmp_path, config_path, monkeypatch):
    from .conftest import MINIMAL_CONFIG_TOML

    demo = DesktopHost(tmp_path / "nightwatch_demo")
    demo.config_dir().mkdir(parents=True)
    config = (MINIMAL_CONFIG_TOML.replace('mode = "dry_run"', 'mode = "live"')
              .replace('[strategy]', '[strategy]\nexit_mode = "fixed"')
              .replace('[risk]', '[risk]\nmax_notional_per_session_usd = 1000\n'
                       'max_daily_loss_usd = 100\nmax_open_lots = 10\nmax_orders_per_session = 3'))
    (demo.config_dir() / "config.toml").write_text(config)
    (demo.root / "ARM_DEMO").touch()
    return demo


def invoke_demo(demo, **kwargs):
    result = json.loads(entry.run_session_armed_demo(str(demo.root), now_utc=kwargs.pop("now_utc", NOW)))
    assert set(result) == KEYS
    assert MARKER not in json.dumps(result)
    return result


@pytest.mark.parametrize("reason", ["wrong_home", "not_armed_on_device", "real_not_allowed", "config_not_live"])
def test_demo_refusals_before_client(demo_home, monkeypatch, reason):
    demo = demo_home
    if reason == "wrong_home":
        demo = DesktopHost(demo.root.parent / "nightwatch_config")
    elif reason == "not_armed_on_device":
        (demo.root / "ARM_DEMO").unlink()
    else:
        path = demo.config_dir() / "config.toml"
        old, new = (('environment = "demo"', 'environment = "real"') if reason == "real_not_allowed"
                    else ('mode = "live"', 'mode = "dry_run"'))
        path.write_text(path.read_text().replace(old, new))
    calls = []
    monkeypatch.setattr(entry, "EtoroClient", lambda *a, **k: calls.append("client"))
    if reason in {"wrong_home", "not_armed_on_device"}:
        monkeypatch.setattr(entry, "load_config", lambda **k: pytest.fail("config loaded before device checks"))
    previous_host = get_host()
    monkeypatch.setenv("NIGHTWATCH_ARM", "previous")
    result = invoke_demo(demo)
    assert result["status"] == "refused" and result["reason_code"] == reason
    assert result["armed"] is False and calls == []
    assert not (demo.root / "data").exists()
    assert get_host() is previous_host
    import os
    assert os.environ["NIGHTWATCH_ARM"] == "previous"


@pytest.mark.parametrize("existing_old_db", [False, True])
@pytest.mark.parametrize("previous_arm", [None, "previous"])
@pytest.mark.parametrize("status", ["executed", "executed_partial", "halted_before_execution", "armed_refused_intents", "unknown"])
def test_demo_write_envelope_and_isolation(demo_home, tmp_path, monkeypatch, previous_arm, status, existing_old_db):
    import os

    demo = demo_home
    old_host = DesktopHost(tmp_path / "nightwatch_config")
    set_host(old_host)
    old_db = default_db_path(old_host)
    if existing_old_db:
        with Repo.open(old_db, host=old_host) as repo:
            repo.create_session("2026-10-01", "dry_run", "demo", False)
    before = old_db.read_bytes() if existing_old_db else None
    if previous_arm is None:
        monkeypatch.delenv("NIGHTWATCH_ARM", raising=False)
    else:
        monkeypatch.setenv("NIGHTWATCH_ARM", previous_arm)
    clients, closed = [], []

    def client(config, *, role, repo):
        assert role == "write" and not config.is_real_money
        clients.append(role)
        return SimpleNamespace(close=lambda: closed.append("client"))

    def run(**kwargs):
        assert kwargs["config"].is_armed and kwargs["wait"] is True
        assert session.arming_refusals(kwargs["config"]) == []
        assert kwargs["config"].risk.max_orders_per_session is not None
        assert os.environ["NIGHTWATCH_ARM"] == "1" and get_host().root == demo.root
        assert default_db_path(get_host()).resolve().is_relative_to(demo.root.resolve())
        assert default_db_path(demo).is_file()
        repo = kwargs["repo"]
        row = repo.create_session(NOW.date().isoformat(), "live", "demo", True)
        repo.record_preflight_check(PreflightCheck(row.id, 1, "halt", True, "{}"))
        repo.finish_session(row.id, "finished", "demo result")
        (demo.root / "HALT").touch()
        return session.SessionResult(row.id, status, None, MARKER)

    monkeypatch.setattr(entry, "EtoroClient", client)
    monkeypatch.setattr(entry, "TelegramNotifier", lambda *a, **k: SimpleNamespace(close=lambda: closed.append("notifier")))
    monkeypatch.setattr(entry, "TiingoClient", lambda *a: SimpleNamespace(close=lambda: closed.append("tiingo")))
    monkeypatch.setattr(entry, "run_session", run)
    first = invoke_demo(demo)
    assert first["armed"] is True and first["halt"] is True
    assert first["status"] == (status if status != "unknown" else "error")
    assert first["gates"] == [[1, "halt", True]]
    assert get_host() is old_host and os.environ.get("NIGHTWATCH_ARM") == previous_arm
    assert (old_db.read_bytes() if old_db.exists() else None) == before
    assert invoke_demo(demo)["status"] == "already_ran" and clients == ["write"]
    assert set(closed) == {"client", "notifier", "tiingo"}


@pytest.mark.parametrize("state,armed,expected", [
    ("finished", True, "already_ran"), ("finished", False, "previous_unfinished"),
    ("preflight", True, "previous_unfinished"), ("aborted", True, "previous_unfinished"),
])
def test_demo_dedupe_never_runs_again(demo_home, monkeypatch, state, armed, expected):
    with Repo.open(default_db_path(demo_home), host=demo_home) as repo:
        row = repo.create_session(NOW.date().isoformat(), "live", "demo", armed)
        if state != "preflight":
            repo.finish_session(row.id, state, "outcome")
        before = repo.get_session(row.id)
    monkeypatch.setattr(entry, "EtoroClient", lambda *a, **k: pytest.fail("dedupe must precede client"))
    result = invoke_demo(demo_home)
    assert result["status"] == expected and result["armed"] is False
    with Repo.open(default_db_path(demo_home), host=demo_home) as repo:
        assert repo.get_session(row.id) == before


@pytest.mark.parametrize("stage", ["config", "client", "session"])
def test_demo_failure_restores_environment_and_host(demo_home, monkeypatch, stage):
    import os

    previous = get_host()
    monkeypatch.setenv("NIGHTWATCH_ARM", "previous")

    def fail(*a, **k):
        raise ConfigError(MARKER)

    monkeypatch.setattr(entry, "EtoroClient", lambda *a, **k: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(entry, "TelegramNotifier", lambda *a, **k: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(entry, "TiingoClient", lambda *a: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(entry, {"config": "load_config", "client": "EtoroClient", "session": "run_session"}[stage], fail)
    result = invoke_demo(demo_home)
    assert result["status"] == "error" and result["error_type"] == "ConfigError"
    assert result["armed"] is (stage == "session")
    assert get_host() is previous and os.environ["NIGHTWATCH_ARM"] == "previous"


def test_dry_restores_previous_host(wired, tmp_path):
    old = DesktopHost(tmp_path / "nightwatch_config")
    set_host(old)
    assert invoke(wired[0])["status"] == "would_have_done"
    assert get_host() is old


def test_demo_window_and_lock_unchanged(demo_home, monkeypatch):
    monkeypatch.setattr(entry, "EtoroClient", lambda *a, **k: pytest.fail("must not construct client"))
    assert invoke_demo(demo_home, now_utc=datetime(2026, 11, 6, 20, 35, 1, tzinfo=timezone.utc))["status"] == "out_of_window"
    with entry._session_lock(demo_home.data_dir() / "session.lock") as acquired:
        assert acquired
        assert invoke_demo(demo_home)["status"] == "busy"


def test_demo_real_money_guard_independent_of_environment(demo_home, monkeypatch):
    from nightwatch.config import EtoroEnvironment

    monkeypatch.setattr(entry, "load_config", lambda **k: SimpleNamespace(
        environment=EtoroEnvironment.DEMO, etoro=SimpleNamespace(is_real_money=True), is_armed=True))
    monkeypatch.setattr(entry, "EtoroClient", lambda *a, **k: pytest.fail("real-money client constructed"))
    result = invoke_demo(demo_home)
    assert result["reason_code"] == "real_not_allowed" and result["armed"] is False


def test_demo_prior_unfinished_armed_session_blocks_gate_five(demo_home, monkeypatch):
    from nightwatch.engine.preflight import gate_5_database

    with Repo.open(default_db_path(demo_home), host=demo_home) as repo:
        previous = repo.create_session("2026-10-01", "live", "demo", True)
    monkeypatch.setattr(entry, "EtoroClient", lambda *a, **k: SimpleNamespace(
        close=lambda: None, set_deadline_in=lambda seconds: None))
    monkeypatch.setattr(entry, "TiingoClient", lambda c: FakeTiingo())
    monkeypatch.setattr(DesktopHost, "now", lambda self: NOW)

    def gates(**kwargs):
        return [GateSpec(n, f"gate_{n}",
                         lambda n=n: gate_5_database(repo=kwargs["repo"], session_id=kwargs["session_id"])
                         if n == 5 else GateResult(n, f"gate_{n}", True, "ok"))
                for n in range(1, 13)]

    monkeypatch.setattr(session, "build_preflight_gates", gates)
    result = invoke_demo(demo_home)
    assert result["status"] == "preflight_failed" and result["failed_gate"] == 5
    with Repo.open(default_db_path(demo_home), host=demo_home) as repo:
        assert repo.get_session(previous.id) == previous
