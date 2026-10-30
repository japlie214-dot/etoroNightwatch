"""T038 orchestration with real limits/ledger, plus a real-ladder sale day."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nightwatch.config import TradingMode
from nightwatch.db.repo import Bucket, Instrument, Intent
from nightwatch.engine import session
from nightwatch.engine.close_executor import CloseOutcome, CloseReport, CloseRecoveryReport
from nightwatch.engine.executor import ExecutedBuy, ExecutionReport
from nightwatch.strategy.ladder import LadderIntent, ReentryBucket
from nightwatch.etoro.models import Balance
from .test_session_armed import armed, BUY, filled
from .test_session_decide import annotation, config, no_network, position
from .test_session_market import DAY, IDS, FakeTiingo, assembled_signal
from .test_sale_effects import confirm, decision, reentry, sale


CLOSE = LadderIntent("UPRO", "close", position_ref="u", book="catapult")
UPRO = LadderIntent("UPRO", "open", amount=100, book="catapult")
REENTRY = LadderIntent("UPRO", "open", amount=100, book="reentry", bucket_id="b")


@pytest.fixture
def two(armed, repo, monkeypatch):
    e = armed
    e.positions = [position("u")]
    repo.insert_lot_annotation(annotation("u"))
    for symbol, iid in IDS.items():
        repo.upsert_instrument(Instrument(symbol, iid))
    e.config = replace(e.config, risk=replace(e.config.risk, max_orders_per_session=10))
    e.decision.intents = [CLOSE, BUY, UPRO]
    e.decision.new_state = replace(e.decision.new_state, recyclable_cash=200,
                                  sale_batch_sequence=1, sale_batch_date=DAY.isoformat())
    e.outcomes = ["confirmed"]

    def portfolio():
        e.calls.append("portfolio")
        return list(e.positions)
    monkeypatch.setattr(e.client, "get_portfolio", portfolio)
    def strict():
        e.calls.append("strict")
        return list(e.positions)
    e.strict = Mock(side_effect=strict)
    monkeypatch.setattr(e.client, "get_portfolio_strict", e.strict, raising=False)
    e.client.close_position = Mock(return_value="close-order")
    e.client.open_position = Mock(side_effect=AssertionError("orchestration uses executor fake"))

    def closes(**kwargs):
        e.calls.append("closes")
        assert kwargs["positions_by_id"] == {p.position_id: p for p in e.positions}
        # The actual ledger, on another connection, must already contain every effect.
        from nightwatch.db.repo import connect
        with connect(repo.conn.execute("PRAGMA database_list").fetchone()[2]) as conn:
            assert conn.execute("SELECT COUNT(*) FROM sale_effects WHERE session_id = ?",
                                (kwargs["session_id"],)).fetchone()[0] == len(kwargs["intents"])
        results = []
        for intent, outcome in zip(kwargs["intents"], e.outcomes):
            sid = kwargs["session_id"]
            seq = repo.next_intent_sequence(sid)
            iid = repo.insert_intent(Intent(sid, f"close-{sid}-{seq}", seq, "UPRO", "close",
                                            position_id=intent.position_ref, status="sent"))
            e.client.close_position(f"close-{sid}-{seq}", intent.position_ref, IDS["UPRO"])
            if outcome == "confirmed":
                confirm(repo, iid, intent.position_ref)
                obs = repo.list_close_observations(intent.position_ref)[-1]
                repo.apply_confirmed_close(obs["id"])
                e.positions = [p for p in e.positions if p.position_id != intent.position_ref]
            results.append(CloseOutcome(intent, iid, "request", None, "order", intent.position_ref,
                                        outcome, "offline close"))
        blocks = any(r.outcome != "confirmed" for r in results)
        return CloseReport(tuple(results), None, (), blocks, ())
    e.closes = Mock(side_effect=closes)
    monkeypatch.setattr(session, "execute_closes", e.closes)
    return e


def bucket(repo, tranches=0):
    repo.upsert_bucket(Bucket("b", "2026-09-01", 110, 300, 100, 0,
                              tranches_bought=tranches, last_reference_spy=110))


def new_bucket(e):
    b = ReentryBucket("batch", DAY.isoformat(), 100, 200, 66.66, 1,
                      last_reference_spy=100)
    e.decision.updated_buckets = [b]
    e.decision.new_state = replace(e.decision.new_state, recyclable_cash=0)
    return b


def test_close_then_fresh_cash_then_buys_checkpoint_final_snapshot(two, repo):
    # SCHG + sale is intentionally an orchestration fake, not ladder strategy proof.
    result = two.run()
    assert result.status == "executed"
    assert two.calls.index("strict") < two.calls.index("closes")
    close_index = two.calls.index("closes")
    assert close_index < two.calls.index("save") < two.calls.index("execute")
    assert two.calls[close_index:].index("balance") < two.calls[close_index:].index("execute")
    assert two.calls[-1] == "snapshot"
    assert two.save.call_count == 2
    assert two.save.call_args_list[0].args[1].bars == 0
    assert two.save.call_args_list[0].args[1].recyclable_cash == 200
    assert repo.get_strategy_state().bars == 1
    assert two.execute.call_args.kwargs["intents"] == [BUY, UPRO]
    assert repo.get_decision(result.session_id).phase == "single"


def test_new_ath_bucket_applied_before_buys_once(two, repo):
    new_bucket(two)
    def execute(**kwargs):
        assert len(repo.active_buckets()) == 1
        assert repo.get_bucket("batch").proceeds == 200
        assert repo.get_strategy_state().recyclable_cash == 0
        two.calls.append("execute")
        return ExecutionReport(tuple(filled(i) for i in kwargs["intents"]), None, (), (), ())
    two.execute.side_effect = execute
    assert two.run().status == "executed"
    assert len(repo.active_buckets()) == 1
    assert repo.get_strategy_state().recyclable_cash == 0


def test_recyclable_checkpoint_exact_cents_and_final_consumption(two, repo):
    # 100 * 1.234567 = 123.4567 -> 123.45; + 10.01 - 100.00 = 33.46.
    two.positions = [replace(two.positions[0], units=1.234567)]
    repo.save_strategy_state(replace(repo.get_strategy_state(), recyclable_cash=10.01))
    two.decision.new_state = replace(two.decision.new_state, recyclable_cash=33.46)
    two.decision.intents = [CLOSE, UPRO]
    assert two.run().status == "executed"
    assert two.save.call_args_list[0].args[1].recyclable_cash == 133.46
    assert repo.get_strategy_state().recyclable_cash == 33.46


@pytest.mark.parametrize("outcome", ["unresolved", "unverified", "rejected"])
def test_nonconfirmed_close_blocks_buys_and_no_save(two, repo, host, outcome):
    two.outcomes = [outcome]
    result = two.run()
    assert result.status == "executed_partial"
    two.execute.assert_not_called()
    two.save.assert_not_called()
    assert repo.get_strategy_state().recyclable_cash == 0
    assert host.halt_flag_path().exists() == (outcome == "unverified")
    if outcome == "unverified":
        assert "close not verified for position(s) u" in host.halt_flag_path().read_text()
    assert "snapshot" in two.calls


def test_strict_read_failure_sends_nothing_no_halt(two, host):
    two.strict.side_effect = RuntimeError("strict unavailable")
    result = two.run()
    assert result.status == "halted_before_execution"
    assert "close snapshot failed" in result.reason
    two.closes.assert_not_called()
    two.execute.assert_not_called()
    two.save.assert_not_called()
    assert not host.halt_flag_path().exists()
    assert "snapshot" in two.calls


@pytest.mark.parametrize("halt_fails", [False, True])
def test_close_raise_after_send_still_snapshots_and_alerts(two, repo, host, monkeypatch, halt_fails):
    def after_send(**kwargs):
        two.client.close_position("request", "u", IDS["UPRO"])
        raise RuntimeError("after send")
    two.closes.side_effect = after_send
    if halt_fails:
        monkeypatch.setattr(session, "write_halt", Mock(side_effect=OSError("disk full")))
    from nightwatch.notify import poll
    monkeypatch.setattr(poll, "process_pending", lambda *args: None)
    notifier = SimpleNamespace(send=Mock())
    result = session.run_session(host=host, config=two.config, repo=repo, client=two.client,
                                 tiingo=FakeTiingo(), trading_date=DAY, wait=False, notifier=notifier)
    assert result.status == "executed_partial"
    assert "close executor error: RuntimeError: after send" in result.reason
    two.execute.assert_not_called()
    assert two.calls.count("balance") == 2 and "snapshot" in two.calls
    assert repo.get_session(result.session_id).state == "finished"
    alert = [c for c in notifier.send.call_args_list if c.kwargs["event"] == "execution_halt"]
    assert len(alert) == 1
    assert ("Next session is NOT protected." in alert[0].args[0]) == halt_fails
    if not halt_fails:
        assert "close executor error" in host.halt_flag_path().read_text()


def test_recovery_close_blocks_both_executors_without_halt(two, host):
    two.recover_closes.return_value = CloseRecoveryReport((), ("u",), (), True)
    two.recover_closes.side_effect = None
    result = two.run()
    assert result.status == "halted_before_execution"
    assert "execution skipped: unresolved close at recovery" in result.reason
    two.closes.assert_not_called()
    two.execute.assert_not_called()
    two.save.assert_not_called()
    assert not host.halt_flag_path().exists()


def test_recovery_tp_skip_precedes_closes(two):
    from nightwatch.engine.executor import RecoveryReport
    two.recovery = RecoveryReport((), (), (), ("prior-position",), ())
    assert two.run().status == "halted_before_execution"
    two.closes.assert_not_called()
    two.execute.assert_not_called()
    two.save.assert_not_called()


@pytest.mark.parametrize("condition", ["stopped", "deferred", "missing_result"])
def test_close_report_incomplete_never_continues_to_buys(two, condition):
    original = two.closes.side_effect
    def incomplete(**kwargs):
        report = original(**kwargs)
        if condition == "stopped":
            return replace(report, stopped="deadline")
        if condition == "deferred":
            return replace(report, deferred=(CLOSE,))
        return replace(report, results=())
    two.closes.side_effect = incomplete
    assert two.run().status == "executed_partial"
    two.execute.assert_not_called()


def test_recovery_close_raise_fails_gate_11_before_buy_recovery(two):
    two.recover_closes.side_effect = RuntimeError("close recovery error")
    result = two.run()
    assert result.status == "preflight_failed" and result.failed_gate == 11
    two.recover.assert_not_called()
    two.closes.assert_not_called()
    two.execute.assert_not_called()


@pytest.mark.parametrize("bad", [LadderIntent("SCHG", "close", position_ref="s"),
                                  replace(BUY, book="unknown"), replace(BUY, book="reentry"),
                                  replace(UPRO, book=None), replace(REENTRY, bucket_id=""),
                                  replace(CLOSE, amount=5), replace(BUY, amount=float("nan"))])
def test_whole_decision_refused_before_either_broker_write(two, bad):
    two.decision.intents = [CLOSE, bad]
    assert two.run().status == "armed_refused_intents"
    two.closes.assert_not_called()
    two.execute.assert_not_called()
    two.client.close_position.assert_not_called()
    two.client.open_position.assert_not_called()


def test_confirmed_sale_checkpoint_survives_buy_failure(two, repo):
    two.report = ExecutionReport((filled(outcome="rejected"),), None, (), (), ())
    result = two.run()
    assert result.status == "executed"
    assert "strategy state not saved: non-filled result" in result.reason
    assert two.save.call_count == 1
    assert repo.get_strategy_state().bars == 0
    assert repo.get_strategy_state().recyclable_cash == 200
    session.reconcile_effects(repo)
    assert repo.get_strategy_state().recyclable_cash == 200


@pytest.mark.parametrize("tranches", [0, 2])
def test_reentry_filled_with_other_buy_failed_is_applied_without_final_save(two, repo, tranches):
    bucket(repo, tranches)
    two.decision.intents = [REENTRY, BUY]
    two.decision.updated_buckets = [] if tranches == 2 else [
        ReentryBucket("b", "2026-09-01", 110, 300, 100, 0, tranches + 1, 100)]
    def execute(**kwargs):
        two.calls.append("execute")
        iid = reentry(repo, kwargs["session_id"])
        return ExecutionReport((filled(REENTRY, intent_id=iid), filled(BUY, outcome="rejected")),
                               None, (), (), ())
    two.execute.side_effect = execute
    result = two.run()
    assert "strategy state not saved: non-filled result" in result.reason
    two.save.assert_not_called()
    assert repo.get_bucket("b").tranches_bought == tranches + 1
    assert repo.get_bucket("b").is_active == (tranches == 0)
    session.reconcile_effects(repo)
    assert repo.get_bucket("b").tranches_bought == tranches + 1


@pytest.mark.parametrize("complete", [False, True])
def test_expired_bucket_deactivated_only_on_final_save(two, repo, complete):
    bucket(repo)
    two.decision.intents = [BUY]
    if not complete:
        two.report = ExecutionReport((filled(outcome="rejected"),), None, (), (), ())
    assert two.run().status == "executed"
    b = repo.get_bucket("b")
    assert b.is_active == (not complete)
    assert b.expired_at == (DAY.isoformat() if complete else None)


def test_limits_receive_fresh_sleeve_cash_and_live_lot_count(two, repo, monkeypatch):
    limits = Mock(wraps=session.apply_submission_limits)
    monkeypatch.setattr(session, "apply_submission_limits", limits)
    two.config = replace(two.config, strategy=replace(two.config.strategy, cash_reserve_usd=50))
    two.run()
    assert limits.call_args.kwargs["cash_usd"] == 850
    assert limits.call_args.kwargs["open_lots"] == 0
    assert limits.call_args.args == ([BUY, UPRO],)


def test_no_closes_is_original_buy_path(two):
    two.decision.intents = [BUY]
    two.decision.new_state = replace(two.decision.new_state, recyclable_cash=0)
    assert two.run().status == "executed"
    two.strict.assert_not_called()
    two.closes.assert_not_called()
    assert two.calls.index("recover_closes") < two.calls.index("recover")
    assert two.save.call_count == 1


def test_dry_run_skips_all_recovery_closes_and_persistence(two, repo):
    two.config = replace(two.config, mode=TradingMode.DRY_RUN, arm_flag_present=False)
    assert two.run().status == "would_have_done"
    two.recover_closes.assert_not_called()
    two.recover.assert_not_called()
    two.closes.assert_not_called()
    two.execute.assert_not_called()
    two.save.assert_not_called()
    assert repo.conn.execute("SELECT COUNT(*) FROM sale_effects").fetchone()[0] == 0


def test_trailing_armed_refused_without_any_calls(two):
    two.config = replace(two.config, strategy=replace(two.config.strategy, exit_mode="trailing"))
    result = two.run()
    assert result.status == "refused_armed"
    assert "armed sessions support fixed exits only" in result.reason
    assert two.calls == []
    two.client.close_position.assert_not_called()
    two.client.open_position.assert_not_called()


def test_sale_insert_failure_means_zero_sends(two, repo, monkeypatch, host):
    monkeypatch.setattr(repo, "insert_sale_effects", Mock(side_effect=RuntimeError("insert failed")))
    result = two.run()
    assert result.status == "halted_before_execution"
    two.closes.assert_not_called()
    two.execute.assert_not_called()
    two.client.close_position.assert_not_called()
    assert not host.halt_flag_path().exists()


@pytest.mark.parametrize("also_snapshot_fails", [False, True])
def test_between_phase_balance_failure_skips_buys_and_still_snapshots(two, host, repo, monkeypatch, also_snapshot_fails):
    count = 0
    def balance():
        nonlocal count
        count += 1
        if count == 2 or (also_snapshot_fails and count == 3):
            raise RuntimeError("cash unavailable")
        return Balance(1000, None, 1000)
    monkeypatch.setattr(two.client, "get_balance", balance)
    result = two.run()
    assert result.status == "executed_partial"
    assert "between-phase balance failed" in result.reason
    two.execute.assert_not_called()
    assert not host.halt_flag_path().exists()
    assert "snapshot" in two.calls
    assert repo.latest_snapshot().available_cash == (None if also_snapshot_fails else 1000)


@pytest.mark.parametrize("where", ["sale", "bucket", "reconcile"])
def test_application_failure_halts_and_snapshots(two, repo, host, monkeypatch, where):
    if where == "sale":
        monkeypatch.setattr(repo, "apply_sale_effect", Mock(side_effect=RuntimeError("apply failed")))
    elif where == "reconcile":
        monkeypatch.setattr(session, "reconcile_effects", Mock(side_effect=RuntimeError("apply failed")))
    else:
        bucket(repo)
        two.decision.intents = [REENTRY, BUY]
        two.report = ExecutionReport((filled(REENTRY), filled(BUY)), None, (), (), ())
        monkeypatch.setattr(repo, "apply_bucket_fill", Mock(side_effect=RuntimeError("apply failed")))
    result = two.run()
    assert result.status == "executed_partial"
    assert "sale-effect persistence failed" in host.halt_flag_path().read_text()
    if where != "bucket":
        two.execute.assert_not_called()
    assert "snapshot" in two.calls
    assert repo.get_strategy_state().bars == 0


def test_confirmed_prefix_applies_even_when_second_close_unresolved(two, repo):
    two.positions.append(position("v", units=3))
    repo.insert_lot_annotation(annotation("v"))
    two.decision.intents = [CLOSE, replace(CLOSE, position_ref="v"), BUY]
    two.outcomes = ["confirmed", "unresolved"]
    result = two.run()
    assert result.status == "executed_partial"
    assert repo.get_strategy_state().recyclable_cash == 200
    assert repo.get_strategy_state().bars == 0
    two.execute.assert_not_called()
    pending = repo.get_intents(result.session_id)[-1]
    confirm(repo, pending.id, "v")
    session.reconcile_effects(repo)
    session.reconcile_effects(repo)
    assert repo.get_strategy_state().recyclable_cash == 500


def test_recovery_fill_advances_before_day_two_decision_and_not_rebought(two, repo, monkeypatch):
    bucket(repo)
    old = repo.create_session("2026-10-01", "live", "demo", True)
    decision(repo, old.id, spy=100)
    iid = reentry(repo, old.id, status="sent")
    def recovery(**kwargs):
        two.calls.append("recover")
        repo.set_intent_status(iid, "filled", resolved=True)
        return two.recovery
    two.recover.side_effect = recovery
    real_decide = __import__("nightwatch.strategy.ladder", fromlist=["decide"]).decide
    def decide(inputs, state, cfg):
        assert inputs.buckets[0].tranches_bought == 1
        assert inputs.buckets[0].last_reference_spy == inputs.spy.close == 100
        d = real_decide(inputs, state, cfg)
        assert not any(i.bucket_id == "b" for i in d.intents)
        return d
    monkeypatch.setattr(session, "decide", decide)
    result = two.run()
    assert result.status == "executed"
    assert repo.get_bucket("b").tranches_bought == 1


def test_buy_executor_raise_after_earlier_fill_reconciles_on_restart(two, repo, monkeypatch):
    bucket(repo)
    two.decision.intents = [REENTRY, BUY]
    def execute(**kwargs):
        reentry(repo, kwargs["session_id"])
        raise RuntimeError("after earlier fill")
    two.execute.side_effect = execute
    result = two.run()
    assert result.status == "executed"  # inherited T030 buy exception rule
    assert repo.get_bucket("b").tranches_bought == 0
    session.reconcile_effects(repo)
    session.reconcile_effects(repo)
    assert repo.get_bucket("b").tranches_bought == 1


def test_real_decide_new_ath_sale_waits_for_next_eligible_session(two, repo, monkeypatch, host):
    from nightwatch.engine.close_executor import execute_closes
    from nightwatch.etoro.client import PollResult
    from nightwatch.etoro.models import OrderResult, OrderStatus
    from nightwatch.strategy.ladder import decide as real_decide
    # Existing lot profitable at 100 with old entry ATH=110; signal ATH=120.
    repo.save_strategy_state(replace(repo.get_strategy_state(), ath=110, bars=8,
                                    budget_year=DAY.year, budget=1000, order_size=100, cash_target=0))
    sig = assembled_signal()
    sig.spy_context = replace(sig.spy_context, ath=120, close=115)
    sig.cone_5_z = 4
    monkeypatch.setattr(session, "assemble_spy_signal", lambda *args, **kwargs: sig)
    decide_spy = Mock(wraps=real_decide)
    monkeypatch.setattr(session, "decide", decide_spy)
    monkeypatch.setattr(session, "execute_closes", execute_closes)
    monkeypatch.setattr(two.client, "get_balance", lambda: Balance(200, None, 0 if two.positions else 200))
    def close(*args):
        assert repo.conn.execute("SELECT COUNT(*) FROM sale_effects").fetchone()[0] == 1
        two.positions = []
        return "closed-u"
    two.client.close_position.side_effect = close
    two.client.poll_order_to_terminal = lambda **kwargs: PollResult(
        OrderResult("closed-u", None, OrderStatus.FILLED, position_id="u", raw={}), 1, False)
    result = two.run()
    assert result.status == "executed"
    two.client.close_position.assert_called_once()
    two.execute.assert_not_called()
    assert decide_spy.call_count == 1
    assert decide_spy.call_args.args[0].cash == 0
    b, = repo.active_buckets()
    assert b.proceeds == 200 and b.tranches_bought == 0
    # Next actual session is eligible after SPY falls; use the same real ladder.
    next_day = DAY + timedelta(days=3)
    sig.spy_context = replace(sig.spy_context, close=100)
    sig.cone_5_z = None
    monkeypatch.setattr(host, "now", lambda: session.session_close_datetime(next_day) - timedelta(minutes=30))
    monkeypatch.setattr(session, "gate_10_price_data", lambda **kwargs: session.GateResult(10, "price", True, "ok"))
    session.reconcile_effects(repo)
    def fill_reentry(**kwargs):
        intent, = kwargs["intents"]
        iid = reentry(repo, kwargs["session_id"], bucket_id=intent.bucket_id)
        return ExecutionReport((filled(intent, intent_id=iid, fill_amount_usd=intent.amount),), None, (), (), ())
    two.execute.side_effect = fill_reentry
    result2 = session._run_session_core(host=host, config=two.config, repo=repo,
                                        client=two.client, tiingo=FakeTiingo(), trading_date=next_day, wait=False)
    assert result2.status == "executed"
    assert decide_spy.call_count == 2
    sent = two.execute.call_args.kwargs["intents"]
    assert len(sent) == 1 and sent[0].book == "reentry" and sent[0].bucket_id == b.bucket_id
    assert repo.get_bucket(b.bucket_id).tranches_bought == 1
    session.reconcile_effects(repo)
    assert repo.get_bucket(b.bucket_id).tranches_bought == 1


@pytest.mark.parametrize("prior", ["rejected", "deferred", "never_sent"])
def test_next_day_close_retry_replaces_stale_effect_and_credits_once(two, repo, host, monkeypatch, prior):
    class BeforeSendCrash(BaseException):
        pass

    two.decision.intents = [CLOSE]
    original = two.closes.side_effect
    if prior == "rejected":
        two.outcomes = ["rejected"]
        first = two.run()
        assert first.status == "executed_partial"
        repo.set_intent_status(repo.get_intents(first.session_id)[0].id, "rejected", resolved=True)
    elif prior == "deferred":
        two.closes.side_effect = lambda **kwargs: CloseReport((), "deadline", tuple(kwargs["intents"]), True, ())
        assert two.run().status == "executed_partial"
    else:
        def crash(**kwargs):
            raise BeforeSendCrash()
        two.closes.side_effect = crash
        with pytest.raises(BeforeSendCrash):
            two.run()
    assert repo.get_strategy_state().recyclable_cash == 0
    row = repo.conn.execute("SELECT * FROM sale_effects WHERE position_id = 'u'").fetchone()
    assert row["gross_proceeds_cents"] == 20000 and row["applied_at"] is None
    old_sid = row["session_id"]
    assert two.client.close_position.call_count == (1 if prior == "rejected" else 0)
    assert not host.halt_flag_path().exists()

    # Retry at a new valuation: stale proceeds must not be credited on confirmation.
    two.positions = [replace(two.positions[0], units=3)]
    two.outcomes = ["confirmed"]
    two.closes.side_effect = original
    two.decision.new_state = replace(two.decision.new_state, recyclable_cash=300)
    next_day = DAY + timedelta(days=3)
    monkeypatch.setattr(host, "now", lambda: session.session_close_datetime(next_day) - timedelta(minutes=30))
    monkeypatch.setattr(session, "gate_10_price_data", lambda **kwargs: session.GateResult(10, "price", True, "ok"))
    result = session._run_session_core(host=host, config=two.config, repo=repo, client=two.client,
                                      tiingo=FakeTiingo(), trading_date=next_day, wait=False)
    assert result.status == "executed"
    assert two.client.close_position.call_count == (2 if prior == "rejected" else 1)
    assert repo.get_strategy_state().recyclable_cash == 300
    row = repo.conn.execute("SELECT * FROM sale_effects WHERE position_id = 'u'").fetchone()
    assert row["session_id"] == result.session_id != old_sid
    assert row["gross_proceeds_cents"] == 30000 and row["applied_at"] is not None
    assert repo.conn.execute("SELECT COUNT(*) FROM sale_effects").fetchone()[0] == 1
    session.reconcile_effects(repo)
    session.reconcile_effects(repo)
    assert repo.get_strategy_state().recyclable_cash == 300


@pytest.mark.parametrize("already_applied", [False, True])
def test_confirmed_position_is_applied_before_decide_and_retry_halts_without_sends(two, repo, monkeypatch, already_applied):
    old = repo.create_session("2026-10-01", "live", "demo", True)
    iid = sale(repo, old.id, "u", cents=12345)
    confirm(repo, iid, "u")
    if already_applied:
        assert repo.apply_sale_effect("u")
    two.decision.intents = [CLOSE]
    def decide(inputs, state, cfg):
        row = repo.conn.execute("SELECT * FROM sale_effects WHERE position_id = 'u'").fetchone()
        assert row["applied_at"] is not None
        assert row["session_id"] == old.id and row["gross_proceeds_cents"] == 12345
        assert state.recyclable_cash == 123.45
        return two.decision
    monkeypatch.setattr(session, "decide", decide)
    result = two.run()
    assert result.status == "halted_before_execution"
    two.closes.assert_not_called()
    two.execute.assert_not_called()
    two.client.close_position.assert_not_called()
    two.client.open_position.assert_not_called()
    assert repo.get_strategy_state().recyclable_cash == 123.45
    two.save.assert_not_called()


SELL = LadderIntent("SCHG", "close", amount=100, reason="fund ladder buy")


@pytest.fixture
def schg(two, repo, host, monkeypatch):
    from nightwatch.engine.close_executor import execute_schg_sell
    from nightwatch.etoro.models import OrderResult, OrderStatus

    e = two
    e.positions.append(position("s", "SCHG", units=4))
    repo.insert_lot_annotation(replace(annotation("s"), symbol="SCHG"))
    e.decision.intents = [CLOSE, SELL, BUY]
    e.clock = host.now()
    monkeypatch.setattr(host, "now", lambda: e.clock)
    e.schg_status = OrderStatus.FILLED

    def sleep(seconds):
        assert seconds > 0
        e.clock += timedelta(seconds=seconds)

    def close(request, pid, iid, deducted=None):
        e.calls.append("schg_send")
        p = next(p for p in e.positions if p.position_id == pid)
        e.positions = [p for p in e.positions if p.position_id != pid]
        if deducted is not None:
            e.positions.append(replace(p, units=p.units - deducted))
        e.last_schg = (pid, p.units if deducted is None else deducted, deducted is not None)
        return "schg-order"

    def lookup(order_id, **kwargs):
        pid, units, partial = e.last_schg
        return OrderResult(order_id, None, e.schg_status, position_id=pid, fill_units=units,
                           raw={"positions": ([{"positionID": "slice"}, {"positionID": pid}]
                                              if partial else [{"positionID": pid}])})

    e.client.close_position.side_effect = close
    e.client.lookup_close_order = Mock(side_effect=lookup)

    def execute(**kwargs):
        e.calls.append("schg")
        return execute_schg_sell(**kwargs, sleep=sleep)

    e.schg = Mock(side_effect=execute)
    monkeypatch.setattr(session, "execute_schg_sell", e.schg)
    return e


@pytest.mark.parametrize("upro", [False, True])
@pytest.mark.parametrize("price", [None, 80])
def test_schg_real_executor_sequence_cash_and_saves(schg, repo, monkeypatch, upro, price):
    if not upro:
        schg.decision.intents = [SELL, BUY]
        schg.decision.new_state = replace(schg.decision.new_state, recyclable_cash=0)
    build = session.build_ladder_inputs

    def inputs(**kwargs):
        value, notes = build(**kwargs)
        return replace(value, schg_price=price), notes

    monkeypatch.setattr(session, "build_ladder_inputs", inputs)
    limits = Mock(wraps=session.apply_submission_limits)
    monkeypatch.setattr(session, "apply_submission_limits", limits)
    result = schg.run()
    assert result.status == "executed"
    assert schg.closes.call_count == int(upro)
    if upro:
        assert schg.calls.index("closes") < schg.calls.index("save") < schg.calls.index("schg")
        assert schg.schg.call_args.kwargs["positions"] == [position("s", "SCHG", units=4)]
        assert schg.calls.index("closes") < schg.calls.index("strict", schg.calls.index("closes")) < schg.calls.index("schg")
    tail = schg.calls[schg.calls.index("schg"):]
    assert tail.index("schg_send") < tail.index("balance") < tail.index("execute") < tail.index("save")
    assert schg.schg.call_args.kwargs["price"] == (100 if price is None else price)
    assert schg.schg.call_args.kwargs["allow_partial"] is True
    assert schg.schg.call_args.kwargs["deadline"] == session.session_close_datetime(DAY) - timedelta(minutes=5)
    assert limits.call_args.kwargs["cash_usd"] == 900
    assert schg.save.call_count == 1 + int(upro)
    assert repo.get_strategy_state().bars == 1
    assert repo.get_strategy_state().recyclable_cash == (200 if upro else 0)
    assert repo.conn.execute("SELECT COUNT(*) FROM sale_effects").fetchone()[0] == int(upro)
    evidence, = repo.confirmed_partial_closes(["s"])
    assert evidence["status"] == "confirmed" and evidence["slice_position_id"] == "slice"
    assert schg.run().status == "already_ran"
    assert schg.schg.call_count == 1
    assert schg.client.close_position.call_count == 1 + int(upro)


@pytest.mark.parametrize("upro", [False, True])
@pytest.mark.parametrize("tp_failed", [False, True])
def test_schg_then_buy_tp_veto_preserves_close_evidence_and_bucket_fill(schg, repo, host, upro, tp_failed):
    bucket(repo)
    schg.decision.intents = ([CLOSE] if upro else []) + [SELL, BUY, REENTRY]
    schg.decision.new_state = replace(schg.decision.new_state, recyclable_cash=200 if upro else 0)

    def execute(**kwargs):
        schg.calls.append("execute")
        assert kwargs["intents"] == [BUY, REENTRY]
        iid = reentry(repo, kwargs["session_id"])
        return ExecutionReport((filled(BUY), filled(REENTRY, intent_id=iid)),
                               None, (), ("p1",) if tp_failed else (), ())

    schg.execute.side_effect = execute
    result = schg.run()
    assert result.status == "executed"
    assert schg.save.call_count == int(upro) + int(not tp_failed)
    assert repo.get_strategy_state().bars == int(not tp_failed)
    assert repo.get_strategy_state().recyclable_cash == (200 if upro else 0)
    if upro:
        checkpoint = schg.save.call_args_list[0].args[1]
        assert checkpoint.bars == 0 and checkpoint.sale_batch_sequence == 1
        assert checkpoint.sale_batch_date == DAY.isoformat()
        effect, = repo.conn.execute("SELECT * FROM sale_effects").fetchall()
        assert effect["position_id"] == "u" and effect["gross_proceeds_cents"] == 20000
        assert effect["applied_at"] is not None
        observation, = repo.list_close_observations("u")
        assert observation["observed_status"] == "confirmed_full_close"
    else:
        assert repo.conn.execute("SELECT COUNT(*) FROM sale_effects").fetchone()[0] == 0
    closes = [i for i in repo.get_intents(result.session_id) if i.side == "close"]
    assert len(closes) == 1 + int(upro) and all(i.status == "filled" for i in closes)
    evidence, = repo.confirmed_partial_closes(["s"])
    assert evidence["status"] == "confirmed" and evidence["slice_position_id"] == "slice"
    assert repo.get_bucket("b").tranches_bought == 1
    assert repo.conn.execute("SELECT COUNT(*) FROM bucket_fill_applications").fetchone()[0] == 1
    assert host.halt_flag_path().exists() == tp_failed
    if tp_failed:
        assert "p1" in host.halt_flag_path().read_text()
        assert "HALT set" in result.reason
        assert "strategy state not saved: take-profit not cleared" in result.reason
    else:
        assert "strategy state not saved" not in result.reason
    assert repo.get_session(result.session_id).outcome == result.reason
    session.reconcile_effects(repo)
    assert repo.get_bucket("b").tranches_bought == 1


def test_bucket_persistence_failure_precedes_tp_save_veto(two, repo, host, monkeypatch):
    bucket(repo)
    two.decision.intents = [REENTRY]
    two.report = ExecutionReport((filled(REENTRY),), None, (), ("p1",), ())
    monkeypatch.setattr(repo, "apply_bucket_fill", Mock(side_effect=RuntimeError("apply failed")))
    result = two.run()
    assert result.status == "executed_partial"
    assert "take-profit not cleared on position(s) p1" in result.reason
    assert "strategy state not saved: persistence failure" in result.reason
    assert "strategy state not saved: take-profit not cleared" not in result.reason
    assert "sale-effect persistence failed: RuntimeError: apply failed" in result.reason
    assert host.halt_flag_path().exists()
    two.save.assert_not_called()
    assert repo.get_strategy_state().bars == 0


@pytest.mark.parametrize("bad", [replace(SELL, amount=float("inf")), replace(SELL, amount=float("nan")),
                                  replace(SELL, amount=0), replace(SELL, amount=-1),
                                  replace(SELL, amount=None), replace(SELL, position_ref="s")])
def test_bad_schg_refused_before_upro_or_broker_write(schg, bad):
    schg.decision.intents = [CLOSE, bad, BUY]
    assert schg.run().status == "armed_refused_intents"
    schg.closes.assert_not_called()
    schg.schg.assert_not_called()
    schg.execute.assert_not_called()
    schg.client.close_position.assert_not_called()
    schg.save.assert_not_called()


def test_duplicate_schg_refused_before_upro(schg):
    schg.decision.intents = [CLOSE, SELL, SELL, BUY]
    result = schg.run()
    assert result.status == "armed_refused_intents" and "multiple SCHG sells" in result.reason
    schg.closes.assert_not_called()
    schg.schg.assert_not_called()
    schg.execute.assert_not_called()
    schg.client.close_position.assert_not_called()
    schg.save.assert_not_called()


@pytest.mark.parametrize("upro", [False, True])
def test_schg_fresh_portfolio_failure_blocks_without_schg_send(schg, repo, upro):
    if not upro:
        schg.decision.intents = [SELL, BUY]
    original = schg.strict.side_effect
    reads = 0

    def strict():
        nonlocal reads
        reads += 1
        if reads == 1 + int(upro):
            raise RuntimeError("fresh unavailable")
        return original()

    schg.strict.side_effect = strict
    result = schg.run()
    assert result.status == "executed_partial"
    assert "SCHG sell portfolio read failed: RuntimeError: fresh unavailable" in result.reason
    schg.schg.assert_not_called()
    schg.execute.assert_not_called()
    assert schg.save.call_count == int(upro) and repo.get_strategy_state().bars == 0
    assert schg.client.close_position.call_count == int(upro)


@pytest.mark.parametrize("case,stopped,sends", [("prefix", "shortfall", 1),
                                                ("empty", "no candidates", 0),
                                                ("disabled", "partials disabled; shortfall", 1)])
def test_schg_shortfall_always_blocks_final_save(schg, repo, case, stopped, sends):
    schg.decision.intents = [SELL, BUY]
    if case == "prefix":
        schg.decision.intents[0] = replace(SELL, amount=500)
    elif case == "empty":
        schg.strict.side_effect = lambda: []
    else:
        schg.positions = [p for p in schg.positions if p.instrument_id != IDS["SCHG"]]
        schg.positions += [position("s", "SCHG", units=0.5), position("t", "SCHG", units=4)]
        repo.insert_lot_annotation(replace(annotation("t"), symbol="SCHG"))
        repo.upsert_instrument(Instrument("SCHG", IDS["SCHG"], allow_partial_close=False))
    result = schg.run()
    assert result.status == "executed_partial" and f"SCHG stopped={stopped}" in result.reason
    if sends:
        assert "SCHG close s: confirmed full close confirmed" in result.reason
    assert schg.client.close_position.call_count == sends
    schg.execute.assert_not_called()
    schg.save.assert_not_called()
    assert repo.get_strategy_state().bars == 0
    assert repo.get_strategy_state().recyclable_cash == 0


@pytest.mark.parametrize("upro", [False, True])
@pytest.mark.parametrize("failure", ["exception", "unresolved", "unverified"])
def test_schg_uncertainty_never_resends_or_saves_final(schg, repo, host, monkeypatch, upro, failure):
    from nightwatch.engine.close_executor import recover_unfinished_closes
    from nightwatch.etoro.models import OrderStatus

    if not upro:
        schg.decision.intents = [SELL, BUY]
    send = schg.client.close_position.side_effect

    def close(*args):
        if len(args) == 3:
            return "upro-order"
        result = send(*args)
        if failure == "unresolved":
            raise RuntimeError("lost response")
        return result

    schg.client.close_position.side_effect = close
    if failure == "exception":
        monkeypatch.setattr(repo, "set_partial_close_order", Mock(side_effect=RuntimeError("after send")))
    elif failure == "unverified":
        schg.schg_status = OrderStatus.PARTIALLY_FILLED
    result = schg.run()
    assert result.status == "executed_partial" and "strategy state not saved" in result.reason
    if failure == "exception":
        assert "SCHG close executor error: RuntimeError: after send" in result.reason
    else:
        assert f"SCHG close s: {failure}" in result.reason
    assert schg.calls.count("balance") == 2
    assert "snapshot" in schg.calls
    assert schg.save.call_count == int(upro) and repo.get_strategy_state().bars == 0
    schg.execute.assert_not_called()
    sends = schg.client.close_position.call_count
    assert sends == 1 + int(upro)
    assert schg.run().status == "already_ran"
    assert schg.client.close_position.call_count == sends
    schg.recover_closes.side_effect = lambda **kwargs: recover_unfinished_closes(**kwargs)
    next_day = DAY + timedelta(days=3)
    monkeypatch.setattr(session, "gate_10_price_data", lambda **kwargs: session.GateResult(10, "price", True, "ok"))
    schg.clock = session.session_close_datetime(next_day) - timedelta(minutes=30)
    result2 = session._run_session_core(host=host, config=schg.config, repo=repo, client=schg.client,
                                        tiingo=FakeTiingo(), trading_date=next_day, wait=False)
    assert result2.status == "halted_before_execution"
    assert "unresolved close at recovery" in result2.reason
    assert schg.client.close_position.call_count == sends and schg.schg.call_count == 1
    assert schg.save.call_count == int(upro) and repo.get_strategy_state().bars == 0
    schg.execute.assert_not_called()


@pytest.mark.parametrize("orphan", [False, True])
def test_schg_pending_recovery_blocks_before_either_executor(schg, repo, orphan):
    from nightwatch.engine.close_executor import recover_unfinished_closes

    old = repo.create_session("2026-10-01", "live", "demo", True)
    intent = Intent(old.id, "old-schg", 1, "SCHG", "close", instrument_id=IDS["SCHG"],
                    position_id="s", units=1, status="sent")
    if orphan:
        repo.insert_intent(intent)
    else:
        repo.begin_schg_close_leg(intent, position_id="s", units_before=4, units_deducted=1)
    schg.recover_closes.side_effect = lambda **kwargs: recover_unfinished_closes(**kwargs)
    result = schg.run()
    assert result.status == "halted_before_execution" and "unresolved close at recovery" in result.reason
    schg.closes.assert_not_called()
    schg.schg.assert_not_called()
    schg.execute.assert_not_called()
    schg.save.assert_not_called()
    schg.client.close_position.assert_not_called()


def test_upro_block_prevents_schg(schg):
    schg.outcomes = ["unresolved"]
    assert schg.run().status == "executed_partial"
    schg.schg.assert_not_called()
    schg.execute.assert_not_called()


def test_dry_schg_never_executes_or_saves(schg):
    schg.config = replace(schg.config, mode=TradingMode.DRY_RUN, arm_flag_present=False)
    assert schg.run().status == "would_have_done"
    schg.closes.assert_not_called()
    schg.schg.assert_not_called()
    schg.execute.assert_not_called()
    schg.save.assert_not_called()
