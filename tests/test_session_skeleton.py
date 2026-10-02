"""S6a session persistence and lazy preflight wiring, without network calls."""

from dataclasses import replace
from datetime import date, datetime, timezone
import json

import httpx
import pytest

from nightwatch import cli
from nightwatch.config import load_config
from nightwatch.db.repo import PreflightCheck
from nightwatch.engine import session
from nightwatch.engine.preflight import GateResult, GateSpec
from nightwatch.etoro.models import Eligibility


TRADING_DATE = date(2026, 10, 2)


class FakeClient:
    environment = "demo"

    def __init__(self):
        self.portfolio_calls = 0
        self.closed = False
        self.order_sizes = []

    def get_balance(self):
        return 1000.0

    def probe_environment(self):
        return self.environment

    def resolve_instrument_id(self, symbol):
        return {"SPY": 1, "SCHG": 2, "UPRO": 3}[symbol]

    def check_eligibility_for_order(self, instrument_id, amount):
        self.order_sizes.append((instrument_id, amount))
        return Eligibility(instrument_id, True, 10.0, 10.0)

    def get_portfolio(self):
        self.portfolio_calls += 1
        return []

    def close(self):
        self.closed = True


@pytest.fixture
def config(config_path, secrets_path, host):
    return load_config(config_path=config_path, secrets_path=secrets_path, env={}, host=host)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refused(*args, **kwargs):
        raise AssertionError("test attempted a real HTTP call")

    monkeypatch.setattr(httpx, "get", refused)
    monkeypatch.setattr(httpx.Client, "send", refused)


def fake_gates(called, failed_gate=None):
    def check(number):
        called.append(number)
        return GateResult(number, f"gate_{number}", number != failed_gate, "offline" if number == failed_gate else "ok")

    return [GateSpec(n, f"gate_{n}", lambda n=n: check(n)) for n in range(1, 13)]


def test_armed_refused_without_session(repo, host, config_path, secrets_path, monkeypatch):
    config_path.write_text(config_path.read_text().replace('mode = "dry_run"', 'mode = "live"'))
    config = load_config(
        config_path=config_path, secrets_path=secrets_path,
        env={"NIGHTWATCH_ARM": "1"}, host=host,
    )
    called = []
    monkeypatch.setattr(session, "build_preflight_gates", lambda **kwargs: fake_gates(called))
    result = session.run_session(host=host, config=config, repo=repo, client=FakeClient(), trading_date=TRADING_DATE)
    assert result == session.SessionResult(None, "refused_armed", None, "armed execution not built (S7); refusing")
    assert repo.latest_session() is None
    assert called == []


def test_existing_session_is_noop(repo, host, config, monkeypatch):
    existing = repo.create_session(TRADING_DATE.isoformat(), "dry_run", "demo", False)
    called = []
    monkeypatch.setattr(session, "build_preflight_gates", lambda **kwargs: fake_gates(called))
    result = session.run_session(host=host, config=config, repo=repo, client=FakeClient(), trading_date=TRADING_DATE)
    assert result == session.SessionResult(
        existing.id, "already_ran", None,
        f"session {existing.id} already exists for 2026-10-02 (state started)",
    )
    assert repo.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    assert repo.get_session(existing.id) == existing
    assert called == []


@pytest.mark.parametrize("failed_gate", [None, 3])
def test_session_verdict_persisted(repo, host, config, monkeypatch, failed_gate):
    called = []
    monkeypatch.setattr(session, "build_preflight_gates", lambda **kwargs: fake_gates(called, failed_gate))
    result = session.run_session(host=host, config=config, repo=repo, client=FakeClient(), trading_date=TRADING_DATE)
    row = repo.get_session(result.session_id)
    assert row.mode == "dry_run" and row.environment == "demo" and not row.armed
    assert row.preflight_failed_gate == result.failed_gate == failed_gate
    assert datetime.fromisoformat(row.preflight_at).utcoffset().total_seconds() == 0
    expected_numbers = list(range(1, 13 if failed_gate is None else 4))
    assert called == expected_numbers
    checks = repo.get_preflight_checks(row.id)
    assert [c.gate for c in checks] == expected_numbers
    if failed_gate is None:
        assert result.status == "preflight_passed"
        assert result.reason == "preflight passed"
        assert row.state == "settled" and row.preflight_verdict == "pass"
        assert row.finished_at is None
        assert all(c.passed for c in checks)
    else:
        assert result.status == "preflight_failed"
        assert row.state == "aborted" and row.preflight_verdict == "fail"
        assert row.outcome == result.reason == "preflight gate 3 gate_3: offline"
        assert row.finished_at is not None
        assert not checks[-1].passed


