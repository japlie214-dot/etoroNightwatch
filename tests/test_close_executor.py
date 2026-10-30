"""Offline full-close safety and restart tests against the real close ledger."""

from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from nightwatch.db.repo import Intent, LotAnnotation, Order
from nightwatch.engine.close_executor import execute_closes, recover_unfinished_closes
from nightwatch.etoro.client import PollResult
from nightwatch.etoro.models import OrderResult, OrderStatus, Position
from nightwatch.strategy.ladder import LadderIntent

NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)
POSITION = Position("p1", 42, 2.0, 50.0, 100.0, 1, True)
CLOSE = LadderIntent("UPRO", "close", position_ref="p1", book="catapult")


class FakeClient:
    def __init__(self):
        self.calls = []
        self.poll_calls = []
        self.polls = []
        self.reads = []
        self.read_count = 0
        self.send_error = None

    def close_position(self, *args, **kwargs):
        assert len(args) == 3 and not kwargs  # never units_to_deduct
        self.calls.append(args)
        assert Counter(call[0] for call in self.calls).most_common(1)[0][1] == 1
        if self.send_error:
            raise self.send_error
        return "o-" + args[1]

    def poll_order_to_terminal(self, **kwargs):
        assert kwargs["kind"] == "close"
        assert "reference_id" not in kwargs
        self.poll_calls.append(kwargs)
        value = self.polls.pop(0) if self.polls else poll()
        if isinstance(value, Exception):
            raise value
        return value

    def get_portfolio_strict(self):
        self.read_count += 1
        value = self.reads.pop(0) if self.reads else []
        if isinstance(value, Exception):
            raise value
        return value

    def get_portfolio(self):
        raise AssertionError("confirmation must use strict read")


def poll(status=OrderStatus.FILLED, timed_out=False, pid="p1"):
    return PollResult(OrderResult("o-" + pid, None, status, position_id=pid,
                                 raw={"statusID": status.value}), 1, timed_out)


@pytest.fixture
def setup(repo):
    session = repo.create_session("2026-10-24", "armed", "demo", True)
    for pid in ("p1", "p2"):
        repo.insert_lot_annotation(LotAnnotation(
            pid, "UPRO", "catapult", "2026-10-01", 100.0, "app",
        ))
    client = FakeClient()
    yield repo, session.id, client
    # Every test checks at-most-once per persisted intent, including error paths.
    counts = Counter(call[0] for call in client.calls)
    assert all(count == 1 for count in counts.values())


def run(setup, intents=(CLOSE,), **kwargs):
    repo, sid, client = setup
    params = dict(repo=repo, client=client, session_id=sid, intents=intents,
                  instrument_ids={"UPRO": 42},
                  positions_by_id={"p1": POSITION, "p2": replace(POSITION, position_id="p2")},
                  deadline=NOW + timedelta(minutes=5), now=lambda: NOW, sleep=lambda _: None)
    params.update(kwargs)
    return execute_closes(**params)


def recover(setup):
    repo, _, client = setup
    before = len(client.calls)
    report = recover_unfinished_closes(repo=repo, client=client, sleep=lambda _: None)
    assert len(client.calls) == before  # recovery never sends, even after a live send
    return report


def saved(setup, *, order=True, status="sent", side="close"):
    repo, sid, _ = setup
    iid = repo.insert_intent(Intent(sid, "request-1", 1, "UPRO", side,
                                   instrument_id=42, book="catapult", position_id="p1",
                                   units=2.0, status=status))
    oid = repo.insert_order(Order(iid, sid, "request-1", "UPRO", side,
                                 order_id="o-p1", position_id="p1")) if order else None
    return iid, oid


