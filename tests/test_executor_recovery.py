"""S7-3a crash recovery (T029): offline tests with a scripted fake client.

Recovery must never buy. Every test asserts ``place_buy_order`` was not called,
and the fake records persist-before-send violations instead of raising them.
"""

from __future__ import annotations

import pytest

from nightwatch.db.repo import Decision, Intent, Order, Repo
from nightwatch.engine.executor import recover_unfinished
from nightwatch.etoro.client import PollResult
from nightwatch.etoro.errors import EtoroTimeoutError
from nightwatch.etoro.models import EditPositionAck, OrderResult, OrderStatus, Position

DAY = "2026-10-15"
TODAY = "2026-10-16"


def _position(
    position_id: str = "P1",
    *,
    amount: float = 633.33,
    take_profit_rate: float | None = 11.0,
    is_no_take_profit: bool = False,
) -> Position:
    return Position(
        position_id=position_id,
        instrument_id=1234,
        units=1.0,
        entry_price=633.33,
        amount=amount,
        leverage=1,
        is_buy=True,
        open_date="2026-10-15T14:31:00Z",
        take_profit_rate=take_profit_rate,
        is_no_take_profit=is_no_take_profit,
    )


def _filled(
    position_id: str | None = "P1",
    *,
    order_id: str | None = "O1",
    status: OrderStatus = OrderStatus.FILLED,
) -> OrderResult:
    return OrderResult(
        order_id=order_id,
        reference_id=None,
        status=status,
        position_id=position_id,
        fill_price=633.33,
        fill_units=1.0,
        fill_amount_usd=633.33,
    )


def _poll(order: OrderResult, timed_out: bool = False) -> PollResult:
    return PollResult(order=order, attempts=1, timed_out=timed_out)


class FakeRecoveryClient:
    """Scripted stand-in for EtoroClient. Each queue: the last item repeats."""

    def __init__(self, repo: Repo) -> None:
        self.repo = repo
        self.calls: list[str] = []
        self.buy_calls: list[str] = []
        self.poll_calls: list[tuple[str | None, str | None]] = []
        self.patch_calls: list[tuple[str, str]] = []
        self.violations: list[str] = []
        self.recover_by_rid: dict[str, list] = {}
        self.poll_queue: list = [_poll(_filled())]
        self.portfolio_queue: list = [
            [_position(take_profit_rate=11.0)],
            [_position(is_no_take_profit=True)],
        ]
        self.patch_queue: list = [EditPositionAck("op1", "P1", None, 202)]

    @staticmethod
    def _pop(queue: list):
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, BaseException):
            raise item
        return item

    def place_buy_order(self, request_id: str, instrument_id: int, amount_usd: float) -> str:
        self.calls.append("place_buy_order")
        self.buy_calls.append(request_id)
        return "O-SHOULD-NEVER-HAPPEN"

    def recover_by_reference_id(self, reference_id: str):
        self.calls.append("recover_by_reference_id")
        return self._pop(self.recover_by_rid.get(reference_id, [None]))

    def poll_order_to_terminal(self, *, order_id=None, reference_id=None, **_kw) -> PollResult:
        self.calls.append("poll_order_to_terminal")
        self.poll_calls.append((order_id, reference_id))
        return self._pop(self.poll_queue)

    def get_portfolio(self) -> list[Position]:
        self.calls.append("get_portfolio")
        return self._pop(self.portfolio_queue)

    def clear_take_profit(self, request_id: str, position_id: str) -> EditPositionAck:
        self.calls.append("clear_take_profit")
        self.patch_calls.append((request_id, position_id))
        pending = [r for r in self.repo.list_tp_clears(position_id) if r["request_id"] == request_id]
        if not pending or pending[0]["status"] != "pending":
            self.violations.append(f"PATCH sent before tp_clears row persisted: {request_id}")
        return self._pop(self.patch_queue)


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def fake(repo: Repo) -> FakeRecoveryClient:
    return FakeRecoveryClient(repo)


def _session(repo: Repo, day: str):
    return repo.get_session_by_date(day) or repo.create_session(day, "live", "demo", True)


def _seed_intent(
    repo: Repo,
    *,
    rid: str,
    day: str = DAY,
    sequence: int = 1,
    status: str = "sent",
    spy_close: float | None = 600.0,
    decision: bool = True,
) -> int:
    session = _session(repo, day)
    if decision and repo.get_decision(session.id) is None:
        repo.insert_decision(
            Decision(
                session_id=session.id,
                trading_date=day,
                spy_close=spy_close,
                spy_ath=700.0,
                drawdown=-0.07,
                tier=1,
            )
        )
    intent_id = repo.insert_intent(
        Intent(
            session_id=session.id,
            request_id=rid,
            sequence=sequence,
            symbol="SCHG",
            side="buy",
            instrument_id=1234,
            book="catapult",
            amount_usd=633.33,
            reason="cash to core",
        )
    )
    if status != "pending":
        repo.set_intent_status(intent_id, status)
    return intent_id