def test_build_gates_lazy_wiring_and_portfolio_cache(repo, host, config, monkeypatch):
    config = replace(config, risk=replace(config.risk, max_open_lots=10))
    client = FakeClient()
    s = repo.create_session(TRADING_DATE.isoformat(), "dry_run", "demo", False, state="preflight")
    calls = []
    fixed_now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(302, headers={"Date": "Fri, 02 Oct 2026 12:00:00 GMT"})

    monkeypatch.setattr(session.httpx, "get", get)
    gates = session.build_preflight_gates(
        host=host, config=config, repo=repo, client=client,
        session_id=s.id, trading_date=TRADING_DATE, now=lambda: fixed_now,
    )
    assert [g.number for g in gates] == list(range(1, 13))
    assert calls == [] and client.portfolio_calls == 0 and client.order_sizes == []
    assert gates[9].check() == GateResult(10, "price_data", False, "not implemented")
    assert all(g.check().passed for g in gates[:9])
    assert calls == [(config.etoro.base_url + "/", {"timeout": 10.0, "follow_redirects": False})] * 2
    assert client.order_sizes == [(2, config.strategy.min_order_size), (3, config.strategy.min_order_size)]
    assert gates[10].check().passed
    assert gates[11].check().passed
    assert client.portfolio_calls == 1  # An empty portfolio is cached too.


def test_gate_12_without_gate_11_fetches_portfolio(repo, host, config):
    client = FakeClient()
    gates = session.build_preflight_gates(
        host=host, config=config, repo=repo, client=client, session_id=1, trading_date=TRADING_DATE,
    )
    assert gates[11].check().gate == 12
    assert client.portfolio_calls == 1


def test_halt_stops_before_network(repo, host, config):
    host.halt_flag_path().parent.mkdir(parents=True, exist_ok=True)
    host.halt_flag_path().write_text("halt")
    client = FakeClient()
    result = session.run_session(host=host, config=config, repo=repo, client=client, trading_date=TRADING_DATE)
    assert result.status == "preflight_failed" and result.failed_gate == 1
    assert [r.gate for r in repo.get_preflight_checks(result.session_id)] == [1]
    assert client.portfolio_calls == 0


@pytest.mark.parametrize("connect_error", [False, True])
def test_fetch_server_date(monkeypatch, connect_error):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        if connect_error:
            raise httpx.ConnectError("offline")
        return httpx.Response(302, headers={"Date": "Fri, 02 Oct 2026 12:00:00 GMT"})

    monkeypatch.setattr(session.httpx, "get", get)
    assert session.fetch_server_date("https://example.test", timeout=2.0) == (
        None if connect_error else "Fri, 02 Oct 2026 12:00:00 GMT"
    )
    assert calls == [("https://example.test/", {"timeout": 2.0, "follow_redirects": False})]


@pytest.mark.parametrize("status", ["preflight_failed", "refused_armed", "preflight_passed", "already_ran"])
def test_cli_run_summary(host, config, monkeypatch, capsys, status):
    client = FakeClient()
    monkeypatch.setattr(cli, "load_config", lambda **kwargs: config)
    monkeypatch.setattr(cli, "get_host", lambda: host)

    def make_client(etoro, *, role, repo):
        assert etoro is config.etoro and role == "read"
        assert repo.schema_version() > 0
        return client

    def run_session(**kwargs):
        assert kwargs["trading_date"] == date(2026, 10, 3)
        assert kwargs["client"] is client
        if status == "refused_armed":
            return session.SessionResult(None, status, None, "armed execution not built (S7); refusing")
        repo = kwargs["repo"]
        s = repo.create_session("2026-10-03", "dry_run", "demo", False)
        failed = status == "preflight_failed"
        repo.record_preflight_check(PreflightCheck(s.id, 3, "network", not failed, json.dumps({"reason": "offline" if failed else "ok"})))
        return session.SessionResult(s.id, status, 3 if failed else None, "preflight gate 3 network: offline" if failed else "ok")

    monkeypatch.setattr(cli, "EtoroClient", make_client)
    monkeypatch.setattr(cli, "run_session", run_session)
    assert cli.main(["run", "--date", "2026-10-03"]) == (1 if status in ("preflight_failed", "refused_armed") else 0)
    out = capsys.readouterr().out
    assert f"status: {status}" in out
    assert "session_id:" in out and "failed_gate:" in out and "reason:" in out
    if status == "preflight_failed":
        assert "failed_gate: 3" in out and "gate 3 network FAIL offline" in out
    elif status == "refused_armed":
        assert "session_id: None" in out and "gate 3" not in out
    else:
        assert "gate 3 network PASS ok" in out
    assert client.closed
