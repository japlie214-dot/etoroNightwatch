"""S7-3a buy executor (T028): SCHG "cash to core" buys, one at a time.

For each SCHG open intent, in order, up to ``max_orders``:

1. persist the intent (committed), then send the buy;
2. persist the order and poll it to a terminal state;
3. on a fill: read the broker position, write the lot annotation, clear the
   take-profit (each attempt persisted before its PATCH) and confirm by
   read-back.

An ambiguous buy outcome stops execution and is never resubmitted. A
take-profit that cannot be cleared is reported in ``tp_failed`` so the session
can set HALT; this module never writes HALT itself.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from ..db.repo import Intent, LotAnnotation, Order, Repo
from ..etoro.auth import new_request_id
from ..etoro.client import EtoroClient
from ..etoro.errors import (
    EtoroAuthError,
    EtoroRateLimitError,
    EtoroValidationError,
)
from ..etoro.models import OrderStatus, Position
from ..strategy.ladder import LadderIntent

__all__ = ["ExecutedBuy", "ExecutionReport", "execute_buys"]

TP_ATTEMPTS = 3
TP_READBACK_TRIES = 7
TP_READBACK_SLEEP = 3.0
READ_TRIES = 10
READ_SLEEP = 3.0

_PARTIAL_STATUSES = frozenset(
    {
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.CANCELED_PARTIALLY_FILLED,
        OrderStatus.REJECTED_PARTIALLY_FILLED,
    }
)


@dataclass(frozen=True, slots=True)
class ExecutedBuy:
    """What happened to one intent the executor attempted."""

    intent: LadderIntent
    intent_id: int
    request_id: str
    order_row_id: int | None
    order_id: str | None
    #: ``filled`` | ``rejected`` | ``unresolved``
    outcome: str
    position_id: str | None
    fill_amount_usd: float | None
    detail: str


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    """Result of one :func:`execute_buys` call."""

    results: tuple[ExecutedBuy, ...]
    #: Why execution ended early, or None.
    stopped: str | None
    #: Intents that were not attempted.
    deferred: tuple[LadderIntent, ...]
    #: Position ids whose take-profit is not confirmed cleared.
    tp_failed: tuple[str, ...]
    #: Human-readable follow-up lines.
    followups: tuple[str, ...]


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _tp_absent(position: Position) -> bool:
    """True only when the broker confirms no take-profit on the position.

    A missing rate with ``is_no_take_profit`` False is NOT confirmed absent.
    """
    return position.is_no_take_profit or (
        position.take_profit_rate is not None and position.take_profit_rate <= 0.0
    )


def _portfolio_position(client: EtoroClient, position_id: str) -> Position | None:
    """One portfolio read; a miss or any error returns None."""
    try:
        return next(
            (p for p in client.get_portfolio() if p.position_id == position_id), None
        )
    except Exception:
        return None


def _read_position(
    client: EtoroClient, position_id: str, sleep: Callable[[float], None]
) -> Position | None:
    for _ in range(READ_TRIES):
        found = _portfolio_position(client, position_id)
        if found is not None:
            return found
        sleep(READ_SLEEP)
    return None


def _clear_take_profit(
    *,
    repo: Repo,
    client: EtoroClient,
    order_row_id: int,
    position: Position,
    sleep: Callable[[float], None],
) -> bool:
    """Clear the position's take-profit with persisted attempts. True if confirmed."""
    position_id = position.position_id
    if _tp_absent(position):
        tp_id = repo.insert_tp_clear(order_row_id, position_id, new_request_id(), 1)
        repo.set_tp_clear_status(tp_id, "cleared", "already no TP at open")
        return True

    for attempt in range(1, TP_ATTEMPTS + 1):
        request_id = new_request_id()
        tp_id = repo.insert_tp_clear(order_row_id, position_id, request_id, attempt)
        try:
            ack = client.clear_take_profit(request_id, position_id)
        except (EtoroValidationError, ValueError) as exc:
            repo.set_tp_clear_status(
                tp_id, "failed", f"refused: {type(exc).__name__}: {exc}"
            )
            return False
        except Exception as exc:
            repo.set_tp_clear_status(
                tp_id, "failed", f"error: {type(exc).__name__}: {exc}"
            )
            continue
        repo.set_tp_clear_status(
            tp_id, "sent", f"ack status {ack.status_code} op {ack.operation_id}"
        )
        confirmed = False
        for _ in range(TP_READBACK_TRIES):
            sleep(TP_READBACK_SLEEP)
            current = _portfolio_position(client, position_id)
            if current is not None and _tp_absent(current):
                confirmed = True
                break
        if confirmed:
            repo.set_tp_clear_status(tp_id, "cleared", "read-back confirmed")
            return True
        repo.set_tp_clear_status(tp_id, "failed", "TP still present after read-back")
    return False


