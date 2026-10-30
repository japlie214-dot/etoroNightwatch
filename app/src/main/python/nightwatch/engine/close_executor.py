"""UPRO full closes: persist before sending, confirm absence, apply once.

Recovery only reads broker state and applies persisted evidence; it never sends.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping, Sequence

from ..db.repo import Intent, Order, Repo
from ..etoro.auth import new_request_id
from ..etoro.client import EtoroClient
from ..etoro.errors import EtoroError
from ..etoro.models import (
    MIN_POSITION_VALUE_USD, OrderStatus, Position, partial_close_leaves_legal_remainder,
)
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


@dataclass(frozen=True, slots=True)
class SchgSellPlan:
    legs: tuple[tuple[str, float, float | None], ...]
    planned_value: float
    shortfall: float
    notes: tuple[str, ...]


def plan_schg_sell(
    positions: Sequence[Position], *, amount: float, price: float, instrument_id: int,
    min_position_value: float = MIN_POSITION_VALUE_USD, allow_partial: bool = True,
) -> SchgSellPlan:
    """Order by entry cost, size by market value, and floor partial units."""
    if not math.isfinite(amount) or not math.isfinite(price) or amount <= 0 or price <= 0:
        return SchgSellPlan((), 0.0, max(0.0, amount), ("non-positive or invalid amount/price",))
    candidates = sorted(
        (p for p in positions if p.instrument_id == instrument_id and p.units > 0),
        key=lambda p: (-p.entry_price, p.position_id),
    )
    legs = []
    notes = []
    remaining = amount
    for p in candidates:
        if remaining <= 0:
            break
        value = p.units * price
        if value <= remaining + 0.005:
            legs.append((p.position_id, p.units, None))
            remaining -= value
            continue
        if not allow_partial:
            notes.append("partials disabled; shortfall")
            break
        units = math.floor(remaining / price * 1_000_000) / 1_000_000
        close_value = units * price
        if close_value < min_position_value:
            notes.append("slice below minimum; shortfall")
            break
        if partial_close_leaves_legal_remainder(
            value, close_value, min_position_value=min_position_value,
        ):
            legs.append((p.position_id, p.units, units))
            remaining -= close_value
        else:
            legs.append((p.position_id, p.units, None))
            remaining -= value
            notes.append("full lot overshoot to avoid illegal remainder")
        break
    if not candidates:
        notes.append("no candidates")
    if remaining > 0 and not notes:
        notes.append("shortfall")
    return SchgSellPlan(tuple(legs), amount - remaining, max(0.0, remaining), tuple(notes))


def _finish_schg_close(
    *, repo, client, row, intent, request_id, deadline, now, sleep,
) -> CloseOutcome:
    """Read-only broker state machine: lookup evidence is mandatory to confirm."""
    pid = row["position_id"]
    deducted = row["units_deducted"]
    partial = deducted is not None
    raw = {}
    res = None
    units_after = None
    slice_id = None
    history = []

    def finish(status, detail):
        repo.finish_schg_close_leg(
            row["id"], status=status,
            intent_status={"confirmed": "filled", "rejected": "rejected"}.get(status, "sent"),
            resolved=status in ("confirmed", "rejected"), units_after=units_after,
            slice_position_id=slice_id,
            close_price=res.fill_price if res and res.status == OrderStatus.FILLED else None,
            proceeds=res.fill_amount_usd if res and res.status == OrderStatus.FILLED else None,
            raw={**raw, "units_after": units_after, "detail": detail},
        )
        if row["order_row_id"] is not None:
            repo.update_order_status(
                row["order_row_id"], last_status=res.status.value if res else None,
                error=detail if status != "confirmed" else None,
            )
        return CloseOutcome(intent, row["intent_id"], request_id, row["order_row_id"],
                            row["order_id"], pid, status, detail)

    if not row["order_id"]:
        return finish("unresolved", "send unverified; manual check")
    lookup_end = min(deadline, now() + timedelta(seconds=60))
    while now() < lookup_end:
        try:
            res = client.lookup_close_order(
                row["order_id"], expected_position_id=pid if partial else None,
            )
        except Exception as exc:
            if not isinstance(exc, EtoroError) or getattr(exc, "status_code", None) != 404:
                return finish("unresolved", f"close lookup failed: {type(exc).__name__}: {exc}")
        else:
            raw = dict(res.raw)
            history.append(res.status.value)
            repo.update_order_status(
                row["order_row_id"], last_status=res.status.value, status_history=history,
                terminal_status=res.status.value if res.is_terminal else None,
                fill_price=res.fill_price, fill_units=res.fill_units,
                fill_amount_usd=res.fill_amount_usd, error=res.reject_reason,
            )
            if now() >= deadline:
                return finish("unresolved", "past deadline")
            if res.status in (OrderStatus.PARTIALLY_FILLED, OrderStatus.CANCELED_PARTIALLY_FILLED,
                              OrderStatus.REJECTED_PARTIALLY_FILLED):
                return finish("unverified", "partial close status; manual check")
            if res.status in (OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED):
                return finish("rejected", f"close ended {res.status.name}: {res.reject_reason}")
            if res.status == OrderStatus.FILLED:
                if res.position_id != pid:
                    return finish("unresolved", "close order position mismatch")
                if partial:
                    slices = [p for p in raw.get("positions", [])
                              if str(p.get("positionID")) != pid]
                    if not slices:
                        return finish("unresolved", "partial close returned single position")
                    if (res.fill_units is None or not math.isfinite(res.fill_units)
                            or abs(res.fill_units - deducted) > 2e-6 + 1e-6 * deducted):
                        return finish("unresolved", "slice units mismatch")
                    slice_id = str(slices[0]["positionID"])
                break
        remaining = (lookup_end - now()).total_seconds()
        if remaining > 0:
            sleep(min(2.0, remaining))
    if now() >= deadline:
        return finish("unresolved", "past deadline")
    filled = res is not None and res.status == OrderStatus.FILLED
    portfolio_end = min(deadline, now() + timedelta(seconds=90))
    absent_at = None
    while now() < portfolio_end:
        try:
            positions = client.get_portfolio_strict()
        except Exception:
            absent_at = None
        else:
            if now() >= deadline:
                return finish("unresolved", "past deadline")
            position = next((p for p in positions if p.position_id == pid), None)
            units_after = position.units if position is not None else None
            if partial:
                if position is not None:
                    expected = row["units_before"] - deducted
                    if abs(position.units - expected) <= 2e-6:
                        if filled:
                            return finish("confirmed", "partial close confirmed")
                    elif abs(position.units - row["units_before"]) > 2e-6:
                        return finish("unresolved" if filled else "unverified", "unexpected units")
            elif position is None:
                if filled and absent_at is not None and (now() - absent_at).total_seconds() >= 3:
                    repo.record_close_observation(
                        intent_id=row["intent_id"], order_row_id=row["order_row_id"],
                        order_id=row["order_id"], position_id=pid,
                        observed_status="confirmed_closed", raw=raw,
                    )
                    units_after = 0.0
                    return finish("confirmed", "full close confirmed")
                absent_at = now()
            else:
                absent_at = None
        remaining = (portfolio_end - now()).total_seconds()
        if remaining > 0:
            sleep(min(3.0, remaining))
    if now() >= deadline:
        return finish("unresolved", "past deadline")
    return finish("unresolved" if filled else "unverified",
                  "portfolio confirmation timed out" if filled else "lookup unverified; portfolio evidence only")


def execute_schg_sell(
    *, repo: Repo, client: EtoroClient, session_id: int, intent: LadderIntent,
    instrument_id: int, positions: Sequence[Position], price: float, deadline: datetime,
    allow_partial: bool = True,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = time.sleep,
) -> CloseReport:
    """Caller must supply a fresh strict portfolio; each persisted leg is sent once."""
    if (intent.symbol != "SCHG" or intent.side != "close" or intent.position_ref
            or intent.amount is None or not math.isfinite(intent.amount) or intent.amount <= 0):
        result = CloseOutcome(intent, None, None, None, None, intent.position_ref or "",
                              "refused", "only SCHG amount closes without position_ref are supported")
        return CloseReport((result,), result.detail, (), True, ())
    plan = plan_schg_sell(positions, amount=intent.amount, price=price,
                          instrument_id=instrument_id, allow_partial=allow_partial)
    leg_intents = tuple(LadderIntent("SCHG", "close", position_ref=pid, reason=intent.reason)
                        for pid, _, _ in plan.legs)
    results = []
    for index, (pid, before, deducted) in enumerate(plan.legs):
        if now() >= deadline:
            return CloseReport(tuple(results), "past deadline", leg_intents[index:], True, ())
        request_id = new_request_id()
        intent_id, ledger_id = repo.begin_schg_close_leg(
            Intent(session_id, request_id, repo.next_intent_sequence(session_id), "SCHG", "close",
                   instrument_id=instrument_id, units=before if deducted is None else deducted,
                   position_id=pid, reason=intent.reason),
            position_id=pid, units_before=before, units_deducted=deducted,
        )
        error = None
        try:
            order_id = client.close_position(request_id, pid, instrument_id, deducted)
        except Exception as exc:
            order_id = None
            error = f"close send failed: {type(exc).__name__}: {exc}"
        order_row_id = repo.insert_order(Order(
            intent_id, session_id, request_id, "SCHG", "close", instrument_id=instrument_id,
            order_id=order_id, reference_id=request_id, position_id=pid, error=error,
        ))
        repo.set_partial_close_order(ledger_id, order_row_id, order_id)
        if error:
            repo.finish_schg_close_leg(ledger_id, status="unresolved", intent_status="sent",
                                       resolved=False, raw={"detail": error})
            result = CloseOutcome(leg_intents[index], intent_id, request_id, order_row_id,
                                  order_id, pid, "unresolved", error)
        else:
            row = next(r for r in repo.pending_partial_closes() if r["id"] == ledger_id)
            result = _finish_schg_close(
                repo=repo, client=client, row=row, intent=leg_intents[index], request_id=request_id,
                deadline=deadline, now=now, sleep=sleep,
            )
        results.append(result)
        if result.outcome != "confirmed":
            return CloseReport(tuple(results), result.detail, leg_intents[index + 1:], True,
                               (result.detail,) if result.outcome != "rejected" else ())
    short = plan.shortfall > 0.005 or not plan.legs
    return CloseReport(tuple(results), "; ".join(plan.notes) if short else None, (), short, ())


def recover_unfinished_closes(
    *, repo: Repo, client: EtoroClient, sleep: Callable[[float], None] = time.sleep,
) -> CloseRecoveryReport:
    """Apply durable full-close evidence first; never send or look up references."""
    resolved = []
    unresolved = []
    followups = []
    handled = set(repo.repair_confirmed_schg_closes())
    resolved.extend(f"intent {iid}: confirmed SCHG close repaired" for iid in sorted(handled))
    pending = {i.id: i for i in repo.pending_intents()}
    for row in repo.pending_partial_closes():
        saved = pending.get(row["intent_id"])
        if saved is None or saved.symbol != "SCHG" or saved.side != "close":
            continue
        handled.add(saved.id)
        result = _finish_schg_close(
            repo=repo, client=client, row=row,
            intent=LadderIntent("SCHG", "close", position_ref=row["position_id"], reason=saved.reason),
            request_id=saved.request_id,
            deadline=datetime.now(timezone.utc) + timedelta(seconds=150),
            now=lambda: datetime.now(timezone.utc), sleep=sleep,
        )
        if result.outcome == "confirmed":
            resolved.append(f"intent {saved.id}: {result.detail}")
        else:
            unresolved.append(f"intent {saved.id}: {result.detail}")
            followups.append(result.detail)
    for saved in pending.values():
        if saved.symbol == "SCHG" and saved.side == "close" and saved.id not in handled:
            detail = f"intent {saved.id}: SCHG close intent without ledger; manual check"
            unresolved.append(detail)
            followups.append(detail)
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
        if intent.side != "close" or intent.symbol == "SCHG" or intent.id in handled:
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
