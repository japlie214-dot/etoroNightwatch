"""T055: real recovery and SQLite, offline gates and broker only."""

from dataclasses import replace
from datetime import date
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nightwatch import cli
from nightwatch.config import TradingMode
from nightwatch.db.repo import Intent
from nightwatch.engine import session
from nightwatch.engine.preflight import GateResult, GateSpec, gate_1_halt, gate_5_database
from .test_executor_recovery import FakeRecoveryClient, _filled, _position, _seed_intent
from .test_session_decide import config, no_network
from .test_session_market import FakeTiingo, NOW


DAY = date(2026, 10, 16)


@pytest.fixture
def recovery(repo, host, config, monkeypatch):
    cfg = replace(config, mode=TradingMode.LIVE, arm_flag_present=True,
                  strategy=replace(config.strategy, exit_mode="fixed"),
                  risk=replace(config.risk, max_daily_loss_usd=100, max_open_lots=10,
                               max_orders_per_session=3))
    client = FakeRecoveryClient(repo)
    client.set_deadline_in = lambda seconds: None
    monkeypatch.setattr(host, "now", lambda: NOW)
    real_gates = session.build_preflight_gates
    calls = []

    def gates(**kwargs):
        built = real_gates(**kwargs)

        def check(number):
            calls.append(number)
            if number == 1:
                return gate_1_halt(host)
            if number == 5:
                return gate_5_database(repo=repo, session_id=kwargs["session_id"])
            if number == 11:
                return built[10].check()
            return GateResult(number, f"gate_{number}", number != 12, "offline stop")

        return [GateSpec(n, f"gate_{n}", lambda n=n: check(n)) for n in range(1, 13)]

    monkeypatch.setattr(session, "build_preflight_gates", gates)
    buy, close = Mock(side_effect=AssertionError("no buy")), Mock(side_effect=AssertionError("no close"))
    monkeypatch.setattr(session, "execute_buys", buy)
    monkeypatch.setattr(session, "execute_closes", close)
    monkeypatch.setattr(session, "execute_schg_sell", close)
    originals = session.recover_unfinished, session.recover_unfinished_closes
    recover_buy, recover_close = Mock(wraps=originals[0]), Mock(wraps=originals[1])
    monkeypatch.setattr(session, "recover_unfinished", recover_buy)
    monkeypatch.setattr(session, "recover_unfinished_closes", recover_close)
    run = lambda **kwargs: session.run_session(
        host=host, config=kwargs.pop("config", cfg), repo=repo, client=client,
        tiingo=FakeTiingo(), trading_date=kwargs.pop("trading_date", DAY), wait=False, **kwargs)
    return SimpleNamespace(config=cfg, client=client, calls=calls, run=run,
                           recover_buy=recover_buy, recover_close=recover_close, buy=buy, close=close)


def seed(repo, recovery, *, rid="sent", day="2026-10-15", status="sent", filled=True):
    _seed_intent(repo, rid=rid, day=day, status=status)
    if filled:
        recovery.client.recover_by_rid[rid] = [_filled()]
        recovery.client.portfolio_queue = [[_position(is_no_take_profit=True)]]
    return repo.get_session_by_date(day)


def test_filled_crash_recovers_before_gate_five_once_without_resubmission(repo, host, recovery):
    old = seed(repo, recovery)
    result = recovery.run()
    previous = repo.get_session(old.id)
    assert previous.state == "finished" and previous.outcome == "crashed_recovered"
    assert previous.finished_at is not None
    assert repo.get_intent_by_request_id("sent").status == "recovered"
    assert repo.get_lot_annotation("P1") is not None
    assert result.failed_gate == 12 and recovery.calls == list(range(1, 13))
    assert gate_5_database(repo=repo, session_id=result.session_id).passed
    recovery.recover_buy.assert_called_once()
    recovery.recover_close.assert_called_once()
    recovery.buy.assert_not_called()
    recovery.close.assert_not_called()
    assert recovery.client.buy_calls == recovery.client.patch_calls == recovery.client.violations == []
    assert not host.halt_flag_path().exists()
    assert "crashed_recovered" in result.reason


