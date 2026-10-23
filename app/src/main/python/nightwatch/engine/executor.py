"""Buy executor: SCHG and UPRO open buys, one at a time.

For each SCHG or UPRO open intent, in order, up to ``max_orders``:

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
from ..etoro.models import OrderResult, OrderStatus, Position
from ..strategy.ladder import LadderIntent, SpyContext

__all__ = [
    "ExecutedBuy",
    "ExecutionReport",
    "RecoveryReport",
    "execute_buys",
    "recover_unfinished",
]

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


def finish_filled_buy(
    *,
    repo: Repo,
    client: EtoroClient,
    intent: LadderIntent,
    intent_id: int,
    request_id: str,
    res: OrderResult,
    order_id: str | None,
    order_row_id: int,
    instrument_id: int,
    trading_date: date,
    spy: Any,
    sleep: Callable[[float], None],
    followups: list[str],
    tp_failed: list[str],
) -> ExecutedBuy:
    """Steps after a confirmed fill: position read, annotation, TP clear.

    ``order_id`` is the effective id (``res.order_id or order_id``). The caller
    has already written the order row and set the intent to ``filled``. Appends
    to ``followups`` and ``tp_failed`` in place.
    """
    partial = res.status in _PARTIAL_STATUSES
    detail = f"filled: status {res.status.name}" + ("; partial fill" if partial else "")
    filled = ExecutedBuy(
        intent=intent, intent_id=intent_id, request_id=request_id,
        order_row_id=order_row_id, order_id=order_id, outcome="filled",
        position_id=res.position_id, fill_amount_usd=res.fill_amount_usd,
        detail=detail,
    )

    position_id = res.position_id
    if position_id is None:
        followups.append(f"order {order_id} filled but no position id")
        return filled

    position = _read_position(client, position_id, sleep)
    if position is None:
        followups.append(
            f"position {position_id} not visible in portfolio after 30 s: "
            "annotation and TP clear skipped"
        )
        tp_failed.append(position_id)
        return filled

    if position.instrument_id != instrument_id:
        followups.append(f"position {position_id}: instrument mismatch")
        tp_failed.append(position_id)
        return filled

    try:
        repo.insert_lot_annotation(
            LotAnnotation(
                position_id=position_id,
                symbol=intent.symbol,
                book=intent.book or "catapult",
                entry_date=position.open_date or trading_date.isoformat(),
                entry_amount=position.amount,
                origin="app",
                entry_context_source="derived",
                entry_reference_spy=spy.close,
                entry_spy_ath=spy.ath,
                entry_spy_close=spy.close,
                entry_spy_drawdown=spy.drawdown,
                entry_tier=spy.tier,
                bucket_id=intent.bucket_id,
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
    instrument_id = instrument_ids[intent.symbol]
    request_id = new_request_id()
    sequence = repo.next_intent_sequence(session_id)
    intent_id = repo.insert_intent(
        Intent(
            session_id=session_id,
            request_id=request_id,
            sequence=sequence,
            symbol=intent.symbol,
            side="buy",
            instrument_id=instrument_id,
            book=intent.book or "catapult",
            bucket_id=intent.bucket_id,
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
            symbol=intent.symbol,
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
    return finish_filled_buy(
        repo=repo, client=client, intent=intent, intent_id=intent_id,
        request_id=request_id, res=res, order_id=effective_order_id,
        order_row_id=order_row_id, instrument_id=instrument_id,
        trading_date=trading_date, spy=spy,
        sleep=sleep, followups=followups, tp_failed=tp_failed,
    )


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
    """Send SCHG and UPRO open buys one at a time, persisting every step.

    ``spy`` is the decision's SPY context (``ath``, ``close``, ``drawdown``,
    ``tier``). Never resubmits: an ambiguous buy ends execution.
    """
    for intent in intents:
        if (
            intent.side != "open"
            or intent.symbol not in ("SCHG", "UPRO")
            or intent.symbol not in instrument_ids
            or intent.amount is None
            or intent.amount <= 0
        ):
            raise ValueError(f"S7-3a executes SCHG opens only or UPRO opens: {intent!r}")
        book = "catapult" if intent.symbol == "SCHG" and intent.book is None else intent.book
        if book not in ("catapult", "reentry"):
            raise ValueError(f"buy book must be catapult or reentry: {intent!r}")
        if book == "reentry" and intent.bucket_id is None:
            raise ValueError(f"reentry buy requires bucket_id: {intent!r}")
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


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """What :func:`recover_unfinished` did at session start."""

    #: Intents or take-profit clears completed this call.
    resolved: tuple[str, ...]
    #: Intents still ``pending``/``sent`` after this call.
    still_unresolved: tuple[str, ...]
    #: Intents marked ``abandoned`` this call.
    abandoned: tuple[str, ...]
    #: Position ids whose take-profit is not confirmed cleared.
    tp_failed: tuple[str, ...]
    #: Human-readable follow-up lines.
    followups: tuple[str, ...]


def _decision_spy(repo: Repo, session_id: int) -> SpyContext | None:
    """The SPY context of the intent's own session, or None if it has none."""
    decision = repo.get_decision(session_id)
    if (
        decision is None
        or decision.spy_close is None
        or decision.spy_ath is None
        or decision.drawdown is None
        or decision.tier is None
    ):
        return None
    return SpyContext(
        ath=decision.spy_ath,
        close=decision.spy_close,
        drawdown=decision.drawdown,
        tier=decision.tier,
    )