def test_full_close_confirms_on_second_read_and_applies_once(setup):
    repo, sid, client = setup
    client.reads = [[POSITION], []]
    sleeps = []
    result = run(setup, sleep=sleeps.append)
    assert result.results[0].outcome == "confirmed"
    assert not result.blocks_buys and result.stopped is None
    assert sleeps == [3.0] and client.read_count == 2
    assert len(client.calls) == 1
    assert not repo.get_lot_annotation("p1").is_open
    intent = repo.get_intents(sid)[0]
    assert intent.status == "filled" and intent.resolved_at
    assert intent.units == POSITION.units and intent.book == "catapult"
    obs = repo.list_close_observations()[0]
    assert obs["observed_status"] == "confirmed_full_close" and obs["applied_at"]
    assert obs["units_closed"] is None and obs["close_price"] is None
    assert '"statusID": 3' in obs["raw_json"]
    assert not recover(setup).blocks_buys
    assert len(repo.list_close_observations()) == 1


@pytest.mark.parametrize("bad", [
    replace(CLOSE, symbol="SCHG"), replace(CLOSE, side="open"),
    replace(CLOSE, amount=1.0), replace(CLOSE, position_ref=None),
    replace(CLOSE, position_ref="missing"), CLOSE,
])
def test_entire_unsupported_or_duplicate_batch_refused_without_writes(setup, bad):
    repo, sid, client = setup
    before = repo.conn.total_changes
    result = run(setup, intents=(CLOSE, bad))
    assert result.blocks_buys and result.stopped == "refused: unsupported close"
    assert all(r.outcome == "refused" and r.detail for r in result.results)
    assert repo.conn.total_changes == before
    assert repo.get_intents(sid) == [] and client.calls == [] and client.poll_calls == []


@pytest.mark.parametrize("positions, ids", [
    ({"p1": replace(POSITION, instrument_id=99)}, {"UPRO": 42}),
    ({"p1": POSITION}, {}),
    ({"p1": replace(POSITION, position_id="p2")}, {"UPRO": 42}),
])
def test_prevalidation_checks_instrument_and_identity(setup, positions, ids):
    repo, _, client = setup
    before = repo.conn.total_changes
    result = run(setup, positions_by_id=positions, instrument_ids=ids)
    assert result.results[0].outcome == "refused" and result.blocks_buys
    assert repo.conn.total_changes == before and client.calls == []


def test_send_failure_is_persisted_never_resent_and_defers_rest(setup):
    repo, sid, client = setup
    client.send_error = RuntimeError("lost response")
    second = replace(CLOSE, position_ref="p2")
    report = run(setup, intents=(CLOSE, second))
    assert report.results[0].outcome == "unresolved" and report.blocks_buys
    assert report.deferred == (second,) and len(client.calls) == 1
    intent = repo.get_intents(sid)[0]
    assert intent.status == "sent"
    order = repo.get_order_by_reference_id(intent.request_id)
    assert order.order_id is None and "close send failed" in order.error
    recovered = recover(setup)
    assert recovered.blocks_buys and "manual check" in recovered.followups[0]
    assert len(client.calls) == 1


@pytest.mark.parametrize("value", [
    RuntimeError("bad poll"), poll(OrderStatus.PLACED, True),
    poll(OrderStatus.PLACED), poll(OrderStatus.FILLED, True),
])
def test_poll_uncertainty_stays_sent_and_never_applies(setup, value):
    repo, sid, client = setup
    client.polls = [value]
    report = run(setup)
    assert report.blocks_buys and report.results[0].outcome == "unresolved"
    assert repo.get_intents(sid)[0].status == "sent"
    obs = repo.list_close_observations()[0]
    assert obs["observed_status"] == "unresolved" and not obs["applied_at"]
    assert repo.get_lot_annotation("p1").is_open and client.read_count == 0
    assert len(client.calls) == 1


@pytest.mark.parametrize("status", [OrderStatus.PARTIALLY_FILLED,
    OrderStatus.CANCELED_PARTIALLY_FILLED, OrderStatus.REJECTED_PARTIALLY_FILLED])
def test_partial_fill_is_unverified_not_a_full_close(setup, status):
    repo, sid, client = setup
    client.polls = [poll(status)]
    report = run(setup)
    assert report.results[0].outcome == "unverified" and report.blocks_buys
    assert report.followups and repo.get_intents(sid)[0].status == "sent"
    assert repo.list_close_observations()[0]["observed_status"] == "unresolved"
    assert repo.get_lot_annotation("p1").is_open and client.read_count == 0


