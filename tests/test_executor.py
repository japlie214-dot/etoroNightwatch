"""S7-3a buy executor (T028): offline tests with a scripted fake client.

The fake records every call and checks persist-before-send (the intent row
exists with status ``sent`` before a buy, the tp_clears row exists as
``pending`` before a PATCH). Violations are recorded, not raised, so an
assertion inside the executor's own try/except cannot be swallowed.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from nightwatch.db.repo import Repo
from nightwatch.engine.executor import execute_buys
from nightwatch.etoro.client import PollResult
from nightwatch.etoro.errors import (
    EtoroTimeoutError,
    EtoroValidationError,
)
from nightwatch.etoro.models import EditPositionAck, OrderResult, OrderStatus, Position
from nightwatch.strategy.ladder import LadderIntent, SpyContext
from .test_session_armed import armed
from .test_session_decide import config, no_network

NOW = datetime(2026, 10, 16, 15, 0, tzinfo=timezone.utc)
FUTURE = NOW + timedelta(hours=1)
TRADING_DATE = date(2026, 10, 16)
SPY = SpyContext(ath=700.0, close=650.0, drawdown=-0.07, tier=1)
INSTRUMENT_IDS = {"SCHG": 1234}


def _intent(amount: float = 633.33) -> LadderIntent:
    return LadderIntent("SCHG", "open", amount=amount, reason="cash to core")


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
        open_date="2026-10-16T14:31:00Z",
        take_profit_rate=take_profit_rate,
        is_no_take_profit=is_no_take_profit,
    )


def _filled(
    position_id: str | None = "P1",
    *,
    status: OrderStatus = OrderStatus.FILLED,
    fill_amount_usd: float | None = 633.33,
    order_id: str | None = "O1",
) -> OrderResult:
    return OrderResult(
        order_id=order_id,
        reference_id=None,
        status=status,
        position_id=position_id,
        fill_price=633.33,
        fill_units=1.0,
        fill_amount_usd=fill_amount_usd,
    )


def _poll(order: OrderResult, timed_out: bool = False) -> PollResult:
    return PollResult(order=order, attempts=1, timed_out=timed_out)


class FakeExecClient:
    """Scripted stand-in for EtoroClient. Queues: the last item repeats."""

    def __init__(self, repo: Repo) -> None:
        self.repo = repo
        self.calls: list[str] = []
        self.buy_calls: list[tuple[str, int, float]] = []
        self.poll_calls: list[tuple[str | None, str | None]] = []
        self.patch_calls: list[tuple[str, str]] = []
        self.violations: list[str] = []
        self.buy_queue: list = ["O1"]
        self.recover_queue: list = [None]
        self.poll_queue: list = [_poll(_filled())]
        self.portfolio_queue: list = [[_position(take_profit_rate=11.0)], [_position(is_no_take_profit=True)]]
        self.patch_queue: list = [EditPositionAck("op1", "P1", None, 202)]

    @staticmethod
    def _pop(queue: list):
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, BaseException):
            raise item
        return item

    def place_buy_order(self, request_id: str, instrument_id: int, amount_usd: float) -> str:
        self.calls.append("place_buy_order")
        self.buy_calls.append((request_id, instrument_id, amount_usd))
        row = self.repo.get_intent_by_request_id(request_id)
        if row is None or row.status != "sent" or row.sent_at is None or self.repo.conn.in_transaction:
            self.violations.append(f"buy sent before intent persisted as sent: {request_id}")
        return self._pop(self.buy_queue)

    def recover_by_reference_id(self, reference_id: str):
        self.calls.append("recover_by_reference_id")
        return self._pop(self.recover_queue)

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
def session_id(repo: Repo) -> int:
    return repo.create_session("2026-10-16", "live", "demo", True).id


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def fake(repo: Repo) -> FakeExecClient:
    return FakeExecClient(repo)


def _run(repo, client, session_id, sleeps, intents, *, max_orders=1, deadline=FUTURE):
    report = execute_buys(
        repo=repo,
        client=client,
        session_id=session_id,
        trading_date=TRADING_DATE,
        intents=intents,
        instrument_ids=INSTRUMENT_IDS,
        spy=SPY,
        max_orders=max_orders,
        deadline=deadline,
        now=lambda: NOW,
        sleep=sleeps.append,
    )
    assert client.violations == []
    return report


# -- happy path and ceiling ---------------------------------------------------


def test_happy_path_fills_annotates_and_clears_tp(repo, session_id, fake, sleeps) -> None:
    report = _run(repo, fake, session_id, sleeps, [_intent()])

    assert len(report.results) == 1
    result = report.results[0]
    assert result.outcome == "filled"
    assert result.position_id == "P1"
    assert report.stopped is None
    assert report.deferred == ()
    assert report.tp_failed == report.tp_uncleared == ()
    assert report.followups == ()

    assert fake.calls.count("place_buy_order") == 1
    assert fake.buy_calls[0][0] == result.request_id

    intent = repo.get_intents(session_id)[0]
    assert intent.side == "buy"
    assert intent.status == "filled"
    assert intent.resolved_at is not None

    order = repo.get_order(result.order_row_id)
    assert order.terminal_status == 3
    assert order.fill_amount_usd == 633.33
    assert order.position_id == "P1"

    annotation = repo.get_lot_annotation("P1")
    assert annotation is not None
    assert annotation.book == "catapult"
    assert annotation.origin == "app"
    assert annotation.entry_order_id == result.order_row_id
    assert annotation.entry_amount == 633.33
    assert annotation.entry_reference_spy == SPY.close

    tp_rows = repo.list_tp_clears("P1")
    assert [r["status"] for r in tp_rows] == ["cleared"]
    assert sleeps == [3.0]


def test_ceiling_defers_the_rest(repo, session_id, fake, sleeps) -> None:
    intents = [_intent(), _intent(100.0), _intent(50.0)]
    report = _run(repo, fake, session_id, sleeps, intents, max_orders=1)
    assert len(report.results) == 1
    assert fake.calls.count("place_buy_order") == 1
    assert report.deferred == tuple(intents[1:])
    assert report.stopped == "session order ceiling reached"


# -- buy outcomes -------------------------------------------------------------


def test_definite_refusal_stops_with_no_order_and_no_recovery(repo, session_id, fake, sleeps) -> None:
    fake.buy_queue = [EtoroValidationError("refused", status_code=400)]
    report = _run(repo, fake, session_id, sleeps, [_intent()])
    result = report.results[0]
    assert result.outcome == "rejected"
    assert result.order_row_id is None
    assert fake.calls.count("place_buy_order") == 1
    assert "recover_by_reference_id" not in fake.calls
    assert report.stopped is not None and report.stopped.startswith("buy refused")
    assert repo.get_intents(session_id)[0].status == "rejected"


def test_ambiguous_validation_error_without_status_code_is_unresolved(repo, session_id, fake, sleeps) -> None:
    fake.buy_queue = [EtoroValidationError("2xx without order id")]
    fake.recover_queue = [None]
    report = _run(repo, fake, session_id, sleeps, [_intent()])
    result = report.results[0]
    assert result.outcome == "unresolved"
    assert fake.calls.count("place_buy_order") == 1
    assert fake.calls.count("recover_by_reference_id") == 1
    assert repo.get_intents(session_id)[0].status == "sent"
    assert report.stopped is not None


def test_ambiguous_then_recovered_with_order_id(repo, session_id, fake, sleeps) -> None:
    fake.buy_queue = [EtoroTimeoutError("timed out")]
    fake.recover_queue = [OrderResult(order_id="O9", reference_id=None, status=OrderStatus.WAITING_FOR_MARKET)]
    fake.poll_queue = [_poll(_filled(order_id="O9"))]
    report = _run(repo, fake, session_id, sleeps, [_intent()])
    assert fake.calls.count("place_buy_order") == 1
    assert fake.calls.count("recover_by_reference_id") == 1
    assert fake.poll_calls[0][0] == "O9"
    assert fake.poll_calls[0][1] == report.results[0].request_id
    assert report.results[0].outcome == "filled"
    assert repo.get_lot_annotation("P1") is not None


def test_ambiguous_then_recovered_without_order_id_polls_by_reference(repo, session_id, fake, sleeps) -> None:
    fake.buy_queue = [EtoroTimeoutError("timed out")]
    fake.recover_queue = [OrderResult(order_id=None, reference_id=None, status=OrderStatus.WAITING_FOR_MARKET)]
    fake.poll_queue = [_poll(_filled())]
    report = _run(repo, fake, session_id, sleeps, [_intent()])
    assert fake.poll_calls[0] == (None, report.results[0].request_id)
    assert report.results[0].outcome == "filled"


def test_ambiguous_and_recovery_finds_nothing_never_resubmits(repo, session_id, fake, sleeps) -> None:
    fake.buy_queue = [EtoroTimeoutError("timed out")]
    fake.recover_queue = [None]
    report = _run(repo, fake, session_id, sleeps, [_intent(), _intent(10.0)], max_orders=2)
    assert report.results[0].outcome == "unresolved"
    assert fake.calls.count("place_buy_order") == 1
    assert repo.get_intents(session_id)[0].status == "sent"
    assert repo.get_lot_annotation("P1") is None
    assert report.deferred == (_intent(10.0),)
    assert report.stopped is not None


def test_poll_timeout_is_unresolved_with_order_row(repo, session_id, fake, sleeps) -> None:
    fake.poll_queue = [_poll(OrderResult(order_id="O1", reference_id=None, status=OrderStatus.WAITING_FOR_MARKET), timed_out=True)]
    report = _run(repo, fake, session_id, sleeps, [_intent()])
    result = report.results[0]
    assert result.outcome == "unresolved"
    assert result.order_row_id is not None
    assert repo.get_intents(session_id)[0].status == "sent"
    assert repo.get_lot_annotation("P1") is None


def test_terminal_rejected_writes_no_annotation(repo, session_id, fake, sleeps) -> None:
    fake.poll_queue = [_poll(OrderResult(order_id="O1", reference_id=None, status=OrderStatus.REJECTED, reject_reason="market closed"))]
    report = _run(repo, fake, session_id, sleeps, [_intent()])
    assert report.results[0].outcome == "rejected"
    assert repo.get_intents(session_id)[0].status == "rejected"
    assert repo.get_lot_annotation("P1") is None
    assert "market closed" in report.results[0].detail


def test_partial_fill_is_filled_and_annotates_position_amount(repo, session_id, fake, sleeps) -> None:
    fake.poll_queue = [_poll(_filled(status=OrderStatus.CANCELED_PARTIALLY_FILLED, fill_amount_usd=300.0))]
    fake.portfolio_queue = [[_position(amount=300.0, is_no_take_profit=True)]]
    report = _run(repo, fake, session_id, sleeps, [_intent()])
    result = report.results[0]
    assert result.outcome == "filled"
    assert "partial" in result.detail
    assert repo.get_lot_annotation("P1").entry_amount == 300.0


def test_timeout_on_partially_filled_status_warns_of_live_partial(repo, session_id, fake, sleeps) -> None:
    fake.poll_queue = [_poll(OrderResult(order_id="O1", reference_id=None, status=OrderStatus.PARTIALLY_FILLED), timed_out=True)]
    report = _run(repo, fake, session_id, sleeps, [_intent()])
    assert report.results[0].outcome == "unresolved"
    assert "live partial position" in report.results[0].detail


# -- position read and take-profit --------------------------------------------


def test_position_never_visible_skips_annotation_and_patch(repo, session_id, fake, sleeps) -> None:
    fake.portfolio_queue = [[]]
    report = _run(repo, fake, session_id, sleeps, [_intent()])
    assert report.results[0].outcome == "filled"
    assert any("not visible in portfolio" in line for line in report.followups)
    assert report.tp_failed == ("P1",)
    assert fake.calls.count("clear_take_profit") == 0
    assert repo.get_lot_annotation("P1") is None
    assert report.tp_uncleared == ()
    assert sleeps == [3.0] * 10


def test_tp_never_clears_makes_three_attempts(repo, session_id, fake, sleeps) -> None:
    fake.portfolio_queue = [[_position(take_profit_rate=11.0)]]
    report = _run(repo, fake, session_id, sleeps, [_intent()])
    assert fake.calls.count("place_buy_order") == 1
    assert fake.calls.count("clear_take_profit") == 3
    assert len({rid for rid, _ in fake.patch_calls}) == 3
    rows = repo.list_tp_clears("P1")
    assert [r["status"] for r in rows] == ["failed", "failed", "failed"]
    assert report.tp_uncleared == ("P1",)
    assert report.tp_failed == ()


def test_tp_definite_refusal_is_not_retried(repo, session_id, fake, sleeps) -> None:
    fake.patch_queue = [EtoroValidationError("refused")]
    report = _run(repo, fake, session_id, sleeps, [_intent()])
    assert fake.calls.count("clear_take_profit") == 1
    rows = repo.list_tp_clears("P1")
    assert [r["status"] for r in rows] == ["failed"]
    assert report.tp_uncleared == ("P1",)
    assert report.tp_failed == ()


def test_tp_ambiguous_then_success(repo, session_id, fake, sleeps) -> None:
    fake.patch_queue = [EtoroTimeoutError("timed out"), EditPositionAck("op2", "P1", None, 202)]
    report = _run(repo, fake, session_id, sleeps, [_intent()])
    assert fake.calls.count("clear_take_profit") == 2
    assert [r["status"] for r in repo.list_tp_clears("P1")] == ["failed", "cleared"]
    assert report.tp_failed == ()


def test_already_no_tp_at_open_sends_no_patch(repo, session_id, fake, sleeps) -> None:
    fake.portfolio_queue = [[_position(take_profit_rate=None, is_no_take_profit=True)]]
    report = _run(repo, fake, session_id, sleeps, [_intent()])
    assert fake.calls.count("clear_take_profit") == 0
    rows = repo.list_tp_clears("P1")
    assert [r["status"] for r in rows] == ["cleared"]
    assert report.tp_failed == ()


def test_missing_tp_rate_is_not_confirmed_absent(repo, session_id, fake, sleeps) -> None:
    fake.portfolio_queue = [[_position(take_profit_rate=None, is_no_take_profit=False)], [_position(is_no_take_profit=True)]]
    report = _run(repo, fake, session_id, sleeps, [_intent()])
    assert fake.calls.count("clear_take_profit") == 1
    assert [r["status"] for r in repo.list_tp_clears("P1")] == ["cleared"]
    assert report.tp_failed == ()


def test_no_position_id_on_fill_stays_unresolved_and_stops(repo, session_id, fake, sleeps) -> None:
    fake.poll_queue = [_poll(_filled(position_id=None))]
    report = _run(repo, fake, session_id, sleeps, [_intent(), _intent(10)], max_orders=2)
    assert report.results[0].outcome == "filled"
    assert any("no position id" in line for line in report.followups)
    assert report.tp_failed == ("order O1: no position id",)
    assert report.stopped and report.deferred == (_intent(10),)
    assert len(fake.buy_calls) == 1
    saved, = repo.get_intents(session_id)
    assert saved.status == "filled_unannotated" and saved.resolved_at is None
    assert fake.calls.count("get_portfolio") == 0
    assert repo.list_tp_clears() == []


# -- deadline, guards, stop-after-failure ------------------------------------


def test_deadline_passed_sends_nothing(repo, session_id, fake, sleeps) -> None:
    intents = [_intent(), _intent(10.0)]
    report = _run(repo, fake, session_id, sleeps, intents, deadline=NOW)
    assert fake.calls == []
    assert report.stopped == "past submission deadline"
    assert report.deferred == tuple(intents)
    assert report.results == ()


@pytest.mark.parametrize(
    "bad",
    [
        LadderIntent("UPRO", "open", amount=100.0),
        LadderIntent("SCHG", "close", amount=100.0),
        LadderIntent("SCHG", "open", amount=None),
        LadderIntent("SCHG", "open", amount=0.0),
    ],
)
def test_guard_rejects_non_schg_open_before_any_call(repo, session_id, fake, sleeps, bad) -> None:
    with pytest.raises(ValueError, match="S7-3a executes SCHG opens only"):
        _run(repo, fake, session_id, sleeps, [_intent(), bad])
    assert fake.calls == []


def test_zero_max_orders_defers_without_writes(repo, session_id, fake, sleeps) -> None:
    report = _run(repo, fake, session_id, sleeps, [_intent()], max_orders=0)
    assert report.results == () and report.deferred == (_intent(),)
    assert report.stopped == "session order ceiling reached"
    assert fake.calls == [] and repo.get_intents(session_id) == []


def test_guard_rejects_negative_max_orders(repo, session_id, fake, sleeps) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        _run(repo, fake, session_id, sleeps, [_intent()], max_orders=-1)
    assert fake.calls == []


def test_stop_after_rejection_defers_the_next_intent(repo, session_id, fake, sleeps) -> None:
    fake.buy_queue = [EtoroValidationError("refused", status_code=400), "O2"]
    intents = [_intent(), _intent(20.0)]
    report = _run(repo, fake, session_id, sleeps, intents, max_orders=2)
    assert fake.calls.count("place_buy_order") == 1
    assert report.deferred == (intents[1],)
    assert report.stopped is not None


@pytest.mark.parametrize("failure", ["missing", "mismatch", "annotation", "resolve"])
def test_finalization_failure_stops_next_buy_and_reserves_cap(
    repo, session_id, fake, sleeps, failure,
):
    from dataclasses import replace

    if failure == "missing":
        fake.portfolio_queue = [[]]
    elif failure == "mismatch":
        fake.portfolio_queue = [[replace(_position(), instrument_id=-1)]]
    else:
        event = "INSERT ON lot_annotations" if failure == "annotation" else "UPDATE ON intents WHEN NEW.status = 'filled'"
        repo.conn.execute(f"CREATE TRIGGER fail_finalize BEFORE {event} "
                          "BEGIN SELECT RAISE(ABORT, 'injected finalization failure'); END")
        repo.conn.commit()
    report = _run(repo, fake, session_id, sleeps, [_intent(), _intent(10)], max_orders=2)

    assert len(fake.buy_calls) == 1
    assert report.tp_failed == ("P1",)
    assert report.stopped and report.deferred == (_intent(10),)
    saved, = repo.get_intents(session_id)
    assert saved.status == "filled_unannotated" and saved.resolved_at is None
    assert repo.unresolved_intents(session_id) == [saved]
    assert repo.reserved_buy_usd(TRADING_DATE.isoformat()) == pytest.approx(633.33)
    assert repo.get_lot_annotation("P1") is None
    assert fake.patch_calls == []
    assert not repo.conn.in_transaction


@pytest.mark.parametrize("failure", ["annotation", "identity"])
def test_run_session_finalization_failure_halts_and_vetoes_final_save(
    armed, repo, host, monkeypatch, failure,
):
    from dataclasses import replace
    from nightwatch.engine import executor, session
    from .test_session_market import DAY, IDS, FakeTiingo

    armed.config = replace(armed.config, risk=replace(armed.config.risk, max_orders_per_session=2))
    armed.decision.intents *= 2
    fake = FakeExecClient(repo)
    fake.portfolio_queue = [[replace(_position(), instrument_id=IDS["SCHG"])]]
    if failure == "annotation":
        repo.conn.execute("CREATE TRIGGER fail_annotation BEFORE INSERT ON lot_annotations "
                          "BEGIN SELECT RAISE(ABORT, 'annotation insert failed'); END")
        repo.conn.commit()
    else:
        fake.poll_queue = [_poll(_filled(position_id=None))]
    armed.execute.side_effect = lambda **kw: executor.execute_buys(
        **{**kw, "client": fake, "sleep": lambda _: None})
    before = repo.get_strategy_state()
    result = session.run_session(host=host, config=armed.config, repo=repo,
                                 client=armed.client, tiingo=FakeTiingo(),
                                 trading_date=DAY, wait=False)

    assert len(fake.buy_calls) == 1 and fake.violations == []
    assert result.halt_set and result.tp_failed and host.halt_flag_path().exists()
    assert "strategy state not saved" in result.reason
    assert ("annotation insert failed" if failure == "annotation" else "no position id") in result.reason
    armed.save.assert_not_called()
    assert repo.get_strategy_state() == before
    saved, = repo.get_intents(result.session_id)
    assert saved.status == "filled_unannotated" and saved.resolved_at is None
    assert repo.get_lot_annotation("P1") is None