def _age_or_abandon(
    *,
    repo: Repo,
    intent: Intent,
    trading_date: str,
    abandon_after_sessions: int,
    label: str,
    still_unresolved: list[str],
    abandoned: list[str],
) -> None:
    """Escalate visibility by session age. Never frees cap, never resubmits."""
    age = repo.sessions_since(trading_date)
    if age >= abandon_after_sessions:
        repo.set_intent_status(intent.id, "abandoned", resolved=True)
        abandoned.append(f"{label}: abandoned after {age} later sessions")
    else:
        still_unresolved.append(f"{label}: unresolved; {age} later sessions")


def _recover_intent(
    *,
    repo: Repo,
    client: EtoroClient,
    intent: Intent,
    trading_date: str,
    abandon_after_sessions: int,
    sleep: Callable[[float], None],
    resolved: list[str],
    still_unresolved: list[str],
    abandoned: list[str],
    followups: list[str],
    tp_failed: list[str],
) -> None:
    intent_id = intent.id
    request_id = intent.request_id
    label = f"intent {intent_id} ({request_id})"

    def age_rule() -> None:
        _age_or_abandon(
            repo=repo, intent=intent, trading_date=trading_date,
            abandon_after_sessions=abandon_after_sessions, label=label,
            still_unresolved=still_unresolved, abandoned=abandoned,
        )

    try:
        res = client.recover_by_reference_id(request_id)
    except Exception as exc:
        followups.append(f"{label}: broker lookup failed ({type(exc).__name__}); cannot tell")
        res = None
    if res is None:
        age_rule()
        return

    order_row = repo.get_order_by_reference_id(request_id)
    if order_row is None:
        order_row_id = repo.insert_order(
            Order(
                intent_id=intent_id,
                session_id=intent.session_id,
                request_id=request_id,
                symbol=intent.symbol,
                side="buy",
                instrument_id=intent.instrument_id,
                order_id=res.order_id,
                reference_id=request_id,
            )
        )
    else:
        order_row_id = order_row.id

    if res.is_terminal:
        final = res
    else:
        try:
            poll = client.poll_order_to_terminal(
                order_id=res.order_id, reference_id=request_id
            )
        except Exception:
            poll = None
        if poll is None or not poll.order.is_terminal:
            last = res if poll is None else poll.order
            repo.update_order_status(
                order_row_id, last_status=last.status.value, order_id=res.order_id
            )
            if last.status == OrderStatus.PARTIALLY_FILLED:
                followups.append(
                    f"{label}: last status 5 (partially filled); a live partial "
                    "position may exist without an annotation"
                )
            age_rule()
            return
        final = poll.order

    effective_order_id = final.order_id or res.order_id
    repo.update_order_status(
        order_row_id,
        last_status=final.status.value,
        status_history=[final.status.value],
        terminal_status=final.status.value,
        fill_price=final.fill_price,
        fill_units=final.fill_units,
        fill_amount_usd=final.fill_amount_usd,
        position_id=final.position_id,
        order_id=effective_order_id,
        error=final.reject_reason,
    )

    if not final.is_filled:
        repo.set_intent_status(intent_id, "rejected", resolved=True)
        resolved.append(f"{label}: recovered rejection ({final.status.name})")
        return

    spy = _decision_spy(repo, intent.session_id)
    if spy is None:
        followups.append(f"no SPY context for intent {intent_id}; annotation skipped")
        still_unresolved.append(f"{label}: filled, no SPY context")
        return

    ladder_intent = LadderIntent(
        intent.symbol, "open", amount=intent.amount_usd,
        reason=intent.reason or "", book=intent.book, bucket_id=intent.bucket_id,
    )
    before = len(followups)
    finish_filled_buy(
        repo=repo, client=client, intent=ladder_intent, intent_id=intent_id,
        request_id=request_id, res=final, order_id=effective_order_id,
        order_row_id=order_row_id, instrument_id=intent.instrument_id,
        trading_date=date.fromisoformat(trading_date),
        spy=spy, sleep=sleep, followups=followups, tp_failed=tp_failed,
    )
    if any("not visible in portfolio" in line for line in followups[before:]):
        still_unresolved.append(f"{label}: filled, position not visible yet")
        return
    repo.set_intent_status(intent_id, "recovered", resolved=True)
    resolved.append(f"{label}: recovered fill")