@pytest.mark.parametrize("case", ["missing", "raises", "pending", "abandoned", "ages_to_abandoned", "close"])
def test_unresolved_crash_sets_halt_and_gate_five_refuses(repo, host, recovery, case):
    status = case if case in {"pending", "abandoned"} else "sent"
    old = seed(repo, recovery, status=status, filled=False)
    if case == "raises":
        recovery.client.recover_by_rid["sent"] = [RuntimeError("broker unavailable")]
    if case == "ages_to_abandoned":
        recovery.config = replace(recovery.config, reconciliation=replace(
            recovery.config.reconciliation, non_terminal_order_sessions_before_abandoned=1))
        # One later session is enough; it need not itself be unfinished.
        later = repo.create_session("2026-10-16", "live", "demo", True)
        repo.finish_session(later.id, "finished")
    if case == "close":
        repo.conn.execute("UPDATE intents SET side='close', symbol='UPRO' WHERE session_id=?", (old.id,))
        repo.conn.commit()
    result = recovery.run(config=recovery.config)
    previous = repo.get_session(old.id)
    assert previous.state == "finished" and previous.outcome == "crashed_unresolved"
    assert result.halt_set and host.halt_flag_path().exists()
    assert result.failed_gate == 1 or result.status == "already_ran"
    gate = gate_5_database(repo=repo, session_id=result.session_id)
    assert not gate.passed and "unresolved" in gate.reason
    assert repo.unresolved_intents(old.id)[0].session_id == old.id
    if case == "ages_to_abandoned":
        assert repo.get_intent_by_request_id("sent").status == "sent"
    recovery.buy.assert_not_called()
    recovery.close.assert_not_called()
    assert recovery.client.buy_calls == recovery.client.patch_calls == []


def test_each_crashed_session_uses_its_own_intents(repo, recovery):
    good = seed(repo, recovery, rid="good", day="2026-10-13")
    bad = seed(repo, recovery, rid="bad", day="2026-10-14", filled=False)
    empty = repo.create_session("2026-10-15", "live", "demo", True)
    result = recovery.run()
    assert repo.get_session(good.id).outcome == repo.get_session(empty.id).outcome == "crashed_recovered"
    assert repo.get_session(bad.id).outcome == "crashed_unresolved"
    assert result.failed_gate == 1
    recovery.recover_buy.assert_called_once()
    recovery.recover_close.assert_called_once()


def test_dry_reports_crash_without_recovery_halt_or_old_row_writes(repo, host, recovery, caplog):
    old = seed(repo, recovery)
    intents = repo.get_intents(old.id)
    dry = replace(recovery.config, mode=TradingMode.DRY_RUN, arm_flag_present=False)
    result = recovery.run(config=dry)
    assert result.failed_gate == 5
    assert repo.get_session(old.id) == old and repo.get_intents(old.id) == intents
    assert repo.get_session(result.session_id).state == "aborted"
    assert len(repo.get_preflight_checks(result.session_id)) == 5
    assert not host.halt_flag_path().exists()
    recovery.recover_buy.assert_not_called()
    recovery.recover_close.assert_not_called()
    assert f"unfinished session ids [{old.id}]" in caplog.text
    assert recovery.client.calls == []


def test_dry_warning_failure_does_not_prevent_dedupe(repo, recovery, monkeypatch):
    old = seed(repo, recovery, day=DAY.isoformat())
    dry = replace(recovery.config, mode=TradingMode.DRY_RUN, arm_flag_present=False)
    monkeypatch.setattr(session.logging.getLogger(session.__name__), "warning",
                        Mock(side_effect=ValueError("sanitize failed")))
    result = recovery.run(config=dry)
    assert result.status == "already_ran" and result.session_id == old.id
    assert repo.get_session(old.id) == old
    recovery.recover_buy.assert_not_called()
    recovery.recover_close.assert_not_called()


def test_same_date_recovers_then_dedupes_and_notifies(repo, recovery, monkeypatch):
    old = seed(repo, recovery, day=DAY.isoformat())
    from nightwatch.notify import poll
    monkeypatch.setattr(poll, "process_pending", lambda *a: None)
    notifier = SimpleNamespace(send=Mock())
    result = recovery.run(notifier=notifier)
    assert result.status == "already_ran" and result.session_id == old.id
    assert repo.get_session(old.id).outcome == "crashed_recovered"
    assert recovery.calls == [] and repo.get_preflight_checks(old.id) == []
    assert repo.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    assert "crashed_recovered" in notifier.send.call_args.args[0]
    assert recovery.client.buy_calls == []


