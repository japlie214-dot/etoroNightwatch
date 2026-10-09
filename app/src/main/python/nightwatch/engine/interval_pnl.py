"""Pure interval P&L and independent-market attribution for managed positions."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from nightwatch.db.repo import PortfolioSnapshot


@dataclass(frozen=True)
class PosObs:
    position_id: str
    instrument_id: int
    units: float
    close_rate: float | None
    pnl: float | None
    fees: float
    is_buy: bool
    leverage: float


@dataclass(frozen=True)
class ClosedLot:
    position_id: str
    instrument_id: int
    units: float
    net_profit: float
    fees: float
    open_date: str | None
    close_date: str | None


@dataclass(frozen=True)
class IntervalResult:
    state: str
    pnl_usd: float | None
    pnl_pct: float | None
    explained_usd: float | None
    residual_usd: float | None
    alert_a: bool
    alert_c: bool
    reasons: tuple[str, ...]
    lines: tuple[str, ...]


def observe(snap: PortfolioSnapshot, managed_ids: frozenset[int]) -> dict[str, PosObs]:
    if "pnl" not in snap.raw:
        raise ValueError("snapshot has no raw pnl")
    observations = {}
    for row in snap.raw["pnl"]["clientPortfolio"]["positions"]:
        if row["instrumentID"] not in managed_ids:
            continue
        mark = row.get("unrealizedPnL") or {}
        position_id = str(row["positionID"])
        observations[position_id] = PosObs(
            position_id, row["instrumentID"], row["units"],
            mark.get("closeRate"), mark.get("pnL"),
            row.get("totalFees", 0) + row.get("totalExternalFees", 0),
            row["isBuy"], row["leverage"],
        )
    return observations


def _utc(s: str) -> datetime:
    result = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result


def compute_interval(
    prev: PortfolioSnapshot | None,
    cur: PortfolioSnapshot,
    *,
    managed_ids: frozenset[int],
    closed: Sequence[ClosedLot] | None,
    entry_marks: Mapping[str, float],
    exit_marks: Mapping[str, float],
    loss_alert_usd: float = 500.0,
    residual_halt_usd: float = 50.0,
) -> IntervalResult:
    if prev is None:
        return IntervalResult("no_baseline", None, None, None, None, False, False,
                              ("no previous snapshot",), ())
    O0 = observe(prev, managed_ids)
    O1 = observe(cur, managed_ids)
    reasons: list[str] = []
    incomplete = False
    integrity = False
    pnl_parts: list[float] = []
    expl_parts: list[float] = []
    lines: list[str] = []
    if closed is None:
        incomplete = True
        reasons.append("trade history not supplied: closes and round trips unknown")
        closed = ()
    if prev.raw.get("complete") is not True or cur.raw.get("complete") is not True:
        incomplete = True
        reasons.append("snapshot marks incomplete")

    skipped = set()
    for id in sorted(O0.keys() | O1.keys()):
        p0, p1 = O0.get(id), O1.get(id)
        if any(not p.is_buy or p.leverage != 1 for p in (p0, p1) if p is not None):
            incomplete = True
            skipped.add(id)
            reasons.append(f"position {id}: short or leveraged, not modelled")
            continue
        expl = None
        if p0 is not None and p1 is not None:
            kind = "held"
            if abs(p1.units - p0.units) > 1e-9:
                integrity = True
                reasons.append(f"position {id}: units changed {p0.units}->{p1.units}")
                continue
            if any(v is None for v in (p0.pnl, p1.pnl, p0.close_rate, p1.close_rate)):
                incomplete = True
                reasons.append(f"position {id}: missing mark")
                continue
            pnl_c = (p1.pnl - p0.pnl) + (p1.fees - p0.fees)
            expl = p1.units * (p1.close_rate - p0.close_rate)
        elif p1 is not None:
            kind = "opened since"
            if p1.pnl is None or p1.close_rate is None:
                incomplete = True
                reasons.append(f"position {id}: missing mark")
                continue
            pnl_c = p1.pnl + p1.fees
            Be = entry_marks.get(id)
            if Be is None:
                incomplete = True
                reasons.append(f"position {id}: opened since, no independent entry mark")
            else:
                expl = p1.units * (p1.close_rate - Be)
        else:
            kind = "closed since"
            lot = next((lot for lot in closed if lot.position_id == id), None)
            if lot is None:
                incomplete = True
                reasons.append(f"position {id}: closed since, realized P&L pending in history")
                continue
            if p0.pnl is None or p0.close_rate is None:
                incomplete = True
                reasons.append(f"position {id}: missing mark")
                continue
            pnl_c = (lot.net_profit + lot.fees) - (p0.pnl + p0.fees)
            Bx = exit_marks.get(id)
            if Bx is None:
                incomplete = True
                reasons.append(f"position {id}: closed since, no independent exit mark")
            else:
                expl = lot.units * (Bx - p0.close_rate)
        pnl_parts.append(pnl_c)
        if expl is not None:
            expl_parts.append(expl)
        market = "n/a" if expl is None else f"{expl:+.2f}"
        lines.append(f"{id} {kind}: pnl {pnl_c:+.2f}, market {market}")

    for lot in closed:
        id = lot.position_id
        if (id in O0 or id in O1 or lot.instrument_id not in managed_ids
                or lot.open_date is None or _utc(lot.open_date) <= _utc(prev.taken_at)):
            continue
        pnl_c = lot.net_profit + lot.fees
        pnl_parts.append(pnl_c)
        Be, Bx = entry_marks.get(id), exit_marks.get(id)
        expl = None
        if Be is None or Bx is None:
            incomplete = True
            reasons.append(f"position {id}: round trip, missing independent mark")
        else:
            expl = lot.units * (Bx - Be)
            expl_parts.append(expl)
        market = "n/a" if expl is None else f"{expl:+.2f}"
        lines.append(f"{id} round trip: pnl {pnl_c:+.2f}, market {market}")

    pnl_usd = sum(pnl_parts, 0.0)
    state = "integrity" if integrity else "incomplete" if incomplete else "complete"
    explained_usd = sum(expl_parts, 0.0) if state == "complete" else None
    residual_usd = pnl_usd - explained_usd if state == "complete" else None
    rows = {row.position_id: row for row in prev.positions}
    base = 0.0
    for id, p0 in O0.items():
        if id in skipped:
            continue
        row = rows.get(id)
        if row is None or row.amount is None or p0.pnl is None:
            base = 0.0
            break
        base += row.amount + p0.pnl
    pnl_pct = 100 * pnl_usd / base if base > 0 else None
    return IntervalResult(
        state, pnl_usd, pnl_pct, explained_usd, residual_usd,
        state == "complete" and pnl_usd <= -loss_alert_usd,
        state == "complete" and residual_usd <= -residual_halt_usd,
        tuple(reasons), tuple(lines),
    )