def _recover_tp_clear(
    *,
    repo: Repo,
    client: EtoroClient,
    row: dict,
    sleep: Callable[[float], None],
    resolved: list[str],
    followups: list[str],
    tp_failed: list[str],
) -> None:
    tp_id = row["id"]
    position_id = row["position_id"]
    label = f"tp clear {tp_id} (position {position_id})"
    try:
        portfolio = client.get_portfolio()
    except Exception as exc:
        followups.append(f"{label}: portfolio read failed ({type(exc).__name__}); row left {row['status']}")
        return
    position = next((p for p in portfolio if p.position_id == position_id), None)
    if position is None:
        repo.set_tp_clear_status(tp_id, "failed", "position not in portfolio")
        followups.append(f"{label}: position not in portfolio")
        return
    if _tp_absent(position):
        repo.set_tp_clear_status(tp_id, "cleared", "confirmed at recovery")
        resolved.append(f"{label}: TP already absent")
        return
    repo.set_tp_clear_status(tp_id, "failed", "superseded by fresh attempt")
    if _clear_take_profit(
        repo=repo, client=client, order_row_id=row["order_id"],
        position=position, sleep=sleep,
    ):
        resolved.append(f"{label}: TP cleared by fresh attempt")
    else:
        tp_failed.append(position_id)


def recover_unfinished(
    *,
    repo: Repo,
    client: EtoroClient,
    abandon_after_sessions: int,
    sleep: Callable[[float], None] = time.sleep,
) -> RecoveryReport:
    """Resolve buys and TP clears a crash left unfinished. Never buys.

    Each unresolved buy is looked up by its ``referenceId`` only; a missing
    record or a failed lookup never permits a resend and never marks the intent
    rejected. Old fills are annotated with their own session's SPY context.
    """
    resolved: list[str] = []
    still_unresolved: list[str] = []
    abandoned: list[str] = []
    tp_failed: list[str] = []
    followups: list[str] = []

    for intent, trading_date in repo.unresolved_buy_intents():
        _recover_intent(
            repo=repo, client=client, intent=intent, trading_date=trading_date,
            abandon_after_sessions=abandon_after_sessions, sleep=sleep,
            resolved=resolved, still_unresolved=still_unresolved,
            abandoned=abandoned, followups=followups, tp_failed=tp_failed,
        )

    for row in repo.pending_tp_clears():
        _recover_tp_clear(
            repo=repo, client=client, row=row, sleep=sleep,
            resolved=resolved, followups=followups, tp_failed=tp_failed,
        )

    return RecoveryReport(
        resolved=tuple(resolved),
        still_unresolved=tuple(still_unresolved),
        abandoned=tuple(abandoned),
        tp_failed=tuple(tp_failed),
        followups=tuple(followups),
    )
