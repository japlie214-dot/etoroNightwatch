"""T048: offline redaction at session persistence and notification boundaries."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nightwatch.config import TradingMode
from nightwatch.db.repo import Decision, Intent
from nightwatch.engine import session
from nightwatch.engine.preflight import GateResult, GateSpec
from nightwatch.etoro.models import OrderResult, OrderStatus, Position
from nightwatch.logging_setup import REDACTED, RedactionFilter
from .test_session_network_abort import harness
from .test_session_market import DAY, config, no_network


PLANTED = "synthetic-private-value"


@pytest.fixture
def recovered(harness, repo, monkeypatch):
    h = harness
    h.config = replace(h.config, mode=TradingMode.LIVE, arm_flag_present=True,
                       etoro=replace(h.config.etoro, secrets={"ETORO_PUBLIC_KEY": PLANTED}),
                       risk=replace(h.config.risk, max_daily_loss_usd=100,
                                    max_open_lots=10, max_orders_per_session=1))
    old = repo.create_session("2026-10-01", "live", "demo", True)
    repo.insert_intent(Intent(old.id, "old-pending-buy", 1, "SCHG", "buy",
                              instrument_id=2, book="catapult", amount_usd=100))
    repo.insert_decision(Decision(old.id, "2026-10-01", spy_close=100, spy_ath=110,
                                  drawdown=-0.1, tier=1))
    repo.finish_session(old.id, "finished", "old session")
    h.client.recover_by_reference_id = Mock(return_value=OrderResult(
        "old-order", "old-pending-buy", OrderStatus.FILLED,
        position_id="old-position", fill_amount_usd=100))
    h.client.get_portfolio = lambda: [Position("old-position", 2, 1, 100, 100, 1, True,
                                              is_no_take_profit=True)]
    h.client.clear_take_profit = Mock(side_effect=AssertionError("TP already absent"))
    monkeypatch.setattr(session, "decide", lambda *a: SimpleNamespace(
        intents=[], new_state=session.load(repo), reason="offline", warnings=[], updated_buckets=[]))
    h.save = Mock(wraps=session.save)
    monkeypatch.setattr(session, "save", h.save)
    return h


@pytest.mark.parametrize("status", ["executed", "executed_partial", "preflight_failed"])
@pytest.mark.parametrize("sanitizer_fails", [False, True])
def test_recovery_reason_and_halt_error_are_redacted(recovered, repo, monkeypatch, status, sanitizer_fails):
    h = recovered
    monkeypatch.setattr(repo, "finalize_filled_buy", Mock(
        side_effect=RuntimeError("annotation foreign-text " + PLANTED)))
    if status == "executed_partial":
        monkeypatch.setattr(session, "reconcile_effects", Mock(
            side_effect=RuntimeError("reconcile foreign-text " + PLANTED)))
        monkeypatch.setattr(session, "write_halt", Mock(
            side_effect=OSError("disk foreign-text " + PLANTED)))
    elif status == "preflight_failed":
        gates = session.build_preflight_gates
        def fail_last(**kwargs):
            return gates(**kwargs)[:-1] + [GateSpec(12, "risk_caps", lambda: GateResult(
                12, "risk_caps", False, "gate foreign-text " + PLANTED))]
        monkeypatch.setattr(session, "build_preflight_gates", fail_last)
    if sanitizer_fails:
        monkeypatch.setattr(RedactionFilter, "redact", Mock(side_effect=ValueError("sanitize failed")))
    result = h.run()
    row = repo.get_session(result.session_id)
    expected_status = "preflight_failed" if status == "preflight_failed" else "halted_before_execution"
    assert result.status == expected_status
    assert row.state == ("aborted" if status == "preflight_failed" else "finished")
    assert row.finished_at is not None and row.outcome == result.reason
    assert row.preflight_failed_gate == (12 if status == "preflight_failed" else None)
    h.save.assert_not_called()
    assert repo.get_intents(row.id) == []
    saved = repo.get_intent_by_request_id("old-pending-buy")
    assert saved.status == "filled_unannotated" and saved.resolved_at is None
    assert result.tp_failed
    assert result.halt_set == (status != "executed_partial")
    assert repo.get_lot_annotation("old-position") is None
    assert repo.list_tp_clears("old-position") == []
    h.client.clear_take_profit.assert_not_called()
    texts = [row.outcome, row.error or "", result.reason,
             *(call.args[0] for call in h.notifier.send.call_args_list)]
    assert h.notifier.send.call_count >= 1
    assert all(PLANTED not in text for text in texts)
    assert "recovery followups 1" in result.reason
    if sanitizer_fails:
        assert all("foreign-text" not in text and "old-position" not in text for text in texts)
        assert expected_status in result.reason
        if status == "executed_partial":
            assert "; HALT details omitted" in result.reason
            assert row.error == "OSError"
    else:
        assert REDACTED in result.reason
        if status == "executed_partial":
            assert "HALT persistence failed: OSError: disk foreign-text " + REDACTED in result.reason
            assert row.error == "disk foreign-text " + REDACTED


@pytest.mark.parametrize("sanitizer_fails", [False, True])
@pytest.mark.parametrize("event", ["tp_not_cleared", "execution_halt", "integrity", "interval_incomplete", "alert_c", "alert_a"])
def test_each_notification_is_redacted(recovered, monkeypatch, event, sanitizer_fails):
    h = recovered
    if event == "tp_not_cleared":
        monkeypatch.setattr(session, "recover_unfinished", Mock(return_value=session.RecoveryReport(
            (), (), (), ("position " + PLANTED,), ("foreign-text " + PLANTED,))))
    elif event == "execution_halt":
        monkeypatch.setattr(session, "reconcile_effects", Mock(
            side_effect=RuntimeError("foreign-text " + PLANTED)))
    else:
        interval = SimpleNamespace(state="incomplete" if event == "interval_incomplete" else
                                   "integrity" if event == "integrity" else "complete",
                                   pnl_usd=-100, pnl_pct=-10, residual_usd=-100, excluded_positions=(),
                                   alert_a=event == "alert_a", alert_c=event == "alert_c",
                                   modelled_residual_usd=-100, reasons=("foreign-text " + PLANTED,))
        monkeypatch.setattr(session, "_interval_for", Mock(return_value=interval))
    if sanitizer_fails:
        monkeypatch.setattr(RedactionFilter, "redact", Mock(side_effect=ValueError("sanitize failed")))
    result = h.run()
    assert result.status == ("halted_before_execution" if event == "tp_not_cleared" else
                             "executed_partial" if event == "execution_halt" else "executed")
    calls = h.notifier.send.call_args_list
    assert [call.kwargs["event"] for call in calls] == ["session_report", event]
    assert all(PLANTED not in call.args[0] for call in calls)
    if sanitizer_fails:
        assert all("foreign-text" not in call.args[0] and "details omitted" in call.args[0] for call in calls)


@pytest.mark.parametrize("sanitizer_fails", [False, True])
@pytest.mark.parametrize("abort", [False, True])
def test_alert_only_tp_positions_are_redacted_even_after_abort(recovered, repo, monkeypatch, sanitizer_fails, abort):
    h = recovered
    monkeypatch.setattr(session, "recover_unfinished", Mock(return_value=session.RecoveryReport(
        (), (), (), (), (), tp_uncleared=("position-" + PLANTED,))))
    if abort:
        gates = session.build_preflight_gates
        def fail_last(**kwargs):
            return gates(**kwargs)[:-1] + [GateSpec(12, "risk_caps", lambda: GateResult(
                12, "risk_caps", False, "offline failure"))]
        monkeypatch.setattr(session, "build_preflight_gates", fail_last)
    if sanitizer_fails:
        monkeypatch.setattr(RedactionFilter, "redact", Mock(side_effect=ValueError("sanitize failed")))
    result = h.run()
    assert result.status == ("preflight_failed" if abort else "executed")
    assert result.tp_uncleared_positions == ("position-" + PLANTED,)
    assert not result.tp_failed and not result.halt_set
    assert h.save.call_count == int(not abort)
    calls = h.notifier.send.call_args_list
    assert [call.kwargs["event"] for call in calls] == ["session_report", "tp_not_cleared"]
    assert all(PLANTED not in call.args[0] for call in calls)
    assert PLANTED not in repo.get_session(result.session_id).outcome
    alert = calls[-1].args[0]
    if sanitizer_fails:
        assert alert == "tp_not_cleared; details omitted"
    else:
        assert "TP still set on: position-" + REDACTED in alert
        assert "; session continued. Clear it in eToro if wanted." in alert
    assert "HALT" not in alert and "/resume" not in alert


@pytest.mark.parametrize("sanitizer_fails", [False, True])
def test_interval_halt_error_is_redacted(harness, repo, monkeypatch, sanitizer_fails):
    h = harness
    h.config = replace(h.config, tiingo=replace(h.config.tiingo, secrets={"TIINGO_KEY": PLANTED}))
    interval = SimpleNamespace(state="integrity", reasons=("units changed",), alert_c=False,
                               pnl_usd=None, pnl_pct=None, residual_usd=None, excluded_positions=(), alert_a=False)
    monkeypatch.setattr(session, "_interval_for", Mock(return_value=interval))
    monkeypatch.setattr(session, "write_halt", Mock(side_effect=OSError("disk foreign-text " + PLANTED)))
    if sanitizer_fails:
        monkeypatch.setattr(RedactionFilter, "redact", Mock(side_effect=ValueError("sanitize failed")))
    result = h.run()
    row = repo.get_session(result.session_id)
    assert result.status == "would_have_done" and row.state == "finished"
    assert row.outcome == result.reason
    assert PLANTED not in result.reason and PLANTED not in row.error
    assert all(PLANTED not in call.args[0] for call in h.notifier.send.call_args_list)
    if sanitizer_fails:
        assert row.error == "OSError" and "foreign-text" not in result.reason
        assert "; HALT details omitted" in result.reason
    else:
        assert row.error == "disk foreign-text " + REDACTED


def test_helper_strips_longest_config_values_before_filter_and_truncation(config, monkeypatch):
    cfg = replace(config, etoro=replace(config.etoro, secrets={"A": "short", "B": "short-long"}),
                  telegram=replace(config.telegram, secrets={"C": "telegram-value"}),
                  tiingo=replace(config.tiingo, secrets={"D": "tiingo-value"}))
    redact = Mock(wraps=RedactionFilter.redact)
    monkeypatch.setattr(RedactionFilter, "redact", redact)
    text = "x" * 290 + " short-long telegram-value tiingo-value token=synthetic"
    result = session._redact_reason(text, config=cfg, fallback="omitted", limit=300)
    composed = "x" * 290 + f" {REDACTED} {REDACTED} {REDACTED} token=synthetic"
    redact.assert_called_once_with(composed)
    assert result == RedactionFilter.redact(composed)[:300]


@pytest.mark.parametrize("sanitizer_fails", [False, True])
def test_abort_halt_failure_is_redacted(recovered, repo, monkeypatch, sanitizer_fails):
    h = recovered
    monkeypatch.setattr(session, "recover_unfinished", Mock(return_value=session.RecoveryReport(
        (), (), (), ("position " + PLANTED,), ("foreign-text " + PLANTED,))))
    monkeypatch.setattr(session, "write_halt", Mock(side_effect=OSError("disk foreign-text " + PLANTED)))
    gates = session.build_preflight_gates
    monkeypatch.setattr(session, "build_preflight_gates", lambda **kwargs: gates(**kwargs)[:-1] + [
        GateSpec(12, "risk_caps", lambda: GateResult(12, "risk_caps", False, "offline"))])
    if sanitizer_fails:
        monkeypatch.setattr(RedactionFilter, "redact", Mock(side_effect=ValueError("sanitize failed")))
    result = h.run()
    row = repo.get_session(result.session_id)
    assert result.status == "preflight_failed" and row.state == "aborted"
    assert row.outcome == result.reason
    assert result.tp_failed and result.halt_failure is not None and not result.halt_set
    assert [call.kwargs["event"] for call in h.notifier.send.call_args_list] == ["session_report", "tp_not_cleared"]
    assert "Next session is NOT protected" in h.notifier.send.call_args.args[0]
    assert PLANTED not in result.reason and PLANTED not in row.error
    assert all(PLANTED not in call.args[0] for call in h.notifier.send.call_args_list)
    if sanitizer_fails:
        assert row.error == "OSError" and "foreign-text" not in result.reason
        assert "; HALT details omitted" in result.reason
    else:
        assert "HALT persistence failed: OSError: disk foreign-text " + REDACTED in result.reason
        assert row.error == "disk foreign-text " + REDACTED


@pytest.mark.parametrize("status", ["refused_armed", "already_ran", "signal_failed"])
@pytest.mark.parametrize("sanitizer_fails", [False, True])
def test_other_result_reasons_are_redacted(harness, repo, monkeypatch, status, sanitizer_fails):
    h = harness
    h.config = replace(h.config, etoro=replace(h.config.etoro, secrets={"ETORO_PUBLIC_KEY": PLANTED}))
    if status == "refused_armed":
        h.config = replace(h.config, mode=TradingMode.LIVE, arm_flag_present=True)
        monkeypatch.setattr(session, "arming_refusals", Mock(return_value=["foreign-text " + PLANTED]))
    elif status == "already_ran":
        repo.create_session(DAY.isoformat(), "dry_run", "demo", False, state="foreign-text " + PLANTED)
    else:
        monkeypatch.setattr(session, "assemble_spy_signal", Mock(side_effect=ValueError("foreign-text " + PLANTED)))
    if sanitizer_fails:
        monkeypatch.setattr(RedactionFilter, "redact", Mock(side_effect=ValueError("sanitize failed")))
    result = h.run()
    assert result.status == status and PLANTED not in result.reason
    if sanitizer_fails:
        assert "foreign-text" not in result.reason and status in result.reason
        if status == "signal_failed":
            assert "ValueError" in result.reason
    else:
        assert REDACTED in result.reason


@pytest.mark.parametrize("source", ["recovery_tp", "execution_tp", "execution", "integrity", "alert_c"])
@pytest.mark.parametrize("sanitizer_fails", [False, True])
def test_failed_halt_remains_unprotected_with_real_core(recovered, repo, monkeypatch, source, sanitizer_fails):
    h = recovered
    if source == "recovery_tp":
        monkeypatch.setattr(session, "recover_unfinished", Mock(return_value=session.RecoveryReport(
            (), (), (), ("position " + PLANTED,), ())))
        event = "tp_not_cleared"
    elif source == "execution_tp":
        from .test_session_armed import BUY, filled
        monkeypatch.setattr(session, "decide", lambda *a: SimpleNamespace(
            intents=[BUY], new_state=session.load(repo), reason="offline", warnings=[], updated_buckets=[]))
        monkeypatch.setattr(session, "execute_buys", Mock(return_value=session.ExecutionReport(
            (filled(),), None, (), ("position " + PLANTED,), ())))
        event = "tp_not_cleared"
    elif source == "execution":
        monkeypatch.setattr(session, "reconcile_effects", Mock(side_effect=RuntimeError("foreign-text " + PLANTED)))
        event = "execution_halt"
    else:
        interval = SimpleNamespace(state="integrity" if source == "integrity" else "complete",
                                   pnl_usd=-100, pnl_pct=-10, residual_usd=-100, excluded_positions=(),
                                   alert_a=False, alert_c=source == "alert_c", modelled_residual_usd=-100,
                                   reasons=("foreign-text " + PLANTED,))
        monkeypatch.setattr(session, "_interval_for", Mock(return_value=interval))
        event = source
    monkeypatch.setattr(session, "write_halt", Mock(side_effect=OSError("disk foreign-text " + PLANTED)))
    if sanitizer_fails:
        monkeypatch.setattr(RedactionFilter, "redact", Mock(side_effect=ValueError("sanitize failed")))
    result = h.run()
    row = repo.get_session(result.session_id)
    calls = h.notifier.send.call_args_list
    assert [call.kwargs["event"] for call in calls] == ["session_report", event]
    alert = calls[-1].args[0]
    assert "halt persistence failed" in alert.lower() and "Next session is NOT protected" in alert
    assert "HALT set" not in alert and "stops at gate 1" not in alert
    assert all(PLANTED not in text for text in [row.outcome, row.error, *(c.args[0] for c in calls)])
    if sanitizer_fails:
        assert all("foreign-text" not in c.args[0] for c in calls)


@pytest.mark.parametrize("source", ["tp", "interval"])
@pytest.mark.parametrize("sanitizer_fails", [False, True])
def test_both_halt_file_reasons_are_redacted(recovered, host, monkeypatch, source, sanitizer_fails):
    from nightwatch.engine.halt import read_halt

    h = recovered
    if source == "tp":
        monkeypatch.setattr(session, "recover_unfinished", Mock(return_value=session.RecoveryReport(
            (), (), (), ("foreign-text " + PLANTED,), ())))
    else:
        interval = SimpleNamespace(state="integrity", pnl_usd=None, pnl_pct=None, residual_usd=None,
                                   excluded_positions=(), alert_a=False, alert_c=False,
                                   reasons=("foreign-text " + PLANTED,))
        monkeypatch.setattr(session, "_interval_for", Mock(return_value=interval))
    if sanitizer_fails:
        monkeypatch.setattr(RedactionFilter, "redact", Mock(side_effect=ValueError("sanitize failed")))
    result = h.run()
    assert result.status == ("halted_before_execution" if source == "tp" else "executed")
    reason = read_halt(host)["reason"]
    assert PLANTED not in reason
    if sanitizer_fails:
        assert "foreign-text" not in reason and "details omitted" in reason
    else:
        assert REDACTED in reason
    assert result.halt_set and result.halt_failure is None


@pytest.mark.parametrize("phrase", ["take-profit not cleared", "close executor error:",
                                    "sale-effect persistence failed", "HALT persistence failed: "])
def test_foreign_reason_cannot_select_alert_or_claim_halt(recovered, monkeypatch, phrase):
    h = recovered
    monkeypatch.setattr(session, "recover_unfinished", Mock(return_value=session.RecoveryReport(
        (), (), (), (), (phrase + " foreign-text " + PLANTED,))))
    interval = SimpleNamespace(state="incomplete", pnl_usd=None, pnl_pct=None, residual_usd=None,
                               excluded_positions=(), alert_a=False, alert_c=False,
                               reasons=("history unavailable",))
    monkeypatch.setattr(session, "_interval_for", Mock(return_value=interval))
    result = h.run()
    assert not result.tp_failed and not result.execution_halted and not result.halt_set
    assert result.halt_failure is None
    calls = h.notifier.send.call_args_list
    assert [call.kwargs["event"] for call in calls] == ["session_report", "interval_incomplete"]
    assert "no HALT." in calls[-1].args[0]
    assert "NOT protected" not in calls[-1].args[0]