def _later_sessions(repo: Repo, days: list[str]) -> None:
    for day in days:
        _session(repo, day)


def _recover(repo, client, sleeps, *, abandon_after: int = 3):
    report = recover_unfinished(
        repo=repo,
        client=client,
        abandon_after_sessions=abandon_after,
        sleep=sleeps.append,
    )
    assert client.buy_calls == []
    assert client.violations == []
    return report


def _status(repo: Repo, rid: str) -> str:
    return repo.get_intent_by_request_id(rid).status


# -- filled and terminal ------------------------------------------------------


def test_sent_filled_visible_recovers_with_original_session_spy(repo, fake, sleeps) -> None:
    _seed_intent(repo, rid="rid-1", spy_close=600.0)
    fake.recover_by_rid["rid-1"] = [_filled(order_id="O1")]

    report = _recover(repo, fake, sleeps)

    assert _status(repo, "rid-1") == "recovered"
    order = repo.get_order_by_reference_id("rid-1")
    assert order.terminal_status == 3
    assert order.position_id == "P1"
    annotation = repo.get_lot_annotation("P1")
    assert annotation is not None
    assert annotation.entry_spy_close == 600.0
    assert annotation.entry_reference_spy == 600.0
    assert [r["status"] for r in repo.list_tp_clears("P1")] == ["cleared"]
    assert any("recovered fill" in line for line in report.resolved)
    assert fake.calls.count("place_buy_order") == 0
    assert fake.calls.count("poll_order_to_terminal") == 0


def test_non_terminal_then_polled_rejected_marks_rejected(repo, fake, sleeps) -> None:
    _seed_intent(repo, rid="rid-2")
    fake.recover_by_rid["rid-2"] = [OrderResult(order_id="O2", reference_id=None, status=OrderStatus.WAITING_FOR_MARKET)]
    fake.poll_queue = [_poll(OrderResult(order_id="O2", reference_id=None, status=OrderStatus.REJECTED, reject_reason="market closed"))]

    report = _recover(repo, fake, sleeps)

    assert _status(repo, "rid-2") == "rejected"
    assert fake.poll_calls == [("O2", "rid-2")]
    assert repo.get_order_by_reference_id("rid-2").terminal_status == 4
    assert repo.get_lot_annotation("P1") is None
    assert any("recovered rejection" in line for line in report.resolved)
    assert fake.calls.count("place_buy_order") == 0


# -- missing record, lookup failure, abandonment -----------------------------


def test_none_with_age_one_stays_sent_and_unresolved(repo, fake, sleeps) -> None:
    _seed_intent(repo, rid="rid-3")
    _later_sessions(repo, [TODAY])
    fake.recover_by_rid["rid-3"] = [None]

    report = _recover(repo, fake, sleeps)

    assert _status(repo, "rid-3") == "sent"
    assert len(report.still_unresolved) == 1
    assert report.abandoned == ()
    assert fake.calls.count("place_buy_order") == 0


def test_none_with_age_three_abandons_and_still_reserves_its_own_date(repo, fake, sleeps) -> None:
    _seed_intent(repo, rid="rid-4")
    _later_sessions(repo, [TODAY, "2026-10-17", "2026-10-18"])
    fake.recover_by_rid["rid-4"] = [None]

    report = _recover(repo, fake, sleeps, abandon_after=3)

    assert _status(repo, "rid-4") == "abandoned"
    assert len(report.abandoned) == 1
    assert repo.reserved_buy_usd(DAY) == pytest.approx(633.33)
    assert fake.calls.count("place_buy_order") == 0


def test_lookup_exception_is_cannot_tell_and_stays_sent(repo, fake, sleeps) -> None:
    _seed_intent(repo, rid="rid-5")
    _later_sessions(repo, [TODAY])
    fake.recover_by_rid["rid-5"] = [EtoroTimeoutError("timed out")]

    report = _recover(repo, fake, sleeps)

    assert _status(repo, "rid-5") == "sent"
    assert len(report.still_unresolved) == 1
    assert any("cannot tell" in line for line in report.followups)
    assert fake.calls.count("place_buy_order") == 0


def test_pending_intent_without_order_row_stays_pending(repo, fake, sleeps) -> None:
    _seed_intent(repo, rid="rid-6", status="pending")
    _later_sessions(repo, [TODAY])
    fake.recover_by_rid["rid-6"] = [None]

    report = _recover(repo, fake, sleeps)

    assert _status(repo, "rid-6") == "pending"
    assert len(report.still_unresolved) == 1
    assert repo.get_order_by_reference_id("rid-6") is None
    assert fake.calls.count("place_buy_order") == 0