def test_rejection_writes_no_observation_and_next_close_runs(setup):
    repo, sid, client = setup
    client.polls = [poll(OrderStatus.REJECTED), poll(pid="p2")]
    report = run(setup, intents=(CLOSE, replace(CLOSE, position_ref="p2")))
    assert [r.outcome for r in report.results] == ["rejected", "confirmed"]
    assert report.blocks_buys and report.stopped is None and len(client.calls) == 2
    assert [i.status for i in repo.get_intents(sid)] == ["rejected", "filled"]
    assert repo.list_close_observations("p1") == []
    assert repo.get_lot_annotation("p1").is_open


@pytest.mark.parametrize("reads, expected, count", [
    ([RuntimeError("read error")] * 10, "unresolved", 10),
    ([RuntimeError("read error")] * 2 + [[]], "confirmed", 3),
    ([[POSITION]] * 10, "unresolved", 10),
])
def test_strict_read_errors_never_count_as_absence(setup, reads, expected, count):
    repo, _, client = setup
    client.reads = reads
    report = run(setup)
    assert report.results[0].outcome == expected
    assert client.read_count == count and len(client.calls) == 1
    assert report.blocks_buys == (expected != "confirmed")
    assert repo.get_lot_annotation("p1").is_open == (expected != "confirmed")
    if reads == [[POSITION]] * 10:
        assert repo.list_close_observations()[0]["observed_status"] == "still_open"


def test_wrong_close_position_identity_blocks_buys(setup):
    repo, _, client = setup
    report = run(setup, intents=(replace(CLOSE, position_ref="p3"),),
                 positions_by_id={"p3": replace(POSITION, position_id="p3")})
    # Default fake fill names p1; use a matching full-close response instead.
    assert report.blocks_buys and "mismatch" in report.results[0].detail
    assert len(client.calls) == 1


def test_confirmed_at_broker_but_no_annotation_stays_unresolved(setup):
    repo, _, client = setup
    client.polls = [poll(pid="p3")]
    report = run(setup, intents=(replace(CLOSE, position_ref="p3"),),
                 positions_by_id={"p3": replace(POSITION, position_id="p3")})
    assert report.results[0].outcome == "unresolved" and report.blocks_buys
    assert "no open sidecar" in report.results[0].detail
    assert repo.unapplied_confirmed_closes()


def test_already_applied_is_unverified_not_fresh_confirmation(setup):
    repo, sid, client = setup
    assert run(setup).results[0].outcome == "confirmed"
    report = run(setup)
    assert report.results[0].outcome == "unverified" and report.blocks_buys
    assert report.followups == ("close already applied for position p1",)
    assert [i.status for i in repo.get_intents(sid)] == ["filled", "sent"]
    assert sum(bool(o["applied_at"]) for o in repo.list_close_observations()) == 1
    assert len(client.calls) == 2  # distinct persisted intents, neither resent


def test_deadline_before_first_close_writes_nothing(setup):
    repo, _, client = setup
    before = repo.conn.total_changes
    report = run(setup, deadline=NOW)
    assert report.deferred == (CLOSE,) and report.blocks_buys
    assert report.stopped == "past deadline" and not report.results
    assert repo.conn.total_changes == before and client.calls == []


def test_deadline_before_second_close_defers_it(setup):
    repo, _, client = setup
    # Cross after the first result has been applied, at the next now() check.
    calls = [0]
    def clock():
        calls[0] += 1
        return NOW if calls[0] <= 6 else NOW + timedelta(minutes=10)

    report = run(setup, intents=(CLOSE, replace(CLOSE, position_ref="p2")), now=clock)
    assert report.results[0].outcome == "confirmed"
    assert report.deferred == (replace(CLOSE, position_ref="p2"),)
    assert report.blocks_buys and len(client.calls) == 1


