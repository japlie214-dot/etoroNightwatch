"""T069: market observations survive execution vetoes without simulated money."""

from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock

import pytest

from nightwatch.db.repo import Bucket, Repo
from nightwatch.engine import session
from nightwatch.engine.close_executor import CloseRecoveryReport
from nightwatch.engine.executor import ExecutionReport, RecoveryReport
from nightwatch.etoro.models import Balance
from nightwatch.strategy.ladder import LadderIntent, decide as real_decide
from .test_session_armed import BUY, armed, filled
from .test_session_decide import config, no_network
from .test_session_market import DAY, FakeTiingo, assembled_signal


def run_day(armed, repo, host, monkeypatch, day):
    monkeypatch.setattr(host, "now", lambda: session.session_close_datetime(day) - timedelta(minutes=30))
    monkeypatch.setattr(session, "gate_10_price_data", lambda **kw: session.GateResult(10, "price", True, "offline"))
    return session._run_session_core(host=host, config=armed.config, repo=repo,
                                    client=armed.client, tiingo=FakeTiingo(),
                                    trading_date=day, wait=False)


def test_vetoed_real_ath_does_not_repeat_cash_to_core_next_session(armed, repo, host, monkeypatch):
    repo.save_strategy_state(replace(repo.get_strategy_state(), ath=100, bars=8,
                                    budget_year=DAY.year, budget=400, order_size=100,
                                    cash_target=100))
    armed.config = replace(armed.config, risk=replace(armed.config.risk, max_notional_per_session_usd=100))
    monkeypatch.setattr(armed.client, "get_balance", lambda: Balance(1000, None, 1000))
    sig = assembled_signal()
    sig.spy_context = replace(sig.spy_context, ath=110, close=110, drawdown=0, tier=0)
    monkeypatch.setattr(session, "assemble_spy_signal", lambda *a, **kw: sig)
    decisions = []

    def decide(*args):
        d = real_decide(*args)
        decisions.append(d)
        return d

    monkeypatch.setattr(session, "decide", decide)
    first = run_day(armed, repo, host, monkeypatch, DAY)
    assert "strategy state not saved: limits dropped intents" in first.reason
    assert decisions[0].diagnostics["new_ath_bar"]
    assert sum(i.amount for i in decisions[0].intents if i.reason == "cash to core") == 859.72
    assert repo.get_strategy_state().ath == 110
    # Reopen SQLite as a new process/session would; no in-memory state carries over.
    path = repo.conn.execute("PRAGMA database_list").fetchone()[2]
    with Repo.open(path) as restarted:
        armed.recover.side_effect = lambda **kw: armed.recovery
        second = run_day(armed, restarted, host, monkeypatch, DAY + timedelta(days=3))
        assert second.status == "executed", second.reason
        assert restarted.get_strategy_state().bars == 10
    assert not decisions[1].diagnostics["new_ath_bar"]
    assert not any(i.reason == "cash to core" for i in decisions[1].intents)
    assert armed.execute.call_count == 1


@pytest.mark.parametrize("veto", ["cap", "stopped", "deferred", "rejected", "partial",
                                   "exception", "tp", "unconfirmed", "recovery_tp",
                                   "recovery_close", "unsupported", "duplicate_sell", "persistence"])
