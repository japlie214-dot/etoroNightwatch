"""T052: offline bounds, deadline pauses and durable recovery across runs."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nightwatch.db.repo import Order
from nightwatch.engine import executor, session
from nightwatch.etoro.client import EtoroClient
from nightwatch.etoro.errors import EtoroTimeoutError
from nightwatch.etoro.models import OrderResult, OrderStatus
from . import test_close_executor as closes
from . import test_executor as buys
from . import test_executor_recovery as recovery
from . import test_schg_sell as schg
from .test_session_armed import armed
from .test_session_decide import config, no_network


class Timer:
    def __init__(self):
        self.current = 0.0
        self.sleeps = []

    def clock(self):
        return self.current

    def sleep(self, seconds):
        assert seconds > 0
        self.sleeps.append(seconds)
        self.current += seconds


@pytest.fixture
def polling():
    timer = Timer()
    # Only the polling/deadline state is needed; no HTTP client is constructed.
    client = object.__new__(EtoroClient)
    client._clock = timer.clock
    client._sleep = timer.sleep
    client._deadline = None
    client._poll_interval = 2.0
    client._poll_timeout = 5.0
    state = SimpleNamespace(client=client, timer=timer, reads=[], fill_at=float("inf"))

    def lookup(*args, **kwargs):
        state.reads.append(timer.current)
        client._check_deadline()
        if timer.current >= state.fill_at:
            return buys._filled()
        return OrderResult("O1", None, OrderStatus.WAITING_FOR_MARKET)

    client.lookup_order = lookup
    client.lookup_close_order = lookup
    return state


def test_deadline_accessor_is_read_only_and_has_no_capacity_or_transport_effect(polling):
    p = polling
    assert not p.client.deadline_expired()
    assert p.client.deadline_remaining() == float("inf")
    p.client.set_deadline_in(5)
    p.timer.current = 4.999
    assert not p.client.deadline_expired()
    assert p.client.deadline_remaining() == pytest.approx(0.001)
    p.timer.current = 5
    assert p.client.deadline_expired()
    assert p.client.deadline_expired()
    assert p.client._deadline == 5
    assert p.client.deadline_remaining() == 0
    p.timer.current = 6
    assert p.client.deadline_remaining() == 0
    p.client.set_deadline_in(None)
    assert not p.client.deadline_expired()
    assert p.client.deadline_remaining() == float("inf")
    assert p.reads == p.timer.sleeps == []


@pytest.mark.parametrize("kind", ["open", "close"])
@pytest.mark.parametrize("timeout, sleeps, reads", [
    (5, [2, 2, 1], [0, 2, 4, 5]),
    (0, [], [0]),
    (1, [1], [0, 1]),
])
def test_poll_clamps_own_timeout_and_reads_at_endpoint(polling, kind, timeout, sleeps, reads):
    p = polling
    result = p.client.poll_order_to_terminal(order_id="O1", kind=kind, timeout_seconds=timeout)
    assert result.timed_out and not result.order.is_terminal
    assert result.attempts == len(reads)
    assert p.timer.sleeps == sleeps and p.reads == reads
    assert p.timer.current == timeout


@pytest.mark.parametrize("fill_at", [0, 4, 5])
def test_poll_observes_terminal_status_including_final_endpoint(polling, fill_at):
    p = polling
    p.fill_at = fill_at
    result = p.client.poll_order_to_terminal(order_id="O1")
    assert not result.timed_out and result.order.is_filled
    assert p.reads[-1] == fill_at
    assert p.timer.sleeps == ([2, 2, 1] if fill_at == 5 else [2] * (fill_at // 2))


@pytest.mark.parametrize("kind", ["open", "close"])
def test_poll_clamps_session_deadline_and_attempts_endpoint(polling, kind):
    p = polling
    p.client.set_deadline_in(3)
    with pytest.raises(EtoroTimeoutError, match="^deadline$"):
        p.client.poll_order_to_terminal(order_id="O1", kind=kind)
    assert p.reads == [0, 2, 3] and p.timer.sleeps == [2, 1]
    assert p.timer.current == 3


@pytest.mark.parametrize("phase", ["before", "sleep", "read"])
def test_read_position_pauses_without_more_reads_or_sleeps(repo, phase):
    timer = Timer()
    client = buys.FakeExecClient(repo)
    client.deadline_expired = lambda: timer.current >= (0 if phase == "before" else 1)
    client.deadline_remaining = lambda: max(0, (0 if phase == "before" else 1) - timer.current)

    def read():
        client.calls.append("get_portfolio")
        if phase == "read":
            timer.current = 1
            raise EtoroTimeoutError("deadline")
        return []

    client.get_portfolio = read
    assert executor._read_position(client, "P1", timer.sleep) is None
    assert client.calls.count("get_portfolio") == (0 if phase == "before" else 1)
    assert timer.sleeps == ([1] if phase == "sleep" else [])


@pytest.mark.parametrize("remaining", [0, 0.25, 1, 3, 4])
@pytest.mark.parametrize("position_visible", [False, True])
def test_recovery_position_and_tp_sleeps_stay_within_client_deadline(
    repo, polling, remaining, position_visible,
):
    p = polling
    p.client.set_deadline_in(remaining)
    recovery._seed_intent(repo, rid="bounded-fill")
    client = recovery.FakeRecoveryClient(repo)
    client.deadline_expired = p.client.deadline_expired
    client.deadline_remaining = p.client.deadline_remaining
    client.recover_by_rid["bounded-fill"] = [recovery._filled()]
    client.portfolio_queue = [[recovery._position()]] if position_visible else [[]]
    result = executor.recover_unfinished(repo=repo, client=client,
                                         abandon_after_sessions=3, sleep=p.timer.sleep)
    assert p.timer.current == remaining
    assert sum(p.timer.sleeps) == remaining
    assert all(seconds > 0 for seconds in p.timer.sleeps)
    assert not client.buy_calls
    assert result.tp_uncleared == (("P1",) if position_visible and remaining > 0 else ())
    assert client.calls.count("get_portfolio") == (0 if remaining == 0 else
                                                  1 if remaining <= 3 else 2)
    assert len(client.patch_calls) == int(position_visible and remaining > 0)


def test_read_position_exhaustion_has_no_final_sleep(repo):
    client = buys.FakeExecClient(repo)
    client.portfolio_queue = [[]]
    timer = Timer()
    assert executor._read_position(client, "P1", timer.sleep) is None
    assert client.calls.count("get_portfolio") == 10
    assert timer.sleeps == [3] * 9 and timer.current == 27


@pytest.mark.parametrize("phase", ["before_attempt", "after_patch", "sleep", "read", "next_attempt"])
def test_tp_deadline_records_fixed_failure_without_further_work(repo, phase):
    iid = recovery._seed_intent(repo, rid="tp-deadline")
    sid = repo.get_intent_by_request_id("tp-deadline").session_id
    oid = repo.insert_order(Order(iid, sid, "tp-deadline", "SCHG", "buy", order_id="O1"))
    client = buys.FakeExecClient(repo)
    timer = Timer()
    client.deadline_expired = lambda: timer.current >= (0 if phase == "before_attempt" else 1)
    client.deadline_remaining = lambda: max(0, (0 if phase == "before_attempt" else 1) - timer.current)
    patch = client.clear_take_profit

    def clear(*args):
        ack = patch(*args)
        if phase in ("after_patch", "next_attempt"):
            timer.current = 1
        if phase == "next_attempt":
            raise EtoroTimeoutError("deadline")
        return ack

    def read():
        client.calls.append("get_portfolio")
        timer.current = 1
        raise EtoroTimeoutError("deadline")

    def sleep(seconds):
        timer.sleep(seconds)
        if phase == "read":
            timer.current = 0.5

    client.clear_take_profit = clear
    client.get_portfolio = read
    assert not executor._clear_take_profit(
        repo=repo, client=client, order_row_id=oid, position=buys._position(), sleep=sleep,
    )
    rows = repo.list_tp_clears("P1")
    assert all(r["status"] == "failed" for r in rows)
    attempt = phase in ("before_attempt", "next_attempt")
    assert rows[-1]["detail"] == ("deadline: attempt not started" if attempt else
                                   "deadline: read-back not attempted")
    assert len(rows) == (2 if phase == "next_attempt" else 1)
    assert len(client.patch_calls) == (0 if phase == "before_attempt" else 1)
    assert client.calls.count("get_portfolio") == (1 if phase == "read" else 0)
    assert timer.sleeps == ([1] if phase in ("sleep", "read") else [])


def test_fresh_filled_buy_unreadable_at_deadline_keeps_t056_unresolved(repo):
    sid = repo.create_session("2026-10-16", "live", "demo", True).id
    client = buys.FakeExecClient(repo)
    client.deadline_expired = lambda: True
    sleeps = []
    report = buys._run(repo, client, sid, sleeps, [buys._intent()])
    saved, = repo.get_intents(sid)
    assert saved.status == "filled_unannotated" and saved.resolved_at is None
    assert repo.reserved_buy_usd("2026-10-16") == pytest.approx(633.33)
    assert report.tp_failed == ("P1",) and not report.tp_uncleared
    assert client.calls.count("get_portfolio") == 0 and not client.patch_calls and not sleeps


def test_threshold_aged_sent_buy_survives_clamped_wait_then_recovers(repo, polling):
    p = polling
    recovery._seed_intent(repo, rid="late-fill")
    recovery._later_sessions(repo, [recovery.TODAY, "2026-10-17", "2026-10-18"])
    client = recovery.FakeRecoveryClient(repo)
    client.recover_by_reference_id = lambda rid: p.client.lookup_order(reference_id=rid)
    client.poll_order_to_terminal = p.client.poll_order_to_terminal
    p.fill_at = 5.5
    first = recovery._recover(repo, client, [])
    saved = repo.get_intent_by_request_id("late-fill")
    assert saved.status == "sent" and saved.resolved_at is None and saved.sent_at is not None
    assert first.still_unresolved and not first.abandoned and not first.resolved
    assert p.reads == [0, 0, 2, 4, 5] and p.timer.sleeps == [2, 2, 1]
    assert repo.get_lot_annotation("P1") is None
    assert repo.reserved_buy_usd(recovery.DAY) == pytest.approx(633.33)
    p.timer.current = 6
    second = recovery._recover(repo, client, [])
    assert second.resolved and not second.abandoned and not second.still_unresolved
    assert repo.get_intent_by_request_id("late-fill").status == "recovered"
    assert repo.get_lot_annotation("P1") is not None and not client.buy_calls


def test_threshold_aged_sent_buy_deadline_error_stays_unresolved(repo):
    recovery._seed_intent(repo, rid="expired-poll")
    recovery._later_sessions(repo, [recovery.TODAY, "2026-10-17", "2026-10-18"])
    client = recovery.FakeRecoveryClient(repo)
    client.recover_by_rid["expired-poll"] = [OrderResult("O1", None, OrderStatus.PLACED)]
    client.poll_queue = [EtoroTimeoutError("deadline")]
    result = recovery._recover(repo, client, [])
    assert result.still_unresolved and not result.abandoned
    saved = repo.get_intent_by_request_id("expired-poll")
    assert saved.status == "sent" and saved.resolved_at is None


def test_unsent_pending_intent_retains_existing_age_abandonment(repo):
    recovery._seed_intent(repo, rid="unsent", status="pending")
    recovery._later_sessions(repo, [recovery.TODAY, "2026-10-17", "2026-10-18"])
    client = recovery.FakeRecoveryClient(repo)
    result = recovery._recover(repo, client, [])
    assert result.abandoned and not result.still_unresolved
    assert repo.get_intent_by_request_id("unsent").status == "abandoned"


@pytest.mark.parametrize("phase", ["poll", "before_read", "read", "read_error", "sleep"])
def test_full_close_recovery_deadline_never_confirms_or_resends(repo, phase):
    sid = repo.create_session("2026-10-24", "armed", "demo", True).id
    client = closes.FakeClient()
    setup = (repo, sid, client)
    iid, _ = closes.saved(setup)
    timer = Timer()
    client.deadline_expired = lambda: timer.current >= 1
    client.deadline_remaining = lambda: max(0, 1 - timer.current)
    poll = client.poll_order_to_terminal

    def polling(**kwargs):
        if phase == "poll":
            raise EtoroTimeoutError("deadline")
        result = poll(**kwargs)
        if phase == "before_read":
            timer.current = 1
        return result

    def read():
        client.read_count += 1
        if phase in ("read", "read_error"):
            timer.current = 1
        if phase == "read_error":
            raise EtoroTimeoutError("deadline")
        return [] if phase == "read" else [closes.POSITION]

    client.poll_order_to_terminal = polling
    client.get_portfolio_strict = read
    report = closes.recover_unfinished_closes(repo=repo, client=client, sleep=timer.sleep)
    detail = ("close poll failed: EtoroTimeoutError: deadline" if phase == "poll" else
              "deadline: confirmation paused")
    assert report.still_unresolved == (f"intent {iid}: {detail}",)
    assert report.blocks_buys and not report.resolved and not client.calls
    assert client.read_count == (0 if phase in ("poll", "before_read") else 1)
    assert timer.sleeps == ([1] if phase == "sleep" else [])
    saved = repo.get_intents(sid)[0]
    assert saved.status == "sent" and saved.resolved_at is None
    assert all(not row["applied_at"] for row in repo.list_close_observations())


def test_full_close_durable_evidence_applies_even_with_expired_client_deadline(repo):
    from nightwatch.db.repo import LotAnnotation

    sid = repo.create_session("2026-10-24", "armed", "demo", True).id
    client = closes.FakeClient()
    client.deadline_expired = lambda: True
    iid, oid = closes.saved((repo, sid, client))
    repo.insert_lot_annotation(LotAnnotation("p1", "UPRO", "catapult", "2026-10-01", 100, "app"))
    repo.record_close_observation(
        intent_id=iid, order_row_id=oid, order_id="o-p1", position_id="p1",
        observed_status="confirmed_full_close",
    )
    result = closes.recover((repo, sid, client))
    assert result.resolved and not result.blocks_buys
    assert repo.get_intents(sid)[0].status == "filled"
    assert not repo.get_lot_annotation("p1").is_open
    assert client.calls == client.poll_calls == [] and client.read_count == 0


@pytest.mark.parametrize("phase", ["before_read", "read", "read_error", "sleep"])
def test_schg_portfolio_deadline_exits_unresolved(repo, phase):
    sid = repo.create_session("2026-10-30", "armed", "demo", True).id
    client = schg.Client(repo, sid)
    clock = schg.Clock()
    expired = [False]
    client.deadline_expired = lambda: expired[0]
    client.deadline_remaining = lambda: 0 if expired[0] else 1
    lookup = client.lookup_close_order

    def poll(*args, **kwargs):
        result = lookup(*args, **kwargs)
        expired[0] = phase == "before_read"
        return result

    def read():
        client.read_count += 1
        expired[0] = phase in ("read", "read_error")
        if phase == "read_error":
            raise EtoroTimeoutError("deadline")
        return [replace(schg.POSITION, units=2)] if phase == "read" else [schg.POSITION]

    def sleep(seconds):
        clock.sleep(seconds)
        expired[0] = True

    client.lookup_close_order = poll
    client.get_portfolio_strict = read
    report = schg.run((repo, sid, client, clock), sleep=sleep)
    assert report.results[0].outcome == "unresolved"
    assert report.results[0].detail == "deadline: confirmation paused"
    assert report.blocks_buys and len(client.calls) == len(client.lookups) == 1
    assert client.read_count == (0 if phase == "before_read" else 1)
    assert clock.sleeps == ([1] if phase == "sleep" else [])
    saved = repo.get_intents(sid)[0]
    assert saved.status == "sent" and saved.resolved_at is None
    assert schg.ledger(repo)["status"] == "unresolved"


@pytest.mark.parametrize("remaining", [0.25, 1, 3])
def test_schg_lookup_sleep_clamps_client_deadline_and_preserves_error_handling(repo, polling, remaining):
    from nightwatch.etoro.errors import EtoroValidationError

    p = polling
    p.client.set_deadline_in(remaining)
    sid = repo.create_session("2026-10-30", "armed", "demo", True).id
    client = schg.Client(repo, sid)
    client.deadline_expired = p.client.deadline_expired
    client.deadline_remaining = p.client.deadline_remaining
    clock = schg.Clock()
    clock.now = lambda: schg.NOW + timedelta(seconds=p.timer.current)
    clock.sleep = p.timer.sleep

    def lookup(*args, **kwargs):
        p.reads.append(p.timer.current)
        p.client._check_deadline()
        raise EtoroValidationError("not visible", status_code=404)

    client.lookup_close_order = lookup
    result = schg.run((repo, sid, client, clock))
    assert result.results[0].outcome == "unresolved" and result.blocks_buys
    assert result.results[0].detail == "close lookup failed: EtoroTimeoutError: deadline"
    assert p.timer.current == remaining and p.reads[-1] == remaining
    assert p.timer.sleeps == ([2, 1] if remaining == 3 else [remaining])
    assert len(client.calls) == 1 and client.read_count == 0
    assert repo.get_intents(sid)[0].resolved_at is None


@pytest.mark.parametrize("symbol", ["UPRO", "SCHG"])
@pytest.mark.parametrize("remaining", [0.25, 1, 3, 4])
def test_close_confirmation_sleep_clamps_real_client_deadline(repo, polling, symbol, remaining):
    p = polling
    p.client.set_deadline_in(remaining)
    sid = repo.create_session("2026-10-30", "armed", "demo", True).id
    if symbol == "UPRO":
        client = closes.FakeClient()
        closes.saved((repo, sid, client))
        client.get_portfolio_strict = lambda: [closes.POSITION]
    else:
        client = schg.Client(repo, sid)
        client.default_read = [schg.POSITION]
    client.deadline_expired = p.client.deadline_expired
    client.deadline_remaining = p.client.deadline_remaining
    if symbol == "UPRO":
        report = closes.recover_unfinished_closes(repo=repo, client=client, sleep=p.timer.sleep)
        assert "deadline: confirmation paused" in report.still_unresolved[0]
        assert not client.calls
    else:
        clock = schg.Clock()
        clock.now = lambda: schg.NOW + timedelta(seconds=p.timer.current)
        clock.sleep = p.timer.sleep
        report = schg.run((repo, sid, client, clock))
        assert report.results[0].detail == "deadline: confirmation paused"
        assert len(client.calls) == 1
    assert report.blocks_buys
    assert p.timer.current == remaining and sum(p.timer.sleeps) == remaining
    assert p.timer.sleeps == ([3, 1] if remaining == 4 else [remaining])
    assert repo.get_intents(sid)[0].resolved_at is None


@pytest.mark.parametrize("failure", ["tp", "position"])
def test_session_deadline_outcomes_preserve_alert_only_and_t056_halt(
    armed, repo, host, monkeypatch, failure,
):
    from nightwatch.notify import poll
    from .test_session_market import DAY, IDS, FakeTiingo

    client = buys.FakeExecClient(repo)
    client.portfolio_queue = [[replace(buys._position(), instrument_id=IDS["SCHG"])]]
    timer = Timer()
    client.deadline_expired = lambda: failure == "position" or timer.current >= 1
    client.deadline_remaining = lambda: max(0, 1 - timer.current)
    armed.execute.side_effect = lambda **kw: executor.execute_buys(
        **{**kw, "client": client, "sleep": timer.sleep})
    monkeypatch.setattr(poll, "process_pending", lambda *args: None)
    notifier = SimpleNamespace(send=Mock())
    result = session.run_session(
        host=host, config=armed.config, repo=repo, client=armed.client,
        tiingo=FakeTiingo(), trading_date=DAY, wait=False, notifier=notifier,
    )
    tp = failure == "tp"
    assert result.halt_set == host.halt_flag_path().exists() == (not tp)
    assert result.tp_failed == (not tp)
    assert result.tp_uncleared_positions == (("P1",) if tp else ())
    assert armed.save.call_count == int(tp)
    saved, = repo.get_intents(result.session_id)
    assert saved.status == ("filled" if tp else "filled_unannotated")
    assert (saved.resolved_at is not None) == tp
    assert len(client.patch_calls) == int(tp)
    assert client.calls.count("get_portfolio") == int(tp)
    assert timer.sleeps == ([1] if tp else [])
    if tp:
        assert repo.list_tp_clears()[0]["detail"] == "deadline: read-back not attempted"
        alert, = [c.args[0] for c in notifier.send.call_args_list
                  if c.kwargs["event"] == "tp_not_cleared"]
        assert "session continued" in alert and "HALT" not in alert
        before = repo.list_tp_clears()
        recovered = executor.recover_unfinished(repo=repo, client=client,
                                               abandon_after_sessions=3, sleep=timer.sleep)
        assert repo.list_tp_clears() == before and len(client.patch_calls) == 1
        assert not recovered.tp_uncleared
    else:
        armed.save.assert_not_called()
        assert repo.get_lot_annotation("P1") is None