def _buy_one(
    *,
    repo: Repo,
    client: EtoroClient,
    session_id: int,
    trading_date: date,
    intent: LadderIntent,
    instrument_ids: Mapping[str, int],
    spy: Any,
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
    tp_failed: list[str],
    followups: list[str],
) -> ExecutedBuy:
    instrument_id = instrument_ids["SCHG"]
    request_id = new_request_id()
    sequence = repo.next_intent_sequence(session_id)
    intent_id = repo.insert_intent(
        Intent(
            session_id=session_id,
            request_id=request_id,
            sequence=sequence,
            symbol="SCHG",
            side="buy",
            instrument_id=instrument_id,
            book="catapult",
            amount_usd=intent.amount,
            reason=intent.reason,
        )
    )
    repo.set_intent_status(intent_id, "sent")

    def stop_refused(exc: Exception) -> ExecutedBuy:
        repo.set_intent_status(intent_id, "rejected", resolved=True)
        return ExecutedBuy(
            intent=intent, intent_id=intent_id, request_id=request_id,
            order_row_id=None, order_id=None, outcome="rejected",
            position_id=None, fill_amount_usd=None,
            detail=f"buy refused: {type(exc).__name__}: {exc}",
        )

    def stop_unresolved(
        detail: str, order_row_id: int | None = None, order_id: str | None = None
    ) -> ExecutedBuy:
        return ExecutedBuy(
            intent=intent, intent_id=intent_id, request_id=request_id,
            order_row_id=order_row_id, order_id=order_id, outcome="unresolved",
            position_id=None, fill_amount_usd=None, detail=detail,
        )

    try:
        order_id: str | None = client.place_buy_order(
            request_id, instrument_id, intent.amount
        )
    except (EtoroAuthError, EtoroRateLimitError) as exc:
        return stop_refused(exc)
    except EtoroValidationError as exc:
        if exc.status_code is not None:
            return stop_refused(exc)
        ambiguous: Exception = exc
        order_id = None
    except Exception as exc:
        ambiguous = exc
        order_id = None
    else:
        ambiguous = None

    if ambiguous is not None:
        try:
            recovered = client.recover_by_reference_id(request_id)
        except Exception:
            recovered = None
        if recovered is None:
            return stop_unresolved(
                f"buy outcome unknown ({type(ambiguous).__name__}); "
                "broker lookup found no order yet; NOT resubmitted"
            )
        order_id = recovered.order_id

    order_row_id = repo.insert_order(
        Order(
            intent_id=intent_id,
            session_id=session_id,
            request_id=request_id,
            symbol="SCHG",
            side="buy",
            instrument_id=instrument_id,
            order_id=order_id,
            reference_id=request_id,
            submitted_at=_iso(now()),
        )
    )

    try:
        poll = client.poll_order_to_terminal(order_id=order_id, reference_id=request_id)
    except Exception:
        poll = None
    if poll is None or (poll.timed_out and not poll.order.is_terminal):
        return stop_unresolved(
            "order not terminal (timeout/error); NOT resubmitted; if the last "
            "status was 5 (partially filled) a live partial position may exist "
            "without an annotation",
            order_row_id=order_row_id,
            order_id=order_id,
        )

    res = poll.order
    repo.update_order_status(
        order_row_id,
        last_status=res.status.value,
        status_history=[res.status.value],
        terminal_status=res.status.value if res.is_terminal else None,
        fill_price=res.fill_price,
        fill_units=res.fill_units,
        fill_amount_usd=res.fill_amount_usd,
        position_id=res.position_id,
        order_id=res.order_id or order_id,
        error=res.reject_reason,
    )
    effective_order_id = res.order_id or order_id

    if not res.is_filled:
        repo.set_intent_status(intent_id, "rejected", resolved=True)
        return ExecutedBuy(
            intent=intent, intent_id=intent_id, request_id=request_id,
            order_row_id=order_row_id, order_id=effective_order_id,
            outcome="rejected", position_id=None, fill_amount_usd=None,
            detail=f"order ended {res.status.name}: {res.reject_reason}",
        )

    repo.set_intent_status(intent_id, "filled", resolved=True)
    partial = res.status in _PARTIAL_STATUSES
    detail = f"filled: status {res.status.name}" + ("; partial fill" if partial else "")
    filled = ExecutedBuy(
        intent=intent, intent_id=intent_id, request_id=request_id,
        order_row_id=order_row_id, order_id=effective_order_id, outcome="filled",
        position_id=res.position_id, fill_amount_usd=res.fill_amount_usd,
        detail=detail,
    )

    position_id = res.position_id
    if position_id is None:
        followups.append(f"order {effective_order_id} filled but no position id")
        return filled

    position = _read_position(client, position_id, sleep)
    if position is None:
        followups.append(
            f"position {position_id} not visible in portfolio after 30 s: "
            "annotation and TP clear skipped"
        )
        tp_failed.append(position_id)
        return filled

    try:
        repo.insert_lot_annotation(
            LotAnnotation(
                position_id=position_id,
                symbol="SCHG",
                book="catapult",
                entry_date=position.open_date or trading_date.isoformat(),
                entry_amount=position.amount,
                origin="app",
                entry_context_source="derived",
                entry_reference_spy=spy.close,
                entry_spy_ath=spy.ath,
                entry_spy_close=spy.close,
                entry_spy_drawdown=spy.drawdown,
                entry_tier=spy.tier,
                bucket_id=None,
                entry_order_id=order_row_id,
                is_open=True,
            )
        )
    except Exception as exc:
        followups.append(f"annotation for position {position_id} failed: {exc}")

    if not _clear_take_profit(
        repo=repo, client=client, order_row_id=order_row_id,
        position=position, sleep=sleep,
    ):
        tp_failed.append(position_id)
    return filled