def test_each_veto_keeps_money_but_advances_market_facts(armed, repo, monkeypatch, veto):
    before = replace(repo.get_strategy_state(), bars=8, ath=100, below_streak=2,
                     recyclable_cash=123.45, budget_year=2025, budget=400,
                     order_size=100, cash_target=800, sale_batch_sequence=4,
                     sale_batch_date="2025-12-30")
    repo.save_strategy_state(before)
    armed.decision.new_state = replace(session.load(repo), bars=9, ath=110, below_streak=3,
                                      recyclable_cash=0, budget_year=2026, budget=800,
                                      order_size=200, cash_target=1600,
                                      sale_batch_sequence=5, sale_batch_date=DAY.isoformat())
    if veto == "cap":
        armed.config = replace(armed.config, risk=replace(armed.config.risk, max_notional_per_session_usd=1))
    elif veto == "exception":
        armed.execute.side_effect = RuntimeError("offline execution failure")
    elif veto == "recovery_tp":
        armed.recovery = RecoveryReport((), (), (), ("p1",), ())
    elif veto == "recovery_close":
        armed.recover_closes.side_effect = None
        armed.recover_closes.return_value = CloseRecoveryReport((), ("u",), (), True)
    elif veto == "unsupported":
        armed.decision.intents = [LadderIntent("UPRO", "open", amount=100)]
    elif veto == "duplicate_sell":
        armed.decision.intents = [LadderIntent("SCHG", "close", amount=100)] * 2
    elif veto == "persistence":
        monkeypatch.setattr(session, "reconcile_effects", Mock(side_effect=RuntimeError("offline persistence failure")))
    else:
        result = filled(outcome="rejected") if veto == "rejected" else filled(
            detail="filled; partial fill" if veto == "partial" else "filled",
            position_id=None if veto == "unconfirmed" else "p1")
        armed.report = ExecutionReport((result,), "deadline" if veto == "stopped" else None,
                                       (BUY,) if veto == "deferred" else (),
                                       ("p1",) if veto == "tp" else (), ())
    outcome = armed.run()
    assert "strategy state not saved:" in outcome.reason
    armed.save.assert_not_called()  # Full money-state checkpoint remains vetoed.
    after = repo.get_strategy_state()
    assert after == replace(before, bars=9, ath=110, below_streak=3, updated_at=after.updated_at)


def test_bucket_expires_after_repeated_cap_vetoes_without_advancing_pending_fill(armed, repo, host, monkeypatch):
    repo.save_strategy_state(replace(repo.get_strategy_state(), bars=8, ath=110,
                                    budget_year=DAY.year, budget=1000, order_size=100, cash_target=100))
    repo.upsert_bucket(Bucket("expiry", "2026-09-01", 120, 300, 100, 8, last_reference_spy=120))
    repo.upsert_bucket(Bucket("third", "2026-09-01", 120, 300, 100, 9,
                              tranches_bought=2, last_reference_spy=120))
    armed.config = replace(armed.config,
                           strategy=replace(armed.config.strategy, bucket_expiry_bars=2),
                           risk=replace(armed.config.risk, max_notional_per_session_usd=1))
    sig = assembled_signal()
    sig.cone_5_z = -3
    monkeypatch.setattr(session, "assemble_spy_signal", lambda *a, **kw: sig)
    monkeypatch.setattr(session, "decide", real_decide)
    for index, day in enumerate((DAY, DAY + timedelta(days=3)), start=1):
        result = run_day(armed, repo, host, monkeypatch, day)
        assert "strategy state not saved: limits dropped intents" in result.reason
        row = repo.get_strategy_state()
        assert row.bars == 8 + index and row.below_streak == index
        expiring = repo.get_bucket("expiry")
        assert expiring.is_active == (index == 1)
        assert expiring.expired_at == (day.isoformat() if index == 2 else None)
        assert expiring.tranches_bought == 0 and expiring.last_reference_spy == 120
        pending = repo.get_bucket("third")
        assert pending.is_active and pending.expired_at is None
        assert pending.tranches_bought == 2 and pending.last_reference_spy == 120
    armed.execute.assert_not_called()


