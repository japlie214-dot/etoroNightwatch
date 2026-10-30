"""Offline Android entry contract, including real-engine and kernel-lock proofs."""

from dataclasses import replace
from datetime import datetime, timezone
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
KEYS = {"status", "armed", "session_id", "failed_gate", "reason_code", "error_type", "gates"}


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
    assert MARKER not in json.dumps(result)
    return result


def test_happy_shape_and_persisted_gates(wired):
    host, clients, closed = wired
    result = invoke(host)
    assert result == dict(status="would_have_done", armed=False, session_id=1, failed_gate=None,
                          reason_code="would_have_done", error_type=None,
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
