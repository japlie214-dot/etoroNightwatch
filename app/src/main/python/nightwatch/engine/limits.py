"""Pure submission limits, preserving the ladder's buy priority."""

from dataclasses import dataclass
from typing import Mapping, Sequence

from ..strategy.ladder import LadderIntent


@dataclass(frozen=True)
class Dropped:
    intent: LadderIntent
    reason: str


def apply_submission_limits(
    intents: Sequence[LadderIntent], *, cap_usd: float, reserved_usd: float,
    cash_usd: float, open_lots: int, max_open_lots: int,
    min_order_usd: Mapping[str, float],
) -> tuple[list[LadderIntent], list[Dropped]]:
    accepted: list[LadderIntent] = []
    dropped: list[Dropped] = []
    used = 0.0
    cash_left = cash_usd
    new_lots = 0
    stopped = False
    for intent in intents:
        if intent.side == "close":
            accepted.append(intent)
            continue
        amt = intent.amount
        minimum = min_order_usd.get(intent.symbol, 0.0)
        if amt is None or amt <= 0:
            reason = "buy without amount"
        elif amt < minimum:
            reason = f"below broker minimum ${minimum:.2f}"
        elif stopped:
            reason = "after cap stop"
        elif reserved_usd + used + amt > cap_usd + 1e-9:
            reason = (
                f"session buy cap ${cap_usd:,.0f} "
                f"(reserved ${reserved_usd:,.2f} + accepted ${used:,.2f})"
            )
            stopped = True
        elif cash_left - amt < -1e-9:
            reason = f"insufficient cash ${cash_left:,.2f}"
            stopped = True
        elif open_lots + new_lots + 1 > max_open_lots:
            reason = f"max_open_lots {max_open_lots}"
            stopped = True
        else:
            accepted.append(intent)
            used += amt
            cash_left -= amt
            new_lots += 1
            continue
        dropped.append(Dropped(intent, reason))
    return accepted, dropped