@pytest.mark.parametrize("veto", [False, True])
def test_market_checkpoint_failure_after_fill_preserves_evidence_and_money_handling(armed, repo, monkeypatch, caplog, veto):
    from nightwatch.engine.executor import execute_buys
    from .test_executor import FakeExecClient, _position
    from .test_session_market import IDS

    repo.save_strategy_state(replace(repo.get_strategy_state(), recyclable_cash=123))
    repo.upsert_bucket(Bucket("expiry", "2026-09-01", 120, 300, 100, 0, last_reference_spy=120))
    armed.decision.new_state = replace(armed.decision.new_state, bars=20, ath=110, recyclable_cash=23)
    armed.decision.intents = [BUY, BUY] if veto else [BUY]
    armed.config = replace(armed.config, risk=replace(armed.config.risk, max_notional_per_session_usd=100))
    fake = FakeExecClient(repo)
    fake.portfolio_queue = [[replace(_position(amount=100, is_no_take_profit=True), instrument_id=IDS["SCHG"])]]
    armed.execute.side_effect = lambda **kw: execute_buys(**{**kw, "client": fake, "sleep": lambda _: None})
    repo.conn.execute("CREATE TEMP TRIGGER refuse_expiry BEFORE UPDATE ON buckets "
                      "BEGIN SELECT RAISE(ABORT, 'offline expiry failure'); END")
    outcome = armed.run()
    assert outcome.status == "executed_partial"
    assert "market facts persistence failed: IntegrityError: offline expiry failure" in outcome.reason
    assert "market facts saved" not in outcome.reason
    assert "market facts persistence failed" in caplog.text
    assert "market facts persistence failed" in session.format_session_report(
        outcome, trading_date=DAY, environment="demo", next_run_at="next")
    assert repo.get_session(outcome.session_id).outcome == outcome.reason
    intent, = repo.get_intents(outcome.session_id)
    assert intent.status == "filled" and intent.sent_at is not None
    assert repo.get_order_by_reference_id(intent.request_id).terminal_status == 3
    assert repo.get_lot_annotation("P1") is not None
    assert len(fake.buy_calls) == 1 and fake.violations == []
    assert repo.get_bucket("expiry").is_active
    assert repo.get_bucket("expiry").expired_at is None
    state = repo.get_strategy_state()
    assert state.recyclable_cash == (123 if veto else 23)
    assert state.bars == (0 if veto else 20)
    assert state.ath == (None if veto else 110)
    assert armed.save.call_count == int(not veto)


@pytest.mark.parametrize("sent_before_crash", [False, True])
def test_hard_crash_retries_new_ath_sweep_from_remaining_real_cash(armed, repo, host, monkeypatch, sent_before_crash):
    from nightwatch.engine.executor import execute_buys
    from .test_executor import FakeExecClient, _position
    from .test_session_market import IDS

    repo.save_strategy_state(replace(repo.get_strategy_state(), ath=100, bars=8,
                                    budget_year=DAY.year, budget=400, order_size=100,
                                    cash_target=100))
    armed.config = replace(armed.config, risk=replace(armed.config.risk, max_notional_per_session_usd=100))
    cash = [1000]
    monkeypatch.setattr(armed.client, "get_balance", lambda: Balance(cash[0], None, cash[0]))
    sig = assembled_signal()
    sig.spy_context = replace(sig.spy_context, ath=110, close=110, drawdown=0, tier=0)
    monkeypatch.setattr(session, "assemble_spy_signal", lambda *a, **kw: sig)
    decisions = []

    def decide(*args):
        d = real_decide(*args)
        decisions.append(d)
        return d

    monkeypatch.setattr(session, "decide", decide)
    fake = FakeExecClient(repo)
    position = replace(_position(amount=100, is_no_take_profit=True), instrument_id=IDS["SCHG"])
    fake.portfolio_queue = [[position]]

    def crash(**kw):
        assert repo.get_strategy_state().ath == 100
        assert repo.get_intents(kw["session_id"]) == []
        if sent_before_crash:
            report = execute_buys(**{**kw, "client": fake, "sleep": lambda _: None})
            assert report.results[0].outcome == "filled"
            cash[0] -= 100
            monkeypatch.setattr(armed.client, "get_portfolio", lambda: [position])
        raise SystemExit("hard process death")

    armed.execute.side_effect = crash
    with pytest.raises(SystemExit, match="hard process death"):
        run_day(armed, repo, host, monkeypatch, DAY)
    assert repo.get_strategy_state().ath == 100 and repo.get_strategy_state().bars == 8
    previous = repo.get_session_by_date(DAY.isoformat())
    assert len(repo.get_intents(previous.id)) == int(sent_before_crash)
    assert len(fake.buy_calls) == int(sent_before_crash)
    assert sum(i.amount for i in decisions[0].intents if i.reason == "cash to core") == 859.72

    armed.execute.side_effect = lambda **kw: ExecutionReport(
        tuple(filled(i) for i in kw["intents"]), None, (), (), ())
    path = repo.conn.execute("PRAGMA database_list").fetchone()[2]
    with Repo.open(path) as restarted:
        armed.recover.side_effect = lambda **kw: armed.recovery
        result = run_day(armed, restarted, host, monkeypatch, DAY + timedelta(days=3))
        assert result.status == "executed", result.reason
        assert restarted.get_session(previous.id).outcome == "crashed_recovered"
        assert restarted.get_strategy_state().ath == 110
        assert restarted.get_strategy_state().bars == 9
    assert decisions[1].diagnostics["new_ath_bar"]
    amount = sum(i.amount for i in decisions[1].intents if i.reason == "cash to core")
    assert amount == (759.72 if sent_before_crash else 859.72)
    assert cash[0] == (900 if sent_before_crash else 1000)


