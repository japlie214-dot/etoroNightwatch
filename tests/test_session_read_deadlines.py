"""T050: armed sessions keep existing phase handlers and clear before sends."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from nightwatch.config import TradingMode
from nightwatch.engine import preflight, session
from nightwatch.engine.executor import ExecutionReport
from nightwatch.engine.preflight import GateResult, GateSpec, NetworkRetry
from nightwatch.etoro.client import EtoroClient
from nightwatch.etoro.ratelimit import RateLimiter
from nightwatch.market.source import TiingoClient
from .test_read_deadlines import Timer
from .test_session_armed import BUY, filled
from .test_session_market import DAY, FakeTiingo, assembled_signal, config, market_client


@pytest.fixture
def deadline_session(repo, host, config, monkeypatch):
    timer = Timer()
    cutoff = session.session_close_datetime(DAY) - timedelta(minutes=25)
    origin = cutoff - timedelta(seconds=105)
    monkeypatch.setattr(host, "now", lambda: origin + timedelta(seconds=timer.now))
    cfg = replace(config, mode=TradingMode.LIVE, arm_flag_present=True,
                  risk=replace(config.risk, max_daily_loss_usd=100, max_open_lots=10,
                               max_orders_per_session=1))
    attempts = []
    state = {"failure": True, "elapsed": 6, "phase": "market", "read_failures": {}}

    def handler(request):
        attempts.append((timer.now, request.method, request.url.path))
        if state["failure"]:
            timer.now += state["elapsed"]
            if request.url.path == "/network":
                raise httpx.ConnectError("offline probe")
            return httpx.Response(503)
        failures = state["read_failures"]
        if request.url.path in failures and failures[request.url.path] > 0:
            failures[request.url.path] -= 1
            return httpx.Response(503)
        return httpx.Response(200, json={})

    http = httpx.Client(base_url="https://offline.test", transport=httpx.MockTransport(handler))
    client = EtoroClient(
        replace(cfg.etoro, secrets={"ETORO_DEMO_PRIVATE_KEY": "synthetic-private",
                                   "ETORO_PUBLIC_KEY": "synthetic-public"}),
        client=http, sleep=timer.sleep, clock=timer.clock,
        rate_limiter=RateLimiter(clock=timer.clock),
    )
    tiingo = TiingoClient(replace(cfg.tiingo, secrets={"TIINGO_KEY": "synthetic-tiingo"}),
                          client=http, sleep=timer.sleep, clock=timer.clock)
    retry = NetworkRetry(clock=timer.clock)
    monkeypatch.setattr(session, "NetworkRetry", lambda: retry)
    fake = market_client()
    for name in ("resolve_instrument_id", "get_rates", "get_candles", "get_portfolio",
                 "get_balance", "get_pnl"):
        monkeypatch.setattr(client, name, getattr(fake, name))
    monkeypatch.setattr(tiingo, "get_iex_quotes", FakeTiingo().get_iex_quotes)
    monkeypatch.setattr(session, "assemble_spy_signal", lambda *a, **k: assembled_signal())
    monkeypatch.setattr(session, "gate_10_price_data", lambda **k: GateResult(10, "price_data", True, "ok"))
    build = session.build_preflight_gates
    monkeypatch.setattr(session, "etoro_reachability_probe", lambda *a: lambda: http.get("/network"))
    monkeypatch.setattr(session, "gate_3_network", lambda **k: preflight.gate_3_network(
        **k, clock=timer.clock, sleep=timer.sleep))

    def gates(**kwargs):
        assert client._deadline == tiingo._deadline == retry._deadline == 105
        real = build(**kwargs)
        return [real[n - 1] if state["phase"] == "network" and n == 3 else
                GateSpec(n, f"gate_{n}", lambda n=n: GateResult(n, f"gate_{n}", True, "ok"))
                for n in range(1, 13)]

    monkeypatch.setattr(session, "build_preflight_gates", gates)
    # Any accidental use of a real transport, including a module-level HTTP call, fails locally.
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda *a: pytest.fail("real transport used"))
    h = SimpleNamespace(timer=timer, cutoff=cutoff, cfg=cfg, client=client, tiingo=tiingo,
                        retry=retry, attempts=attempts, state=state)
    h.run = lambda: session._run_session_core(host=host, config=cfg, repo=repo, client=client,
                                             trading_date=DAY, tiingo=tiingo, wait=False)
    yield h
    http.close()


@pytest.mark.parametrize("phase,status,gate", [
    ("network", "preflight_failed", 3), ("market", "market_data_failed", 10),
    ("history", "signal_failed", None), ("portfolio", "decide_failed", None),
    ("balance", "decide_failed", None), ("fallback", "aborted_before_orders", None),
])
def test_armed_deadline_uses_existing_phase_handler(deadline_session, repo, monkeypatch, phase, status, gate):
    h = deadline_session
    h.state["phase"] = phase

    def read():
        return h.client._request("GET", "/synthetic", request_class="read")

    if phase == "market":
        monkeypatch.setattr(h.client, "get_rates", lambda *a: read())
    elif phase == "history":
        monkeypatch.setattr(session, "assemble_spy_signal", lambda *a, **k: h.tiingo._request("/history", None))
    elif phase in ("portfolio", "balance"):
        monkeypatch.setattr(h.client, "get_" + phase, read)
    elif phase == "fallback":
        monkeypatch.setattr(repo, "record_price_window_fingerprint", lambda *a: read())
    result = h.run()
    assert result.status == status and result.failed_gate == gate
    assert "deadline" in result.reason
    row = repo.get_session(result.session_id)
    assert row.state == "aborted" and row.finished_at is not None and row.outcome == result.reason
    assert len(h.attempts) == 1 and h.attempts[0][0] == 100
    assert h.timer.now == 106 and h.timer.sleeps == []
    # The transport was already running at the cutoff; no bounded loop starts another attempt.
    assert all(start < 105 for start, _, _ in h.attempts)


def test_armed_deadline_cleared_before_sends_and_postorder_reads(deadline_session, repo, monkeypatch):
    h = deadline_session
    h.state["failure"] = False
    h.state["read_failures"] = {"/after-etoro": 1, "/after-tiingo": 1}
    monkeypatch.setattr(session, "decide", lambda *a: SimpleNamespace(
        intents=[BUY], new_state=replace(session.load(repo), bars=1),
        reason="offline decision", warnings=[], updated_buckets=[]))
    sent = []

    def execute(**kwargs):
        assert h.client._deadline is h.tiingo._deadline is h.retry._deadline is None
        h.timer.now = 106  # Later reads must be free of the old close-25 deadline.
        h.client._request("POST", "/send", request_class="write")
        sent.append(True)
        h.client._request("GET", "/after-etoro", request_class="read")
        h.tiingo._request("/after-tiingo", None)
        calls = []

        def probe():
            calls.append(h.timer.now)
            if len(calls) == 1:
                raise httpx.ConnectError("offline once")

        assert preflight.gate_3_network(probe=probe, network_retry=h.retry,
                                        clock=h.timer.clock, sleep=h.timer.sleep).passed
        assert len(calls) == 2
        return ExecutionReport((filled(),), None, (), (), ())

    monkeypatch.setattr(session, "execute_buys", execute)
    result = h.run()
    assert result.status == "executed" and sent == [True]
    assert [method for _, method, _ in h.attempts] == ["POST", "GET", "GET", "GET", "GET"]
    assert [delay for _, delay in h.timer.sleeps] == [1, 1, 5]
    assert all(start > 105 for start, _, _ in h.attempts)