def execute_buys(
    *,
    repo: Repo,
    client: EtoroClient,
    session_id: int,
    trading_date: date,
    intents: Sequence[LadderIntent],
    instrument_ids: Mapping[str, int],
    spy: Any,
    max_orders: int,
    deadline: datetime,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = time.sleep,
) -> ExecutionReport:
    """Send SCHG cash-to-core buys one at a time, persisting every step.

    ``spy`` is the decision's SPY context (``ath``, ``close``, ``drawdown``,
    ``tier``). Never resubmits: an ambiguous buy ends execution.
    """
    for intent in intents:
        if intent.side != "open" or intent.symbol != "SCHG" or intent.amount is None or intent.amount <= 0:
            raise ValueError(f"S7-3a executes SCHG opens only: {intent!r}")
    if max_orders < 1:
        raise ValueError(f"max_orders must be at least 1, got {max_orders}")
    if now() >= deadline:
        return ExecutionReport((), "past submission deadline", tuple(intents), (), ())

    results: list[ExecutedBuy] = []
    tp_failed: list[str] = []
    followups: list[str] = []
    stopped: str | None = None
    deferred: tuple[LadderIntent, ...] = ()

    for index, intent in enumerate(intents):
        if len(results) == max_orders:
            deferred = tuple(intents[index:])
            break
        if now() >= deadline:
            stopped = "past submission deadline"
            deferred = tuple(intents[index:])
            break
        result = _buy_one(
            repo=repo, client=client, session_id=session_id,
            trading_date=trading_date, intent=intent,
            instrument_ids=instrument_ids, spy=spy, now=now, sleep=sleep,
            tp_failed=tp_failed, followups=followups,
        )
        results.append(result)
        if result.outcome != "filled":
            stopped = result.detail
            deferred = tuple(intents[index + 1:])
            break

    return ExecutionReport(
        results=tuple(results),
        stopped=stopped,
        deferred=deferred,
        tp_failed=tuple(tp_failed),
        followups=tuple(followups),
    )
