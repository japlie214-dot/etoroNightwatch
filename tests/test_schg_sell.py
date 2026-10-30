"""Offline SCHG leg planning, at-most-once sends and bounded confirmation."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from nightwatch.engine.close_executor import execute_schg_sell, plan_schg_sell
from nightwatch.etoro.errors import EtoroValidationError
from nightwatch.etoro.models import OrderResult, OrderStatus, Position
from nightwatch.strategy.ladder import LadderIntent

NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)
POSITION = Position("100", 42, 4.0, 30.0, 120.0, 1, True)
SELL = LadderIntent("SCHG", "close", amount=20.0, reason="fund ladder buy")


class Clock:
    def __init__(self):
        self.current = NOW
        self.sleeps = []

    def now(self):
        return self.current

    def sleep(self, seconds):
        assert seconds > 0
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)


def fill(pid="100", units=2.0, status=OrderStatus.FILLED, partial=True):
    return OrderResult("900", None, status, position_id=pid, fill_units=units,
                       fill_price=10.0, fill_amount_usd=20.0,
                       raw={"positions": ([{"positionID": "101", "units": units},
                                            {"positionID": pid}] if partial else [{"positionID": pid}])})


class Client:
    def __init__(self, repo, sid):
        self.repo, self.sid = repo, sid
        self.calls, self.lookups, self.read_count = [], [], 0
        self.polls = []
        self.reads = []
        self.default_poll = fill()
        self.default_read = [replace(POSITION, units=2.0)]
        self.send_error = None

    def close_position(self, request_id, pid, instrument_id, deducted):
        saved = self.repo.get_intent_by_request_id(request_id)
        rows = self.repo.pending_partial_closes()
        assert saved.status == "sent" and saved.position_id == pid
        assert any(r["intent_id"] == saved.id and r["status"] == "pending" for r in rows)
        assert not self.repo.conn.in_transaction
        self.calls.append((request_id, pid, instrument_id, deducted))
        assert len({c[0] for c in self.calls}) == len(self.calls)
        if self.send_error:
            raise self.send_error
        return "900"

    def lookup_close_order(self, order_id, **kwargs):
        self.lookups.append((order_id, kwargs))
        value = self.polls.pop(0) if self.polls else self.default_poll
        if isinstance(value, Exception):
            raise value
        return value

    def get_portfolio_strict(self):
        self.read_count += 1
        value = self.reads.pop(0) if self.reads else self.default_read
        if isinstance(value, Exception):
            raise value
        return value

    def recover_by_reference_id(self, *args, **kwargs):
        raise AssertionError("never recover close by reference")


@pytest.fixture
def setup(repo):
    sid = repo.create_session("2026-10-30", "armed", "demo", True).id
    return repo, sid, Client(repo, sid), Clock()


def run(setup, **kwargs):
    repo, sid, client, clock = setup
    params = dict(repo=repo, client=client, session_id=sid, intent=SELL,
                  instrument_id=42, positions=[POSITION], price=10.0,
                  deadline=NOW + timedelta(minutes=5), now=clock.now, sleep=clock.sleep)
    params.update(kwargs)
    return execute_schg_sell(**params)


def ledger(repo):
    return dict(repo.conn.execute("SELECT * FROM partial_close_effects ORDER BY id").fetchone())


def test_plan_orders_by_cost_ties_by_id_and_uses_market_not_cost():
    positions = [replace(POSITION, position_id="c", entry_price=10),
                 replace(POSITION, position_id="b", units=1),
                 replace(POSITION, position_id="a", units=1)]
    plan = plan_schg_sell(positions, amount=35, price=10, instrument_id=42)
    assert plan.legs == (("a", 1, None), ("b", 1, None), ("c", 4, 1.5))
    assert plan.planned_value == 35 and plan.shortfall == 0


@pytest.mark.parametrize("allow, amount, units, legs, shortfall, note", [
    (True, 15, 2, (("100", 2, None),), 0, "overshoot"),
    (False, 15, 2, (), 15, "disabled"),
    (True, 5, 4, (), 5, "below minimum"),
    (False, 25, 2, (("100", 2, None),), 5, "shortfall"),
])
def test_plan_gates_and_overshoot(allow, amount, units, legs, shortfall, note):
    plan = plan_schg_sell([replace(POSITION, units=units)], amount=amount, price=10,
                          instrument_id=42, allow_partial=allow)
    assert plan.legs == legs and plan.shortfall == shortfall
    assert note in " ".join(plan.notes)


def test_plan_floors_units_and_filters_candidates():
    plan = plan_schg_sell([POSITION, replace(POSITION, instrument_id=99),
                           replace(POSITION, units=0)], amount=15.1234567, price=10, instrument_id=42)
    assert plan.legs == (("100", 4, 1.512345),)
    assert plan.planned_value <= 15.1234567


@pytest.mark.parametrize("amount, price, positions", [(0, 10, [POSITION]), (-1, 10, [POSITION]),
    (10, 0, [POSITION]), (10, -1, [POSITION]), (10, 10, [])])
def test_empty_plans_have_a_reason(amount, price, positions):
    plan = plan_schg_sell(positions, amount=amount, price=price, instrument_id=42)
    assert not plan.legs and plan.notes


def test_durable_send_404_lag_then_reduced_units_confirms(setup):
    repo, sid, client, clock = setup
    client.polls = [EtoroValidationError("not visible", status_code=404)] * 2 + [fill()]
    client.reads = [[], [], [], [POSITION], [replace(POSITION, units=2.0)]]
    report = run(setup)
    assert not report.blocks_buys and report.results[0].outcome == "confirmed"
    assert len(client.calls) == 1 and client.read_count == 5
    assert clock.sleeps == [2, 2, 3, 3, 3, 3]
    assert all(k == {"expected_position_id": "100"} for _, k in client.lookups)
    row = ledger(repo)
    assert row["units_after"] == 2 and row["slice_position_id"] == "101"
    assert row["close_price"] == 10 and row["proceeds"] == 20 and row["confirmed_at"]
    assert repo.get_intents(sid)[0].status == "filled"
    assert repo.confirmed_partial_closes(["100"]) == [row]
    assert repo.confirmed_partial_closes([]) == []


@pytest.mark.parametrize("status", [OrderStatus.PARTIALLY_FILLED,
    OrderStatus.CANCELED_PARTIALLY_FILLED, OrderStatus.REJECTED_PARTIALLY_FILLED])
def test_any_partial_fill_status_exits_without_portfolio(setup, status):
    repo, sid, client, _ = setup
    client.default_poll = fill(status=status)
    report = run(setup)
    assert report.blocks_buys and report.results[0].outcome == "unverified"
    assert client.read_count == 0 and len(client.lookups) == 1
    assert ledger(repo)["status"] == "unverified"
    assert repo.get_intents(sid)[0].resolved_at is None


@pytest.mark.parametrize("status", [OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED])
def test_zero_fill_rejection_stops_later_legs(setup, status):
    repo, sid, client, _ = setup
    client.default_poll = fill(status=status, partial=False)
    report = run(setup, intent=replace(SELL, amount=60),
                 positions=[POSITION, replace(POSITION, position_id="200")])
    assert report.blocks_buys and report.results[0].outcome == "rejected"
    assert len(client.calls) == 1 and len(report.deferred) == 1
    assert client.read_count == 0 and ledger(repo)["status"] == "rejected"
    assert repo.get_intents(sid)[0].resolved_at


@pytest.mark.parametrize("result, message", [(fill(pid="999"), "position mismatch"),
    (fill(units=1.9), "slice units mismatch"), (fill(partial=False), "single position")])
def test_bad_identity_or_slice_never_confirms(setup, result, message):
    repo, _, client, _ = setup
    client.default_poll = result
    report = run(setup)
    assert report.blocks_buys and message in report.results[0].detail
    assert ledger(repo)["status"] == "unresolved" and client.read_count == 0


@pytest.mark.parametrize("poll", [EtoroValidationError("not visible", status_code=404),
                                  fill(status=OrderStatus.PLACED)])
@pytest.mark.parametrize("partial", [True, False])
def test_lookup_budget_only_collects_evidence_never_confirms(setup, poll, partial):
    repo, sid, client, clock = setup
    client.default_poll = poll
    client.default_read = [replace(POSITION, units=2)] if partial else []
    report = run(setup, intent=SELL if partial else replace(SELL, amount=40))
    assert report.blocks_buys and report.results[0].outcome == "unverified"
    row = ledger(repo)
    assert row["status"] == "unverified" and row["units_after"] == (2 if partial else None)
    assert json.loads(row["raw_json"])["units_after"] == row["units_after"]
    assert row["close_price"] is row["proceeds"] is row["slice_position_id"] is None
    assert repo.get_intents(sid)[0].status == "sent" and not repo.get_intents(sid)[0].resolved_at
    assert clock.current == NOW + timedelta(seconds=150)
    assert client.read_count == 30 and len(client.lookups) == 30
    assert repo.confirmed_partial_closes(["100"]) == []


def test_late_filled_within_lookup_budget(setup):
    _, _, client, clock = setup
    client.polls = [fill(status=OrderStatus.PLACED)] * 29 + [fill()]
    report = run(setup)
    assert not report.blocks_buys and clock.current == NOW + timedelta(seconds=58)


@pytest.mark.parametrize("reads", [[POSITION], [], [RuntimeError("read failed")]])
def test_stale_absent_and_failed_partial_reads_timeout(setup, reads):
    repo, _, client, clock = setup
    client.default_read = reads
    if reads and isinstance(reads[0], Exception):
        client.default_read = reads[0]
    report = run(setup)
    assert report.blocks_buys and ledger(repo)["status"] == "unresolved"
    assert clock.current == NOW + timedelta(seconds=90)
    assert len(client.calls) == 1


def test_unexpected_units_stop_immediately(setup):
    repo, _, client, _ = setup
    client.default_read = [replace(POSITION, units=1)]
    report = run(setup)
    assert report.blocks_buys and report.results[0].detail == "unexpected units"
    assert ledger(repo)["units_after"] == 1


def test_full_close_needs_two_spaced_successful_absences_without_sidecar(setup):
    repo, _, client, clock = setup
    client.default_poll = fill(partial=False)
    client.reads = [[], RuntimeError("failed"), [], [POSITION], [], []]
    report = run(setup, intent=replace(SELL, amount=40))
    assert not report.blocks_buys and client.read_count == 6
    assert clock.current == NOW + timedelta(seconds=15)
    assert ledger(repo)["units_after"] == 0
    assert repo.list_close_observations()[0]["observed_status"] == "confirmed_closed"
    assert repo.confirmed_partial_closes(["100"]) == []
    assert client.lookups == [("900", {"expected_position_id": None})]


def test_send_exception_never_retries_and_defers_later_legs(setup):
    repo, _, client, _ = setup
    client.send_error = RuntimeError("lost response")
    report = run(setup, intent=replace(SELL, amount=60),
                 positions=[POSITION, replace(POSITION, position_id="200")])
    assert report.blocks_buys and len(report.deferred) == 1
    assert len(client.calls) == 1 and not client.lookups
    assert ledger(repo)["status"] == "unresolved"
    assert repo.get_order(report.results[0].order_row_id).error.startswith("close send failed")


def test_non404_lookup_error_does_not_run_portfolio(setup):
    repo, _, client, _ = setup
    client.default_poll = EtoroValidationError("bad shape", status_code=400)
    report = run(setup)
    assert report.blocks_buys and client.read_count == 0
    assert ledger(repo)["status"] == "unresolved"


@pytest.mark.parametrize("phase", ["before", "lookup", "portfolio"])
def test_deadline_prevents_confirmation(setup, phase):
    repo, _, client, clock = setup
    if phase == "lookup":
        client.default_poll = EtoroValidationError("not visible", status_code=404)
    if phase == "portfolio":
        client.default_read = [POSITION]
    report = run(setup, deadline=NOW + timedelta(seconds=0 if phase == "before" else 5))
    assert report.blocks_buys and report.stopped == "past deadline"
    if phase == "before":
        assert not client.calls and not repo.pending_partial_closes()
    else:
        assert ledger(repo)["status"] == "unresolved" and len(client.calls) == 1
        assert clock.current == NOW + timedelta(seconds=5)


@pytest.mark.parametrize("intent", [replace(SELL, symbol="UPRO"), replace(SELL, side="open"),
    replace(SELL, amount=0), replace(SELL, amount=None), replace(SELL, position_ref="100")])
def test_refuse_before_any_write(setup, intent):
    repo, _, client, _ = setup
    before = repo.conn.total_changes
    report = run(setup, intent=intent)
    assert report.blocks_buys and report.results[0].outcome == "refused"
    assert repo.conn.total_changes == before and not client.calls