# -- filled bookkeeping edge cases -------------------------------------------


def test_filled_position_not_visible_then_visible_on_second_call(repo, fake, sleeps) -> None:
    _seed_intent(repo, rid="rid-7")
    fake.recover_by_rid["rid-7"] = [_filled(order_id="O1")]
    fake.portfolio_queue = [[]]

    first = _recover(repo, fake, sleeps)

    assert _status(repo, "rid-7") == "sent"
    assert any("not visible in portfolio" in line for line in first.followups)
    assert len(first.still_unresolved) == 1
    assert repo.get_lot_annotation("P1") is None

    fake.portfolio_queue = [
        [_position(take_profit_rate=11.0)],
        [_position(is_no_take_profit=True)],
    ]
    second = _recover(repo, fake, sleeps)

    assert _status(repo, "rid-7") == "recovered"
    assert repo.get_lot_annotation("P1").entry_spy_close == 600.0
    assert second.tp_failed == ()
    assert fake.calls.count("place_buy_order") == 0


def test_filled_without_decision_row_skips_annotation(repo, fake, sleeps) -> None:
    _seed_intent(repo, rid="rid-8", decision=False)
    fake.recover_by_rid["rid-8"] = [_filled(order_id="O1")]

    report = _recover(repo, fake, sleeps)

    assert _status(repo, "rid-8") == "sent"
    assert any("no SPY context" in line for line in report.followups)
    assert len(report.still_unresolved) == 1
    assert repo.get_lot_annotation("P1") is None
    assert fake.calls.count("place_buy_order") == 0


def test_filled_with_null_spy_close_skips_annotation(repo, fake, sleeps) -> None:
    _seed_intent(repo, rid="rid-8b", spy_close=None)
    fake.recover_by_rid["rid-8b"] = [_filled(order_id="O1")]

    report = _recover(repo, fake, sleeps)

    assert _status(repo, "rid-8b") == "sent"
    assert any("no SPY context" in line for line in report.followups)
    assert repo.get_lot_annotation("P1") is None


def test_poll_timeout_at_status_11_updates_order_row_and_stays_sent(repo, fake, sleeps) -> None:
    _seed_intent(repo, rid="rid-9a")
    fake.recover_by_rid["rid-9a"] = [OrderResult(order_id="O3", reference_id=None, status=OrderStatus.WAITING_FOR_MARKET)]
    fake.poll_queue = [_poll(OrderResult(order_id="O3", reference_id=None, status=OrderStatus.WAITING_FOR_MARKET), timed_out=True)]

    report = _recover(repo, fake, sleeps)

    assert _status(repo, "rid-9a") == "sent"
    assert repo.get_order_by_reference_id("rid-9a").last_status == 11
    assert len(report.still_unresolved) == 1
    assert fake.calls.count("place_buy_order") == 0


def test_poll_timeout_at_status_5_warns_of_live_partial(repo, fake, sleeps) -> None:
    _seed_intent(repo, rid="rid-9b")
    fake.recover_by_rid["rid-9b"] = [OrderResult(order_id="O4", reference_id=None, status=OrderStatus.PARTIALLY_FILLED)]
    fake.poll_queue = [_poll(OrderResult(order_id="O4", reference_id=None, status=OrderStatus.PARTIALLY_FILLED), timed_out=True)]

    report = _recover(repo, fake, sleeps)

    assert _status(repo, "rid-9b") == "sent"
    assert repo.get_order_by_reference_id("rid-9b").last_status == 5
    assert any("live partial position" in line for line in report.followups)
    assert fake.calls.count("place_buy_order") == 0


def test_found_order_without_order_id_polls_by_reference(repo, fake, sleeps) -> None:
    _seed_intent(repo, rid="rid-10")
    fake.recover_by_rid["rid-10"] = [OrderResult(order_id=None, reference_id=None, status=OrderStatus.WAITING_FOR_MARKET)]
    fake.poll_queue = [_poll(_filled(order_id=None))]

    _recover(repo, fake, sleeps)

    assert fake.poll_calls[0] == (None, "rid-10")
    assert _status(repo, "rid-10") == "recovered"


# -- take-profit clears -------------------------------------------------------


