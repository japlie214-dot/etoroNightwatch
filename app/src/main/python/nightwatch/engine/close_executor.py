"""UPRO full closes: persist before sending, confirm absence, apply once.

Recovery only reads broker state and applies persisted evidence; it never sends.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping, Sequence

from ..db.repo import Intent, Order, Repo
from ..etoro.auth import new_request_id
from ..etoro.client import EtoroClient
from ..etoro.models import OrderStatus, Position
from ..strategy.ladder import LadderIntent


@dataclass(frozen=True, slots=True)
class CloseOutcome:
    intent: LadderIntent
    intent_id: int | None
    request_id: str | None
    order_row_id: int | None
    order_id: str | None
    position_id: str
    outcome: str
    detail: str


@dataclass(frozen=True, slots=True)
class CloseReport:
    results: tuple[CloseOutcome, ...]
    stopped: str | None
    deferred: tuple[LadderIntent, ...]
    blocks_buys: bool
    followups: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CloseRecoveryReport:
    resolved: tuple[str, ...]
    still_unresolved: tuple[str, ...]
    followups: tuple[str, ...]
    blocks_buys: bool


def _finish_close(
    *, repo, client, intent, intent_id, request_id, order_row_id, order_id,
    deadline, now, sleep,
) -> CloseOutcome:
    pid = intent.position_ref
    raw = {}
    res = None

    def outcome(kind, detail):
        return CloseOutcome(
            intent, intent_id, request_id, order_row_id, order_id, pid, kind, detail,
        )

    def uncertain(detail, status="unresolved", kind="unresolved"):
        repo.update_order_status(
            order_row_id, last_status=res.status.value if res else None, error=detail,
        )
        repo.record_close_observation(
            intent_id=intent_id, order_row_id=order_row_id, order_id=order_id,
            position_id=pid, observed_status=status, raw=raw,
        )
        return outcome(kind, detail)

    if now() >= deadline:
        return uncertain("past deadline")
    try:
        poll = client.poll_order_to_terminal(
            order_id=order_id, kind="close",
            timeout_seconds=min(90.0, max(0.0, (deadline - now()).total_seconds())),
        )
    except Exception as exc:
        return uncertain(f"close poll failed: {type(exc).__name__}: {exc}")
    res = poll.order
    raw = res.raw
    repo.update_order_status(
        order_row_id, last_status=res.status.value,
        status_history=[res.status.value],
        terminal_status=res.status.value if res.is_terminal else None,
        fill_price=res.fill_price, fill_units=res.fill_units,
        order_id=res.order_id or order_id, error=res.reject_reason,
    )
    if now() >= deadline:
        return uncertain("past deadline")
    if res.status in (
        OrderStatus.PARTIALLY_FILLED, OrderStatus.CANCELED_PARTIALLY_FILLED,
        OrderStatus.REJECTED_PARTIALLY_FILLED,
    ):
        return uncertain("partial close status; manual check", kind="unverified")
    if poll.timed_out or not res.is_terminal:
        return uncertain("close order not terminal (timeout/error); NOT resubmitted")
    if res.status != OrderStatus.FILLED:
        repo.set_intent_status(intent_id, "rejected", resolved=True)
        return outcome("rejected", f"close ended {res.status.name}: {res.reject_reason}")
    if res.position_id != pid:
        return uncertain("close order position mismatch")

    successful = 0
    for attempt in range(10):
        if now() >= deadline:
            return uncertain("past deadline")
        try:
            positions = client.get_portfolio_strict()
        except Exception:
            pass
        else:
            successful += 1
            if now() >= deadline:
                return uncertain("past deadline")
            if not any(p.position_id == pid for p in positions):
                obs = repo.record_close_observation(
                    intent_id=intent_id, order_row_id=order_row_id, order_id=order_id,
                    position_id=pid, observed_status="confirmed_full_close",
                    units_closed=res.fill_units, close_price=res.fill_price, raw=raw,
                )
                try:
                    applied = repo.apply_confirmed_close(obs)
                except LookupError:
                    return uncertain("confirmed at broker, no open sidecar")
                if not applied:
                    return uncertain(
                        f"close already applied for position {pid}", kind="unverified",
                    )
                repo.set_intent_status(intent_id, "filled", resolved=True)
                return outcome("confirmed", "full close confirmed and applied")
        if attempt < 9:
            remaining = (deadline - now()).total_seconds()
            if remaining <= 0:
                return uncertain("past deadline")
            sleep(min(3.0, remaining))
    return uncertain(
        "position still open after confirmation" if successful else "no successful portfolio read",
        status="still_open" if successful else "unresolved",
    )


def execute_closes(
    *, repo: Repo, client: EtoroClient, session_id: int,
    intents: Sequence[LadderIntent], instrument_ids: Mapping[str, int],
    positions_by_id: Mapping[str, Position], deadline: datetime,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = time.sleep,
) -> CloseReport:
    """Refuse unsupported batches before writing; stop on any uncertain close."""
    reasons = []
    seen = set()
    for intent in intents:
        pid = intent.position_ref
        reason = ""
        if intent.side != "close" or intent.symbol != "UPRO" or intent.amount is not None:
            reason = "only UPRO full closes by position_ref are supported"
        elif not pid or pid not in positions_by_id:
            reason = "position_ref missing or absent from portfolio"
        elif (
            "UPRO" not in instrument_ids
            or positions_by_id[pid].instrument_id != instrument_ids["UPRO"]
            or positions_by_id[pid].position_id != pid
        ):
            reason = "position instrument or identity mismatch"
        if pid in seen:
            reason = "duplicate position_ref"
        seen.add(pid)
        reasons.append(reason)
    if any(reasons):
        return CloseReport(tuple(
            CloseOutcome(i, None, None, None, None, i.position_ref or "", "refused",
                         reason or "batch contains unsupported close")
            for i, reason in zip(intents, reasons)
        ), "refused: unsupported close", (), True, ())

    results = []
    followups = []
    for index, intent in enumerate(intents):
        if now() >= deadline:
            return CloseReport(tuple(results), "past deadline", tuple(intents[index:]), True,
                               tuple(followups))
        pid = intent.position_ref
        request_id = new_request_id()
        intent_id = repo.insert_intent(Intent(
            session_id=session_id, request_id=request_id,
            sequence=repo.next_intent_sequence(session_id), symbol="UPRO", side="close",
            instrument_id=instrument_ids["UPRO"], book=intent.book,
            units=positions_by_id[pid].units, position_id=pid, reason=intent.reason,
        ))
        repo.set_intent_status(intent_id, "sent")
        error = None
        try:
            order_id = client.close_position(request_id, pid, instrument_ids["UPRO"])
        except Exception as exc:
            order_id = None
            error = f"close send failed: {type(exc).__name__}: {exc}"
        order_row_id = repo.insert_order(Order(
            intent_id=intent_id, session_id=session_id, request_id=request_id,
            symbol="UPRO", side="close", instrument_id=instrument_ids["UPRO"],
            order_id=order_id, reference_id=request_id, position_id=pid, error=error,
        ))
        result = (
            CloseOutcome(intent, intent_id, request_id, order_row_id, order_id, pid,
                         "unresolved", error)
            if error else _finish_close(
                repo=repo, client=client, intent=intent, intent_id=intent_id,
                request_id=request_id, order_row_id=order_row_id, order_id=order_id,
                deadline=deadline, now=now, sleep=sleep,
            )
        )
        results.append(result)
        if result.outcome not in ("confirmed", "rejected"):
            followups.append(result.detail)
            return CloseReport(tuple(results), result.detail, tuple(intents[index + 1:]),
                               True, tuple(followups))
    return CloseReport(tuple(results), None, (),
                       any(r.outcome != "confirmed" for r in results), tuple(followups))


def recover_unfinished_closes(
    *, repo: Repo, client: EtoroClient, sleep: Callable[[float], None] = time.sleep,
) -> CloseRecoveryReport:
    """Apply durable full-close evidence first; never send or look up references."""
    resolved = []
    unresolved = []
    followups = []
    handled = set()
    for obs in repo.unapplied_confirmed_closes():
        handled.add(obs["intent_id"])
        try:
            applied = repo.apply_confirmed_close(obs["id"])
        except LookupError:
            detail = f"position {obs['position_id']}: confirmed at broker, no open sidecar"
        else:
            if applied:
                repo.set_intent_status(obs["intent_id"], "filled", resolved=True)
                resolved.append(f"intent {obs['intent_id']}: confirmed close applied")
                continue
            detail = f"close already applied for position {obs['position_id']}"
        unresolved.append(detail)
        followups.append(detail)
    for intent in repo.pending_intents():
        if intent.side != "close" or intent.id in handled:
            continue
        order = repo.get_order_by_reference_id(intent.request_id)
        if order is None or not order.order_id:
            detail = f"intent {intent.id}: close reference lookup unverified; manual check"
            unresolved.append(detail)
            followups.append(detail)
            continue
        repo.set_intent_status(intent.id, "sent")
        result = _finish_close(
            repo=repo, client=client,
            intent=LadderIntent(intent.symbol, "close", position_ref=intent.position_id,
                                book=intent.book),
            intent_id=intent.id, request_id=intent.request_id, order_row_id=order.id,
            order_id=order.order_id, deadline=datetime.max.replace(tzinfo=timezone.utc),
            now=lambda: datetime.now(timezone.utc), sleep=sleep,
        )
        if result.outcome == "confirmed":
            resolved.append(f"intent {intent.id}: {result.detail}")
        else:
            # A recovered rejection also blocks buying in this session.
            unresolved.append(f"intent {intent.id}: {result.detail}")
            if result.outcome != "rejected":
                followups.append(result.detail)
    return CloseRecoveryReport(tuple(resolved), tuple(unresolved), tuple(followups),
                               bool(unresolved))