@pytest.mark.parametrize("phase", ["poll", "read", "sleep"])
def test_deadline_mid_close_stays_sent_without_more_reads(setup, phase):
    repo, sid, client = setup
    current = [NOW]
    end = NOW + timedelta(seconds=1)
    def expire(*args, **kwargs):
        current[0] = end
        return poll()
    if phase == "poll":
        client.poll_order_to_terminal = expire
    elif phase == "read":
        def read():
            current[0] = end
            return []
        client.get_portfolio_strict = read
    else:
        client.reads = [[POSITION]]
    report = run(setup, deadline=end, now=lambda: current[0],
                 sleep=lambda _: current.__setitem__(0, end))
    assert report.results[0].outcome == "unresolved" and report.blocks_buys
    assert repo.get_intents(sid)[0].status == "sent"
    assert repo.get_lot_annotation("p1").is_open
    assert all(not o["applied_at"] for o in repo.list_close_observations())
    assert len(client.calls) == 1


def test_deadline_after_send_skips_poll(setup):
    repo, sid, client = setup
    current = [NOW]
    send = client.close_position
    def close(*args):
        result = send(*args)
        current[0] = NOW + timedelta(minutes=10)
        return result
    client.close_position = close
    report = run(setup, now=lambda: current[0])
    assert report.blocks_buys and client.poll_calls == []
    assert repo.get_intents(sid)[0].status == "sent" and len(client.calls) == 1


def test_recovery_applies_persisted_observation_and_is_idempotent(setup):
    repo, sid, client = setup
    iid, oid = saved(setup)
    repo.record_close_observation(intent_id=iid, order_row_id=oid, order_id="o-p1",
                                 position_id="p1", observed_status="confirmed_full_close")
    first = recover(setup)
    assert first.resolved and not first.blocks_buys
    assert repo.get_intents(sid)[0].status == "filled"
    assert not repo.get_lot_annotation("p1").is_open
    assert recover(setup).resolved == ()
    assert client.calls == [] and client.poll_calls == []


def test_recovery_sent_order_confirms_without_sending(setup):
    repo, sid, client = setup
    saved(setup)
    report = recover(setup)
    assert report.resolved and not report.blocks_buys
    assert repo.get_intents(sid)[0].status == "filled"
    assert not repo.get_lot_annotation("p1").is_open and client.calls == []


@pytest.mark.parametrize("pending", ["pending", "sent"])
def test_recovery_missing_order_does_not_lookup_reference_or_send(setup, pending):
    repo, sid, client = setup
    saved(setup, order=False, status=pending)
    report = recover(setup)
    assert report.blocks_buys and report.still_unresolved
    assert "close reference lookup unverified; manual check" in report.followups[0]
    assert repo.get_intents(sid)[0].status == pending
    assert client.calls == [] and client.poll_calls == []


@pytest.mark.parametrize("value", [RuntimeError("poll failed"),
    poll(OrderStatus.PLACED, True), poll(OrderStatus.PLACED),
    poll(OrderStatus.REJECTED), poll(OrderStatus.REJECTED_PARTIALLY_FILLED)])
def test_recovery_uncertain_or_rejected_blocks_buys_without_sending(setup, value):
    repo, sid, client = setup
    saved(setup)
    client.polls = [value]
    report = recover(setup)
    assert report.blocks_buys and report.still_unresolved and client.calls == []
    rejected = not isinstance(value, Exception) and value.order.status == OrderStatus.REJECTED
    assert repo.get_intents(sid)[0].status == ("rejected" if rejected else "sent")
    assert repo.get_lot_annotation("p1").is_open
    assert all(not o["applied_at"] for o in repo.list_close_observations())
    if rejected:
        assert repo.list_close_observations() == []


def test_recovery_missing_sidecar_keeps_durable_evidence(setup):
    repo, _, client = setup
    iid, oid = saved(setup)
    repo.record_close_observation(intent_id=iid, order_row_id=oid, order_id="o-p1",
                                 position_id="missing", observed_status="confirmed_full_close")
    report = recover(setup)
    assert report.blocks_buys and "no open sidecar" in report.followups[0]
    assert client.calls == [] and client.poll_calls == []
    assert len(repo.unapplied_confirmed_closes()) == 1


@pytest.mark.parametrize("reads, confirmed", [
    ([RuntimeError("read error")] * 10, False),
    ([[POSITION]] * 10, False),
    ([RuntimeError("read error"), []], True),
])
def test_recovery_uses_same_strict_confirmation_without_sending(setup, reads, confirmed):
    repo, sid, client = setup
    saved(setup)
    client.reads = reads
    report = recover(setup)
    assert report.blocks_buys != confirmed
    assert repo.get_lot_annotation("p1").is_open != confirmed
    assert repo.get_intents(sid)[0].status == ("filled" if confirmed else "sent")
    assert client.calls == []