def test_recovery_tp_patch_remains_allowed_without_buy_or_close(repo, recovery, monkeypatch):
    from nightwatch.engine import executor
    seed(repo, recovery, day=DAY.isoformat())
    recovery.client.portfolio_queue = [[_position()], [_position(is_no_take_profit=True)]]
    original = executor.recover_unfinished
    recovery.recover_buy.side_effect = lambda **kw: original(**kw, sleep=lambda _: None)
    result = recovery.run()
    assert result.status == "already_ran"
    assert len(recovery.client.patch_calls) == 1
    assert recovery.client.buy_calls == recovery.client.violations == []
    recovery.close.assert_not_called()


def test_concurrent_run_session_is_refused_before_repo_access(repo, host, recovery, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def core(**kwargs):
        entered.set()
        assert release.wait(10)
        return session.SessionResult(None, "already_ran", None, "offline")

    monkeypatch.setattr(session, "_run_session_core", core)
    results = []
    worker = threading.Thread(target=lambda: results.append(recovery.run()))
    worker.start()
    try:
        assert entered.wait(10)
        assert recovery.run().status == "busy"
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive() and results[0].status == "already_ran"
    with session._session_lock(host) as acquired:
        assert acquired


def test_stale_lease_takeover_is_audited_and_old_owner_cannot_release(host, caplog, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(session.time, "time", lambda: clock[0])
    first = session._session_lock(host)
    assert first.__enter__()
    with session._session_lock(host) as acquired:
        assert not acquired
    clock[0] += 90 * 60
    with session._session_lock(host) as acquired:
        assert not acquired
    clock[0] += 1
    with session._session_lock(host) as acquired:
        assert acquired
        assert "session lock stale takeover" in caplog.text
        first.__exit__(None, None, None)
        with session._session_lock(host) as acquired:
            assert not acquired
    with session._session_lock(host) as acquired:
        assert acquired


def test_stale_lock_is_taken_over_by_run_session(repo, host, recovery, caplog):
    path = host.data_dir() / "engine-session.lock"
    path.write_bytes(b"\0" + json.dumps({"token": "old", "acquired_at": time.time() - 5401}).encode())
    old = seed(repo, recovery, day=DAY.isoformat())
    assert recovery.run().status == "already_ran"
    assert repo.get_session(old.id).outcome == "crashed_recovered"
    assert "session lock stale takeover" in caplog.text


def test_invalid_lock_metadata_is_preserved_and_refuses_recovery(host, recovery):
    path = host.data_dir() / "engine-session.lock"
    path.write_bytes(b"\0invalid")
    with pytest.raises(ValueError):
        recovery.run()
    assert path.read_bytes() == b"\0invalid"
    recovery.recover_buy.assert_not_called()


def test_engine_exception_releases_shared_lock(host, recovery, monkeypatch):
    def fail(**kwargs):
        raise RuntimeError("offline engine failure")

    monkeypatch.setattr(session, "_run_session_core", fail)
    with pytest.raises(RuntimeError, match="offline engine failure"):
        recovery.run()
    with session._session_lock(host) as acquired:
        assert acquired


def test_cli_run_acquires_engine_lock(repo, host, recovery, monkeypatch, capsys):
    monkeypatch.setattr(cli, "get_host", lambda: host)
    monkeypatch.setattr(cli, "load_config", lambda **kw: recovery.config)
    recovery.client.close = lambda: None
    monkeypatch.setattr(cli, "EtoroClient", lambda *a, **kw: recovery.client)
    monkeypatch.setattr(cli, "TiingoClient", lambda *a: FakeTiingo())
    monkeypatch.setattr(cli, "TelegramNotifier", lambda *a, **kw: SimpleNamespace(close=lambda: None))
    with session._session_lock(host) as acquired:
        assert acquired
        assert cli.main(["run", "--date", DAY.isoformat(), "--no-wait"]) == 1
    assert "status: busy" in capsys.readouterr().out
    assert repo.latest_session() is None


def test_gate_five_refuses_unresolved_from_a_terminal_session(repo):
    old = repo.create_session("2026-10-15", "live", "demo", True)
    repo.insert_intent(Intent(old.id, "pending", 1, "SCHG", "buy"))
    repo.finish_session(old.id, "finished")
    assert not gate_5_database(repo=repo, session_id=old.id).passed


def test_finalize_crashed_rejects_unknown_outcome(repo):
    old = repo.create_session("2026-10-15", "live", "demo", True)
    with pytest.raises(ValueError, match="invalid crash outcome"):
        repo.finalize_crashed(old.id, "finished")
    assert repo.get_session(old.id) == old
