"""Armed session wiring with real limits/SQLite and offline broker fakes."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nightwatch import cli
from nightwatch.config import TradingMode
from nightwatch.engine import session
from nightwatch.engine.executor import ExecutedBuy, ExecutionReport, RecoveryReport
from nightwatch.engine.preflight import GateResult, GateSpec
from nightwatch.etoro.models import Balance
from nightwatch.strategy.ladder import LadderIntent
from .test_session_decide import config, no_network, prepare_session
from .test_session_market import DAY, IDS, FakeTiingo
from .test_session_skeleton import FakeClient


BUY = LadderIntent("SCHG", "open", amount=100, reason="cash to core", book="catapult")


def filled(intent=BUY, **changes):
    return replace(ExecutedBuy(intent, 1, "request", 1, "order1", "filled", "p1", 100, "filled"), **changes)


@pytest.fixture
def armed(repo, host, config, monkeypatch):
    build_gates = session.build_preflight_gates
    client = prepare_session(host, monkeypatch)
    cfg = replace(config, mode=TradingMode.LIVE, arm_flag_present=True,
                  risk=replace(config.risk, max_daily_loss_usd=100, max_open_lots=10,
                               max_orders_per_session=1))
    decision = SimpleNamespace(intents=[BUY], new_state=replace(session.load(repo), bars=1),
                               reason="offline decision", warnings=[], updated_buckets=[])
    e = SimpleNamespace(config=cfg, client=client, decision=decision, calls=[], report=None,
                        recovery=RecoveryReport((), (), (), (), ()), failed_gate=None)
    monkeypatch.setattr(session, "decide", lambda *args: e.decision)

    def recover(**kwargs):
        assert kwargs == dict(repo=repo, client=client,
                              abandon_after_sessions=cfg.reconciliation.non_terminal_order_sessions_before_abandoned)
        e.calls.append("recover")
        return e.recovery
    e.recover = Mock(side_effect=recover)
    monkeypatch.setattr(session, "recover_unfinished", e.recover)
    from nightwatch.engine.close_executor import CloseRecoveryReport
    def recover_closes(**kwargs):
        e.calls.append("recover_closes")
        return CloseRecoveryReport((), (), (), False)
    e.recover_closes = Mock(side_effect=recover_closes)
    monkeypatch.setattr(session, "recover_unfinished_closes", e.recover_closes)

    def gates(**kwargs):
        real = build_gates(**kwargs)
        before = [GateSpec(n, f"gate_{n}", lambda n=n: GateResult(n, f"gate_{n}", True, "ok"))
                  for n in range(1, 11)]
        def risk():
            if e.failed_gate == 12:
                return GateResult(12, "risk_caps", False, "offline risk failure")
            return real[11].check()
        return before + [real[10], GateSpec(12, "risk_caps", risk)]
    monkeypatch.setattr(session, "build_preflight_gates", gates)

    portfolio = client.get_portfolio
    def get_portfolio():
        e.calls.append("portfolio")
        return portfolio()
    monkeypatch.setattr(client, "get_portfolio", get_portfolio)
    def balance():
        e.calls.append("balance")
        return Balance(1000, None, 1000 if e.calls.count("balance") == 1 else 900)
    monkeypatch.setattr(client, "get_balance", balance)

    def execute(**kwargs):
        e.calls.append("execute")
        return e.report if e.report is not None else ExecutionReport(
            tuple(filled(i) for i in kwargs["intents"]), None, (), (), ())
    e.execute = Mock(side_effect=execute)
    monkeypatch.setattr(session, "execute_buys", e.execute)
    save = session.save
    def save_state(*args):
        e.calls.append("save")
        return save(*args)
    e.save = Mock(side_effect=save_state)
    monkeypatch.setattr(session, "save", e.save)
    insert = repo.insert_snapshot
    def insert_snapshot(snap):
        e.calls.append("snapshot")
        return insert(snap)
    monkeypatch.setattr(repo, "insert_snapshot", insert_snapshot)
    e.run = lambda: session._run_session_core(host=host, config=e.config, repo=repo,
                                              client=client, tiingo=FakeTiingo(),
                                              trading_date=DAY, wait=False)
    return e


def test_armed_fill_saves_before_post_execution_snapshot(armed, repo, host):
    result = armed.run()
    row = repo.get_session(result.session_id)
    assert result.status == "executed" and row.mode == "live" and row.armed
    assert row.state == "finished" and row.outcome == result.reason
    armed.execute.assert_called_once()
    args = armed.execute.call_args.kwargs
    assert args == dict(repo=repo, client=armed.client, session_id=row.id, trading_date=DAY,
                        intents=[BUY], instrument_ids=IDS, spy=args["spy"], max_orders=1,
                        deadline=session.session_close_datetime(DAY) - timedelta(minutes=5), now=host.now)
    assert args["spy"].close == 100
    armed.save.assert_called_once_with(repo, armed.decision.new_state)
    assert repo.get_strategy_state().bars == 1
    assert armed.calls.index("execute") < armed.calls.index("save") < armed.calls.index("snapshot")
    assert armed.calls.count("balance") == 2
    snap = repo.latest_snapshot()
    assert snap.raw["phase"] == "post_execution" and snap.available_cash == 900
    assert "accepted 1, dropped 0" in result.reason
    assert "filled $100.00 filled $100.00 filled" in result.reason
    assert "stopped=None; deferred 0; followups 0" in result.reason


def test_armed_without_order_ceiling_refuses_without_broker_calls(armed, repo):
    armed.config = replace(armed.config, risk=replace(armed.config.risk, max_orders_per_session=None))
    result = armed.run()
    assert result == session.SessionResult(None, "refused_armed", None,
                                          "armed but refused: risk.max_orders_per_session unset")
    assert repo.latest_session() is None and armed.calls == []
    armed.recover.assert_not_called()
    armed.execute.assert_not_called()


@pytest.mark.parametrize("raises", [False, True])
def test_recovery_inside_gate_11_before_portfolio(armed, repo, raises):
    if raises:
        armed.recover.side_effect = RuntimeError("offline recovery")
    else:
        armed.recovery = RecoveryReport(("resolved1",), ("pending1",), ("old1",), (), ("inspect1",))
    result = armed.run()
    if raises:
        assert result.status == "preflight_failed" and result.failed_gate == 11
        assert "recovery: RuntimeError: offline recovery" in result.reason
        assert repo.get_session(result.session_id).state == "aborted"
        assert "portfolio" not in armed.calls
        armed.execute.assert_not_called()
        armed.save.assert_not_called()
    else:
        assert armed.calls.index("recover_closes") < armed.calls.index("recover") < armed.calls.index("portfolio")
        assert result.status == "executed"
        for name, line in (("resolved", "resolved1"), ("still_unresolved", "pending1"),
                           ("abandoned", "old1"), ("followups", "inspect1")):
            assert f"recovery {name} 1: {line}" in result.reason


@pytest.mark.parametrize("persistence_fails", [False, True])
@pytest.mark.parametrize("intents", [[BUY], [], [LadderIntent("UPRO", "open", amount=100)]])
def test_recovery_tp_failure_skips_execution_and_still_snapshots(armed, repo, host, monkeypatch, persistence_fails, intents):
    armed.recovery = RecoveryReport((), (), (), ("p1",), ())
    armed.decision.intents = intents
    limits = Mock(side_effect=AssertionError("limits must not run after recovery TP failure"))
    monkeypatch.setattr(session, "apply_submission_limits", limits)
    if persistence_fails:
        monkeypatch.setattr(session, "write_halt", Mock(side_effect=OSError("disk full")))
    get_pnl = armed.client.get_pnl
    def pnl():
        if persistence_fails:
            session.write_halt.assert_called_once_with(
                host, reason="take-profit not cleared on position(s) p1", source="session")
        else:
            assert "p1" in host.halt_flag_path().read_text()
        return get_pnl()
    monkeypatch.setattr(armed.client, "get_pnl", pnl)
    before = repo.get_strategy_state()
    result = armed.run()
    assert result.status == "halted_before_execution"
    assert "; execution skipped: take-profit not cleared at recovery" in result.reason
    assert "strategy state not saved: take-profit not cleared at recovery" in result.reason
    limits.assert_not_called()
    armed.execute.assert_not_called()
    armed.save.assert_not_called()
    assert repo.get_strategy_state() == before
    assert armed.calls.count("balance") == 1 and "snapshot" in armed.calls
    snap = repo.latest_snapshot()
    assert snap.raw["phase"] == "post_execution" and snap.available_cash == 1000
    row = repo.get_session(result.session_id)
    assert row.state == "finished" and row.outcome == result.reason
    if persistence_fails:
        assert "HALT persistence failed: OSError: disk full" in result.reason
        assert row.error == "disk full"
    else:
        assert "take-profit not cleared on position(s) p1; HALT set" in result.reason
        assert row.error is None


@pytest.mark.parametrize("persistence_fails", [False, True])
def test_execution_tp_failure_halt_and_persistence_error(armed, repo, host, monkeypatch, persistence_fails):
    armed.report = ExecutionReport((filled(),), None, (), ("p1",), ("inspect TP",))
    if persistence_fails:
        monkeypatch.setattr(session, "write_halt", Mock(side_effect=OSError("disk full")))
    get_pnl = armed.client.get_pnl
    def pnl():
        if not persistence_fails:
            assert "p1" in host.halt_flag_path().read_text()
        else:
            session.write_halt.assert_called_once_with(
                host, reason="take-profit not cleared on position(s) p1", source="session")
        return get_pnl()
    monkeypatch.setattr(armed.client, "get_pnl", pnl)
    result = armed.run()
    assert "followups 1: inspect TP" in result.reason
    row = repo.get_session(result.session_id)
    if persistence_fails:
        assert "HALT persistence failed: OSError: disk full" in result.reason
        assert row.error == "disk full"
    else:
        assert "HALT set" in result.reason and row.error is None


def test_real_limits_drop_prevents_state_save(armed, repo):
    armed.decision.intents = [BUY, BUY]
    armed.config = replace(armed.config, risk=replace(armed.config.risk, max_notional_per_session_usd=100))
    before = repo.get_strategy_state()
    result = armed.run()
    assert result.status == "executed" and "accepted 1, dropped 1" in result.reason
    assert armed.execute.call_args.kwargs["intents"] == [BUY]
    armed.save.assert_not_called()
    assert repo.get_strategy_state() == before
    assert "strategy state not saved: limits dropped intents" in result.reason


@pytest.mark.parametrize("case", ["stopped", "unresolved", "rejected", "deferred", "missing_result"])
def test_incomplete_execution_never_saves(armed, case):
    reports = {
        "stopped": ExecutionReport((filled(),), "ambiguous outcome", (), (), ()),
        "unresolved": ExecutionReport((filled(outcome="unresolved", fill_amount_usd=None),), None, (), (), ()),
        "rejected": ExecutionReport((filled(outcome="rejected", fill_amount_usd=None),), None, (), (), ()),
        "deferred": ExecutionReport((filled(),), None, (BUY,), (), ()),
        "missing_result": ExecutionReport((), None, (), (), ()),
    }
    armed.report = reports[case]
    result = armed.run()
    assert result.status == "executed" and "strategy state not saved" in result.reason
    armed.save.assert_not_called()
    if case in ("unresolved", "rejected"):
        assert f"{case} $100.00 filled $n/a" in result.reason


def test_execution_exception_still_rereads_balance_and_snapshots(armed, repo):
    armed.execute.side_effect = RuntimeError("after submission")
    result = armed.run()
    assert result.status == "executed"
    assert "execution error: RuntimeError: after submission" in result.reason
    assert "strategy state not saved: execution exception" in result.reason
    armed.save.assert_not_called()
    assert armed.calls.count("balance") == 2 and "snapshot" in armed.calls
    snap = repo.latest_snapshot()
    assert snap.available_cash == 900 and snap.raw["phase"] == "post_execution"


@pytest.mark.parametrize("intent", [LadderIntent("UPRO", "open", amount=100),
                                     LadderIntent("SCHG", "close", position_ref="p1"),
                                     LadderIntent("UPRO", "close", position_ref="p1", amount=5),
                                     replace(BUY, amount=None), replace(BUY, amount=0),
                                     replace(BUY, amount=-1)])
def test_non_executable_intents_refused(armed, repo, intent):
    armed.decision.intents = [BUY, intent]
    result = armed.run()
    assert result.status == "armed_refused_intents"
    assert "armed: unsupported intents (SCHG sells unsupported in S7-3b):" in result.reason
    armed.execute.assert_not_called()
    armed.save.assert_not_called()
    assert repo.latest_snapshot().raw["phase"] == "post_execution"
    assert armed.calls.count("balance") == 1


def test_empty_decision_advances_state_without_executor(armed, repo):
    armed.decision.intents = []
    result = armed.run()
    assert result.status == "executed"
    armed.execute.assert_not_called()
    armed.save.assert_called_once_with(repo, armed.decision.new_state)
    assert repo.get_strategy_state().bars == 1
    assert armed.calls.count("balance") == 1


def test_dry_run_never_recovers_executes_or_saves(armed, repo):
    armed.config = replace(armed.config, mode=TradingMode.DRY_RUN, arm_flag_present=False)
    result = armed.run()
    assert result.status == "would_have_done"
    row = repo.get_session(result.session_id)
    assert row.mode == "dry_run" and not row.armed
    armed.recover_closes.assert_not_called()
    assert repo.latest_snapshot().raw["phase"] == "post_decision"
    armed.recover.assert_not_called()
    armed.execute.assert_not_called()
    armed.save.assert_not_called()


@pytest.mark.parametrize("is_armed,status,code", [(True, "executed", 0),
                                                 (True, "armed_refused_intents", 1),
                                                 (True, "halted_before_execution", 1),
                                                 (True, "executed_partial", 1),
                                                 (False, "would_have_done", 0)])
def test_cli_role_and_execution_exit_code(config, host, monkeypatch, is_armed, status, code):
    cfg = replace(config, mode=TradingMode.LIVE if is_armed else TradingMode.DRY_RUN,
                  arm_flag_present=is_armed)
    client = FakeClient()
    make_client = Mock(return_value=client)
    notifier = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(cli, "load_config", lambda **kwargs: cfg)
    monkeypatch.setattr(cli, "get_host", lambda: host)
    monkeypatch.setattr(cli, "EtoroClient", make_client)
    monkeypatch.setattr(cli, "TiingoClient", lambda *args: FakeTiingo())
    monkeypatch.setattr(cli, "TelegramNotifier", lambda *args, **kwargs: notifier)
    monkeypatch.setattr(cli, "run_session", lambda **kwargs: session.SessionResult(None, status, None, "offline"))
    assert cli.main(["run", "--date", DAY.isoformat(), "--no-wait"]) == code
    make_client.assert_called_once()
    assert make_client.call_args.kwargs["role"] == ("write" if is_armed else "read")
    assert client.closed


def test_partial_fill_never_saves(armed):
    armed.report = ExecutionReport((filled(detail="filled; partial fill"),), None, (), (), ())
    result = armed.run()
    assert "strategy state not saved: partial fill" in result.reason
    armed.save.assert_not_called()


def test_filled_without_position_sets_halt_and_never_saves(armed, host):
    armed.report = ExecutionReport((filled(position_id=None),), None, (), (), ())
    result = armed.run()
    assert "order order1" in host.halt_flag_path().read_text()
    assert "HALT set" in result.reason
    assert "strategy state not saved: filled result has no position id" in result.reason
    armed.save.assert_not_called()


@pytest.mark.parametrize("failure", ["gate12", "rerun", "signal", "portfolio", "balance", "inputs", "decide"])
@pytest.mark.parametrize("persistence_fails", [False, True])
def test_recovery_halt_survives_all_early_returns(armed, repo, host, monkeypatch, failure, persistence_fails):
    armed.recovery = RecoveryReport((), (), (), ("p1",), ())
    if persistence_fails:
        monkeypatch.setattr(session, "write_halt", Mock(side_effect=OSError("disk full")))
    def fail(*args, **kwargs):
        raise RuntimeError("offline failure")
    if failure == "gate12":
        armed.failed_gate = 12
    elif failure == "rerun":
        monkeypatch.setattr(session, "gate_10_price_data", lambda **kwargs: GateResult(10, "price_data", False, "offline"))
    elif failure == "signal":
        monkeypatch.setattr(session, "assemble_spy_signal", fail)
    elif failure == "portfolio":
        portfolio = armed.client.get_portfolio
        def after_gate11():
            if "portfolio" in armed.calls:
                fail()
            return portfolio()
        monkeypatch.setattr(armed.client, "get_portfolio", after_gate11)
    elif failure == "balance":
        monkeypatch.setattr(armed.client, "get_balance", fail)
    elif failure == "inputs":
        def bad_inputs(**kwargs):
            raise session.DecideInputError("offline inputs")
        monkeypatch.setattr(session, "build_ladder_inputs", bad_inputs)
    else:
        monkeypatch.setattr(session, "decide", fail)
    result = armed.run()
    expected = {"gate12": "preflight_failed", "rerun": "rerun_failed", "signal": "signal_failed"}
    assert result.status == expected.get(failure, "decide_failed")
    row = repo.get_session(result.session_id)
    assert row.state == "aborted" and row.outcome == result.reason
    assert "recovery resolved 0; recovery still_unresolved 0; recovery abandoned 0; recovery followups 0" in result.reason
    if persistence_fails:
        assert "HALT persistence failed: OSError: disk full" in result.reason
        assert row.error == "disk full"
    else:
        assert "p1" in host.halt_flag_path().read_text() and "HALT set" in result.reason
        assert row.error is None
    armed.execute.assert_not_called()
    armed.save.assert_not_called()


@pytest.mark.parametrize("persistence_fails", [False, True])
@pytest.mark.parametrize("source", ["execution", "recovery"])
def test_tp_failure_sends_extra_notifier_event(armed, repo, host, monkeypatch, persistence_fails, source):
    if source == "recovery":
        armed.recovery = RecoveryReport((), (), (), ("p1",), ())
    else:
        armed.report = ExecutionReport((filled(),), None, (), ("p1",), ())
    if persistence_fails:
        monkeypatch.setattr(session, "write_halt", Mock(side_effect=OSError("disk full")))
    from nightwatch.notify import poll
    monkeypatch.setattr(poll, "process_pending", lambda *args: None)
    notifier = SimpleNamespace(send=Mock())
    result = session.run_session(host=host, config=armed.config, repo=repo,
                                 client=armed.client, tiingo=FakeTiingo(), trading_date=DAY,
                                 wait=False, notifier=notifier)
    assert result.status == ("halted_before_execution" if source == "recovery" else "executed")
    if source == "recovery":
        armed.execute.assert_not_called()
        armed.save.assert_not_called()
    calls = notifier.send.call_args_list
    assert [c.kwargs["event"] for c in calls] == ["session_report", "tp_not_cleared"]
    text = calls[1].args[0]
    assert text.startswith("[DEMO] TP NOT CLEARED — HALT ")
    assert ("persistence FAILED: OSError: disk full" if persistence_fails else "HALT set") in text
    if persistence_fails:
        assert text.endswith(" Next session is NOT protected.")
        assert "Next session stops at gate 1" not in text
    else:
        assert text.endswith(" Next session stops at gate 1 until you /resume.")
        assert "NOT protected" not in text