def _tp_row(repo: Repo, *, rid: str, position_id: str, status: str) -> tuple[int, int]:
    session = _session(repo, DAY)
    intent_id = repo.insert_intent(
        Intent(session_id=session.id, request_id=rid, sequence=repo.next_intent_sequence(session.id),
               symbol="SCHG", side="buy", amount_usd=633.33)
    )
    order_row_id = repo.insert_order(
        Order(intent_id=intent_id, session_id=session.id, request_id=rid, symbol="SCHG", side="buy")
    )
    tp_id = repo.insert_tp_clear(order_row_id, position_id, f"tp-{rid}", 1)
    if status != "pending":
        repo.set_tp_clear_status(tp_id, status)
    return tp_id, order_row_id


def test_sent_tp_row_with_no_tp_on_broker_is_cleared(repo, fake, sleeps) -> None:
    tp_id, _ = _tp_row(repo, rid="tp-a", position_id="P1", status="sent")
    fake.portfolio_queue = [[_position(is_no_take_profit=True)]]

    _recover(repo, fake, sleeps)

    row = repo.list_tp_clears("P1")[0]
    assert row["id"] == tp_id
    assert row["status"] == "cleared"
    assert row["detail"] == "confirmed at recovery"
    assert fake.calls.count("clear_take_profit") == 0
    assert fake.calls.count("place_buy_order") == 0


def test_pending_tp_row_with_tp_on_broker_superseded_and_cleared(repo, fake, sleeps) -> None:
    tp_id, _ = _tp_row(repo, rid="tp-b", position_id="P1", status="pending")
    fake.portfolio_queue = [[_position(take_profit_rate=11.0)], [_position(is_no_take_profit=True)]]

    report = _recover(repo, fake, sleeps)

    rows = repo.list_tp_clears("P1")
    assert rows[0]["id"] == tp_id
    assert rows[0]["status"] == "failed"
    assert rows[0]["detail"] == "superseded by fresh attempt"
    assert rows[1]["status"] == "cleared"
    assert len(fake.patch_calls) == 1
    assert fake.patch_calls[0][0] == rows[1]["request_id"]
    assert rows[1]["request_id"] != rows[0]["request_id"]
    assert report.tp_failed == ()
    assert fake.calls.count("place_buy_order") == 0


def test_pending_tp_row_still_dirty_after_three_attempts_is_tp_failed(repo, fake, sleeps) -> None:
    _tp_row(repo, rid="tp-c", position_id="P1", status="pending")
    fake.portfolio_queue = [[_position(take_profit_rate=11.0)]]

    report = _recover(repo, fake, sleeps)

    assert len(fake.patch_calls) == 3
    assert [r["status"] for r in repo.list_tp_clears("P1")] == ["failed", "failed", "failed", "failed"]
    assert report.tp_failed == ("P1",)
    assert fake.calls.count("place_buy_order") == 0


def test_missing_tp_rate_is_not_confirmed_absent_at_recovery(repo, fake, sleeps) -> None:
    _tp_row(repo, rid="tp-d", position_id="P1", status="pending")
    fake.portfolio_queue = [
        [_position(take_profit_rate=None, is_no_take_profit=False)],
        [_position(is_no_take_profit=True)],
    ]

    _recover(repo, fake, sleeps)

    assert fake.calls.count("clear_take_profit") == 1
    assert [r["status"] for r in repo.list_tp_clears("P1")] == ["failed", "cleared"]


def test_tp_row_whose_position_is_absent_fails_with_followup(repo, fake, sleeps) -> None:
    _tp_row(repo, rid="tp-e", position_id="P9", status="pending")
    fake.portfolio_queue = [[]]

    report = _recover(repo, fake, sleeps)

    assert repo.list_tp_clears("P9")[0]["status"] == "failed"
    assert any("not in portfolio" in line for line in report.followups)
    assert fake.calls.count("clear_take_profit") == 0


# -- idempotency and independence --------------------------------------------


def test_second_call_on_same_state_makes_no_broker_calls(repo, fake, sleeps) -> None:
    _seed_intent(repo, rid="rid-15")
    fake.recover_by_rid["rid-15"] = [_filled(order_id="O1")]
    _recover(repo, fake, sleeps)
    calls_after_first = len(fake.calls)

    second = _recover(repo, fake, sleeps)

    assert len(fake.calls) == calls_after_first
    assert second.resolved == ()
    assert second.still_unresolved == ()
    assert second.abandoned == ()


def test_two_intents_are_handled_independently(repo, fake, sleeps) -> None:
    _seed_intent(repo, rid="rid-16a", sequence=1)
    _seed_intent(repo, rid="rid-16b", sequence=2)
    fake.recover_by_rid["rid-16a"] = [_filled(order_id="O1")]
    fake.recover_by_rid["rid-16b"] = [None]

    report = _recover(repo, fake, sleeps)

    assert _status(repo, "rid-16a") == "recovered"
    assert _status(repo, "rid-16b") == "sent"
    assert len(report.still_unresolved) == 1
    assert fake.calls.count("place_buy_order") == 0