def test_recovery_already_applied_evidence_is_not_fresh_confirmation(setup):
    repo, sid, client = setup
    iid, oid = saved(setup)
    first = repo.record_close_observation(
        intent_id=iid, order_row_id=oid, order_id="o-p1", position_id="p1",
        observed_status="confirmed_full_close",
    )
    assert repo.apply_confirmed_close(first)
    repo.record_close_observation(
        intent_id=iid, order_row_id=oid, order_id="o-p1", position_id="p1",
        observed_status="confirmed_full_close",
    )
    report = recover(setup)
    assert report.blocks_buys and report.resolved == ()
    assert report.followups == ("close already applied for position p1",)
    assert repo.get_intents(sid)[0].status == "sent"
    assert client.calls == [] and client.poll_calls == []


def test_intent_is_durable_and_sent_before_close_call(setup):
    repo, sid, client = setup
    send = client.close_position
    def close(request_id, pid, instrument_id):
        intent = repo.get_intent_by_request_id(request_id)
        assert intent.status == "sent" and intent.position_id == pid
        assert intent.instrument_id == instrument_id and not repo.conn.in_transaction
        return send(request_id, pid, instrument_id)
    client.close_position = close
    assert run(setup).results[0].outcome == "confirmed"
    assert len(client.calls) == 1 and repo.get_intents(sid)[0].status == "filled"


@pytest.mark.parametrize("state", ["pending", "unresolved", "unverified", "confirmed", "orphan", "no_order"])
def test_schg_recovery_is_ledger_only_and_never_sends(setup, state):
    repo, sid, client = setup
    intent = Intent(sid, "schg-request", 1, "SCHG", "close", instrument_id=42,
                    units=2, position_id="100")
    if state == "orphan":
        iid = repo.insert_intent(intent)
    else:
        iid, lid = repo.begin_schg_close_leg(intent, position_id="100", units_before=4, units_deducted=2)
        if state != "no_order":
            oid = repo.insert_order(Order(iid, sid, "schg-request", "SCHG", "close",
                                          order_id="900", position_id="100"))
            repo.set_partial_close_order(lid, oid, "900")
        if state in ("confirmed", "unresolved", "unverified"):
            # Legacy split-write crash: evidence is final but intent remains sent.
            repo.update_partial_close(lid, status=state, units_after=2)
    lookups = []
    def lookup(order_id, **kwargs):
        lookups.append((order_id, kwargs))
        return OrderResult("900", None, OrderStatus.FILLED, position_id="100", fill_units=2,
                           raw={"positions": [{"positionID": "101", "units": 2},
                                              {"positionID": "100"}]})
    client.lookup_close_order = lookup
    client.reads = [[replace(POSITION, position_id="100", units=2)]]
    report = recover(setup)
    saved_intent = repo.get_intent_by_request_id("schg-request")
    assert not client.calls and not client.poll_calls  # no UPRO finisher or sidecar
    if state in ("orphan", "no_order"):
        assert report.blocks_buys and not lookups and client.read_count == 0
        assert saved_intent.status in ("pending", "sent") and not saved_intent.resolved_at
        assert ("without ledger" if state == "orphan" else "send unverified") in report.followups[0]
    else:
        assert not report.blocks_buys and saved_intent.status == "filled" and saved_intent.resolved_at
        assert len(lookups) == (0 if state == "confirmed" else 1)
        assert repo.confirmed_partial_closes(["100"])[0]["units_after"] == 2
        assert not recover(setup).blocks_buys
        assert len(lookups) == (0 if state == "confirmed" else 1)
    assert not repo.list_close_observations()


def test_recovery_ignores_pending_buys(setup):
    _, _, client = setup
    saved(setup, side="buy")
    report = recover(setup)
    assert not report.blocks_buys and not report.resolved
    assert client.calls == [] and client.poll_calls == []