def test_full_save_failure_without_sends_leaves_market_facts_for_next_session(armed, repo, host, monkeypatch):
    repo.save_strategy_state(replace(repo.get_strategy_state(), ath=100, bars=8,
                                    budget_year=DAY.year, budget=400, order_size=100,
                                    cash_target=100))
    before = repo.get_strategy_state()
    armed.config = replace(armed.config, risk=replace(armed.config.risk, max_notional_per_session_usd=2000))
    monkeypatch.setattr(armed.client, "get_balance", lambda: Balance(1000, None, 1000))
    send = Mock(side_effect=AssertionError("offline test must not send orders"))
    monkeypatch.setattr(armed.client, "place_buy_order", send, raising=False)
    sig = assembled_signal()
    sig.spy_context = replace(sig.spy_context, ath=110, close=110, drawdown=0, tier=0)
    monkeypatch.setattr(session, "assemble_spy_signal", lambda *a, **kw: sig)
    decisions = []

    def decide(*args):
        d = real_decide(*args)
        decisions.append(d)
        return d

    monkeypatch.setattr(session, "decide", decide)
    # The orchestration fixture returns reports without sends or intent rows.
    repo.conn.execute("CREATE TEMP TRIGGER refuse_full_save BEFORE UPDATE OF budget ON strategy_state "
                      "BEGIN SELECT RAISE(ABORT, 'offline full save failure'); END")
    first = run_day(armed, repo, host, monkeypatch, DAY)
    assert first.status == "aborted_before_orders"
    assert "offline full save failure" in first.reason
    assert decisions[0].intents and decisions[0].diagnostics["new_ath_bar"]
    assert sum(i.amount for i in decisions[0].intents if i.reason == "cash to core") == 859.72
    assert armed.execute.call_count == 1 and armed.save.call_count == 1
    assert repo.get_intents(first.session_id) == []
    send.assert_not_called()
    assert repo.get_strategy_state() == before
    assert not repo.conn.in_transaction

    repo.conn.execute("DROP TRIGGER refuse_full_save")
    second = run_day(armed, repo, host, monkeypatch, DAY + timedelta(days=3))
    assert second.status == "executed", second.reason
    assert decisions[1].diagnostics["new_ath_bar"]
    assert sum(i.amount for i in decisions[1].intents if i.reason == "cash to core") == 859.72
    assert repo.get_strategy_state().ath == 110 and repo.get_strategy_state().bars == 9
    assert repo.get_intents(second.session_id) == []
    send.assert_not_called()
