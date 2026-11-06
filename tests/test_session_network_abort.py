"""T047: synthetic sessions, injected clocks and offline broker failures."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nightwatch.config import TradingMode
from nightwatch.db.repo import Intent, Order
from nightwatch.engine import session
from nightwatch.engine import executor
from nightwatch.engine.preflight import GateResult, GateSpec
from nightwatch.etoro.errors import EtoroNetworkError, EtoroValidationError
from nightwatch.etoro.models import Position
from nightwatch.logging_setup import RedactionFilter
from .test_session_market import DAY, FakeTiingo, assembled_signal, config, no_network, market_client


@pytest.fixture
def harness(repo, host, config, monkeypatch):
    clock = [session.session_close_datetime(DAY) - timedelta(minutes=30)]
    sleeps, attempts = [], []
    client = market_client()
    monkeypatch.setattr(host, "now", lambda: clock[0])
    monkeypatch.setattr(session, "assemble_spy_signal", lambda *a, **k: assembled_signal())
    monkeypatch.setattr(session, "gate_10_price_data", lambda **k: GateResult(10, "price_data", True, "ok"))

    def gates(**kwargs):
        def check(n):
            if n == 11 and kwargs["recover"] is not None:
                kwargs["recovery_reports"].append(kwargs["recover"]())
            return GateResult(n, f"gate_{n}", True, "ok")
        return [GateSpec(n, f"gate_{n}", lambda n=n: check(n)) for n in range(1, 13)]

    monkeypatch.setattr(session, "build_preflight_gates", gates)
    fetch = session.fetch_market_inputs
    h = SimpleNamespace(config=config, client=client, clock=clock, sleeps=sleeps,
                        attempts=attempts, failures=[], elapsed=0, oversleep=0, trading_date=DAY)

    def market(**kwargs):
        attempts.append(clock[0])
        clock[0] += timedelta(seconds=h.elapsed)
        if h.failures:
            failure = h.failures.pop(0)
            if failure is not None:
                raise failure
        return fetch(**kwargs)

    def sleep(target):
        sleeps.append((target - clock[0]).total_seconds())
        clock[0] = target + timedelta(seconds=h.oversleep)

    monkeypatch.setattr(session, "fetch_market_inputs", market)
    from nightwatch.notify import poll
    monkeypatch.setattr(poll, "process_pending", lambda *a: None)
    h.notifier = SimpleNamespace(send=Mock())
    h.run = lambda: session.run_session(host=host, config=h.config, repo=repo, client=client,
                                      trading_date=h.trading_date, tiingo=FakeTiingo(), wait=False,
                                      sleep_until=sleep, notifier=h.notifier)
    return h


@pytest.mark.parametrize("armed", [False, True])
def test_three_network_failures_finish_session(harness, repo, armed):
    h = harness
    if armed:
        h.config = replace(h.config, mode=TradingMode.LIVE, arm_flag_present=True,
                           risk=replace(h.config.risk, max_daily_loss_usd=100,
                                        max_open_lots=10, max_orders_per_session=1))
    h.failures = [EtoroNetworkError("offline") for _ in range(3)]
    try:
        result = h.run()
    except Exception:
        row = repo.get_session_by_date(DAY.isoformat())
        print(f"escaped: state={row.state}, finished_at={row.finished_at}, intents={len(repo.get_intents(row.id))}")
        raise
    row = repo.get_session(result.session_id)
    assert result.status == "market_data_failed" and result.failed_gate == 10
    assert row.state == "aborted" and row.finished_at is not None
    assert row.outcome == result.reason
    assert row.outcome.startswith("market data: EtoroNetworkError: offline")
    assert repo.get_intents(row.id) == []
    assert len(h.attempts) == 3 and h.sleeps == [20, 20]
    assert all(t < session.session_close_datetime(DAY) - timedelta(minutes=25) for t in h.attempts)
    assert "market_data_failed (gate 10)" in h.notifier.send.call_args.args[0]


def test_retry_success_matches_control(harness, repo):
    h = harness
    h.failures = [EtoroNetworkError("offline"), None]
    result = h.run()
    decision = repo.get_decision(result.session_id)
    assert result.status == "would_have_done" and decision is not None
    assert len(h.attempts) == 2 and h.sleeps == [20]
    # Same inputs and strategy decision as the no-failure invocation.
    inputs = session.build_ladder_inputs(
        iso=DAY.isoformat(), year=DAY.year, signal=assembled_signal(),
        prices={"SPY": 100, "UPRO": 100, "SCHG": 100}, positions=[],
        symbol_by_instrument={1: "SPY", 2: "SCHG", 3: "UPRO"},
        balance=h.client.get_balance(), repo=repo, cash_reserve_usd=h.config.strategy.cash_reserve_usd,
    )[0]
    control = session.decide(inputs, session.load(repo), session.LadderConfig.from_strategy_config(h.config.strategy))
    assert decision.intents == [session.intent_to_dict(i) for i in control.intents]
    assert result.reason.startswith(f"would have done {len(control.intents)} intent(s)")


def test_validation_error_is_not_retried(harness, repo):
    harness.failures = [EtoroValidationError("bad request")]
    result = harness.run()
    assert result.status == "market_data_failed" and result.failed_gate == 10
    assert result.reason.startswith("market data: EtoroValidationError: bad request")
    assert len(harness.attempts) == 1 and harness.sleeps == []
    assert repo.get_session(result.session_id).finished_at is not None


@pytest.mark.parametrize("case,attempts", [("past", 0), ("equal", 0), ("no_budget", 1),
                                          ("late_success", 1), ("equal_success", 1), ("oversleep", 1)])
def test_cutoff(harness, repo, case, attempts):
    h = harness
    cutoff = session.session_close_datetime(DAY) - timedelta(minutes=25)
    if case in ("past", "equal"):
        h.clock[0] = cutoff + timedelta(microseconds=1 if case == "past" else 0)
    elif case == "no_budget":
        h.clock[0] = cutoff - timedelta(seconds=30)
        h.failures = [EtoroNetworkError("offline")]
    elif case in ("late_success", "equal_success"):
        h.clock[0] = cutoff - timedelta(seconds=10)
        h.elapsed = 11 if case == "late_success" else 10
    else:
        h.failures = [EtoroNetworkError("offline")]
        h.oversleep = 300
    result = h.run()
    assert result.status == "market_data_failed" and result.failed_gate == 10
    assert "cutoff" in result.reason
    assert len(h.attempts) == attempts
    assert repo.get_decision(result.session_id) is None
    assert repo.get_session(result.session_id).state == "aborted"


@pytest.mark.parametrize("stage", ["record_price_window_fingerprint", "insert_decision"])
@pytest.mark.parametrize("sanitizer_fails", [False, True])
def test_unhandled_preorder_error_is_sanitized(harness, repo, monkeypatch, stage, sanitizer_fails):
    h = harness
    planted = "short-private-value"
    h.config = replace(h.config, etoro=replace(h.config.etoro, secrets={"ETORO_PUBLIC_KEY": planted}))
    def fail(*a, **k):
        raise RuntimeError("x " * 145 + planted + " tail" * 100)
    monkeypatch.setattr(repo, stage, fail)
    if sanitizer_fails:
        monkeypatch.setattr(RedactionFilter, "redact", Mock(side_effect=ValueError("sanitize failed")))
    result = h.run()
    row = repo.get_session(result.session_id)
    text = h.notifier.send.call_args.args[0]
    assert result.status == "aborted_before_orders" and row.state == "aborted"
    assert row.finished_at is not None and row.outcome == result.reason
    assert planted not in row.outcome and planted not in result.reason and planted not in text
    if sanitizer_fails:
        assert result.reason == "aborted before orders: RuntimeError"
    else:
        expected = ("x " * 145 + "***REDACTED***" + " tail" * 100)[:300]
        assert result.reason == "aborted before orders: RuntimeError: " + expected
    assert "aborted_before_orders" in text


@pytest.mark.parametrize("uncertain", ["intent", "intent_read"])
def test_uncertain_errors_propagate_original(harness, repo, monkeypatch, uncertain):
    error = RuntimeError("original failure")
    if uncertain == "intent":
        def fail(session_id, *a, **k):
            repo.insert_intent(Intent(session_id, "synthetic-intent", 1, "SCHG", "buy"))
            raise error
        monkeypatch.setattr(repo, "finish_session", fail)
    else:
        monkeypatch.setattr(repo, "insert_decision", Mock(side_effect=error))
        monkeypatch.setattr(repo, "get_intents", Mock(side_effect=ValueError("read failed")))
    with pytest.raises(RuntimeError) as raised:
        harness.run()
    assert raised.value is error
    row = repo.get_session_by_date(DAY.isoformat())
    assert row.state == "preflight" and row.finished_at is None


@pytest.mark.parametrize("write,failure", [(True, "fetch"), (True, "unexpected"), (False, "fetch")])
def test_older_session_tp_recovery_controls(harness, repo, monkeypatch, write, failure):
    h = harness
    h.config = replace(h.config, mode=TradingMode.LIVE, arm_flag_present=True,
                       risk=replace(h.config.risk, max_daily_loss_usd=100,
                                    max_open_lots=10, max_orders_per_session=1))
    old = repo.create_session("2026-10-01", "live", "demo", True)
    iid = repo.insert_intent(Intent(old.id, "old-intent", 1, "SCHG", "buy", status="filled"))
    oid = repo.insert_order(Order(iid, old.id, "old-intent", "SCHG", "buy"))
    repo.insert_tp_clear(oid, "old-position", "old-tp", 1)
    repo.finish_session(old.id, "finished", "old session")
    position = Position("old-position", 2, 1, 100, 100, 1, True, take_profit_rate=150,
                        is_no_take_profit=not write)
    h.client.get_portfolio = lambda: [position]
    calls = []
    def clear(*a, **k):
        calls.append("PATCH")
        raise EtoroValidationError("offline patch refusal")
    h.client.clear_take_profit = clear
    monkeypatch.setattr(executor.time, "sleep", lambda _: None)
    error = EtoroNetworkError("offline") if failure == "fetch" else RuntimeError("fingerprint failure")
    if failure == "fetch":
        h.failures = [error] * 3
    else:
        monkeypatch.setattr(repo, "record_price_window_fingerprint", Mock(side_effect=error))
    if write:
        with pytest.raises(type(error)) as raised:
            h.run()
        assert raised.value is error
        row = repo.get_session_by_date(DAY.isoformat())
        assert row.state == "preflight" and row.finished_at is None
        assert calls == ["PATCH"] and h.sleeps == [] and len(h.attempts) == 1
    else:
        result = h.run()
        assert result.status == "market_data_failed" and len(h.attempts) == 3
        assert calls == [] and repo.get_session(result.session_id).state == "aborted"


@pytest.mark.parametrize("error_type", [EtoroNetworkError, EtoroValidationError])
def test_market_failure_redacts_config_secret_in_db_result_and_notification(harness, repo, error_type):
    h = harness
    planted = "opaque-private-key"
    h.config = replace(h.config, tiingo=replace(h.config.tiingo, secrets={"TIINGO_KEY": planted}))
    h.failures = [error_type("offline " + planted)] * 3
    result = h.run()
    row = repo.get_session(result.session_id)
    assert result.status == "market_data_failed"
    assert planted not in row.outcome
    assert row.outcome == result.reason and "***REDACTED***" in result.reason
    assert planted not in h.notifier.send.call_args.args[0]


@pytest.mark.parametrize("uncertain", ["intent", "transient_read_failure"])
def test_market_failure_does_not_retry_when_intent_state_is_uncertain(harness, repo, monkeypatch, uncertain):
    error = EtoroNetworkError("original market failure")
    harness.failures = [error] * 3
    if uncertain == "intent":
        insert = repo.update_session
        def update(session_id, **kwargs):
            insert(session_id, **kwargs)
            if "preflight_verdict" in kwargs:
                repo.insert_intent(Intent(session_id, "synthetic-intent", 1, "SCHG", "buy"))
        monkeypatch.setattr(repo, "update_session", update)
    else:
        monkeypatch.setattr(repo, "get_intents", Mock(side_effect=[ValueError("read failed"), []]))
    with pytest.raises(EtoroNetworkError) as raised:
        harness.run()
    assert raised.value is error
    assert len(harness.attempts) == 1 and harness.sleeps == []
    row = repo.get_session_by_date(DAY.isoformat())
    assert row.state == "preflight" and row.finished_at is None
    if uncertain == "transient_read_failure":
        assert repo.get_intents.call_count == 1


@pytest.mark.parametrize("remaining,attempts", [(50, 2), (49, 1)])
def test_retry_budget_boundary(harness, remaining, attempts):
    h = harness
    cutoff = session.session_close_datetime(DAY) - timedelta(minutes=25)
    h.clock[0] = cutoff - timedelta(seconds=remaining)
    h.failures = [EtoroNetworkError("offline"), None]
    result = h.run()
    assert len(h.attempts) == attempts
    assert result.status == ("would_have_done" if attempts == 2 else "market_data_failed")
    assert h.sleeps == ([20] if attempts == 2 else [])


def test_keyboard_interrupt_is_not_caught(harness, repo, monkeypatch):
    monkeypatch.setattr(repo, "insert_decision", Mock(side_effect=KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        harness.run()
    assert repo.get_session_by_date(DAY.isoformat()).finished_at is None


@pytest.mark.parametrize("method,wrote", [("GET", False), ("POST", True), ("PATCH", True)])
def test_recovery_transport_write_marker_and_restoration(harness, repo, monkeypatch, method, wrote):
    h = harness
    h.config = replace(h.config, mode=TradingMode.LIVE, arm_flag_present=True,
                       risk=replace(h.config.risk, max_daily_loss_usd=100,
                                    max_open_lots=10, max_orders_per_session=1))
    request = Mock(return_value=None)
    monkeypatch.setattr(h.client, "_request", request, raising=False)
    def recover(**kwargs):
        kwargs["client"]._request(method=method, path="/synthetic")
        return executor.RecoveryReport((), (), (), (), ())
    monkeypatch.setattr(session, "recover_unfinished", recover)
    error = RuntimeError("after recovery")
    monkeypatch.setattr(repo, "record_price_window_fingerprint", Mock(side_effect=error))
    if wrote:
        with pytest.raises(RuntimeError) as raised:
            h.run()
        assert raised.value is error
    else:
        assert h.run().status == "aborted_before_orders"
    assert h.client._request is request
    request.assert_called_once_with(method=method, path="/synthetic")


def test_aborted_same_date_stays_blocked_next_date_is_not(harness, repo):
    harness.failures = [EtoroNetworkError("offline")] * 3
    first = harness.run()
    again = harness.run()
    assert again.status == "already_ran" and again.session_id == first.session_id
    assert repo.unfinished_sessions() == []
    harness.trading_date = DAY + timedelta(days=3)
    harness.clock[0] = session.session_close_datetime(harness.trading_date) - timedelta(minutes=30)
    next_date = harness.run()
    assert next_date.status == "would_have_done" and next_date.session_id != first.session_id
    assert repo.get_session(next_date.session_id).trading_date == harness.trading_date.isoformat()


@pytest.mark.parametrize("status", ["market_data_failed", "aborted_before_orders"])
@pytest.mark.parametrize("sanitizer_fails", [False, True])
def test_abort_redacts_recovered_buy_annotation_failure(harness, repo, monkeypatch, status, sanitizer_fails):
    from nightwatch.db.repo import Decision
    from nightwatch.etoro.models import OrderResult, OrderStatus

    h = harness
    planted = "recovery-private-value"
    h.config = replace(h.config, mode=TradingMode.LIVE, arm_flag_present=True,
                       etoro=replace(h.config.etoro, secrets={"ETORO_PUBLIC_KEY": planted}),
                       risk=replace(h.config.risk, max_daily_loss_usd=100,
                                    max_open_lots=10, max_orders_per_session=1))
    old = repo.create_session("2026-10-01", "live", "demo", True)
    repo.insert_intent(Intent(old.id, "old-pending-buy", 1, "SCHG", "buy",
                              instrument_id=2, book="catapult", amount_usd=100))
    repo.insert_decision(Decision(old.id, "2026-10-01", spy_close=100, spy_ath=110,
                                  drawdown=-0.1, tier=1))
    repo.finish_session(old.id, "finished", "old session")
    fill = OrderResult("old-order", "old-pending-buy", OrderStatus.FILLED,
                       position_id="old-position", fill_amount_usd=100)
    h.client.recover_by_reference_id = Mock(return_value=fill)
    h.client.get_portfolio = lambda: [Position("old-position", 2, 1, 100, 100, 1, True,
                                              is_no_take_profit=True)]
    h.client.clear_take_profit = Mock(side_effect=AssertionError("TP already absent"))
    monkeypatch.setattr(repo, "finalize_filled_buy", Mock(side_effect=RuntimeError("annotation " + planted)))
    if status == "market_data_failed":
        h.failures = [EtoroNetworkError("offline") for _ in range(3)]
    else:
        monkeypatch.setattr(repo, "record_price_window_fingerprint", Mock(side_effect=RuntimeError("fingerprint failure")))
    redact = Mock(side_effect=ValueError("sanitizer failure")) if sanitizer_fails else Mock(wraps=RedactionFilter.redact)
    monkeypatch.setattr(RedactionFilter, "redact", redact)
    save = Mock(wraps=session.save)
    monkeypatch.setattr(session, "save", save)
    result = h.run()
    save.assert_not_called()
    row = repo.get_session(result.session_id)
    assert result.status == status and row.state == "aborted" and row.finished_at is not None
    assert repo.get_intents(row.id) == []
    saved = repo.get_intent_by_request_id("old-pending-buy")
    assert saved.status == "filled_unannotated" and saved.resolved_at is None
    assert result.tp_failed and result.halt_set and result.halt_failure is None
    assert repo.get_lot_annotation("old-position") is None
    assert repo.list_tp_clears("old-position") == []
    h.client.clear_take_profit.assert_not_called()
    assert row.outcome == result.reason
    assert planted not in row.outcome
    assert planted not in result.reason
    assert all(planted not in call.args[0] for call in h.notifier.send.call_args_list)
    if sanitizer_fails:
        assert "recovery followups 1" in result.reason
        assert "old-position" not in result.reason and "annotation" not in result.reason
        assert ("EtoroNetworkError" if status == "market_data_failed" else "RuntimeError") in result.reason
    else:
        assert "***REDACTED***" in result.reason
        assert "recovery followups 1" in result.reason
        assert any("annotation ***REDACTED***" in call.args[0] for call in redact.call_args_list)
