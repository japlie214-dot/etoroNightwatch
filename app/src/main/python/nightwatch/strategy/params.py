"""Tier caps and drawdown-tier lookup (DESIGN §1.4).

``tier_for_drawdown`` is implemented ahead of the rest of this module (which
otherwise remains a step-4 stub -- see the module-level TODO below) because
the §4.7 adoption migration (DESIGN_market_data.md §4.7, §10.9)
needs it *today* to stamp ``entry_tier`` for the 209 adopted SCHG/UPRO
positions, and both this function and the full ladder must agree on the
tier boundaries or the ladder would silently reinterpret an adopted lot's
tier the first time it touches it. Implementing the lookup once, here, is
what keeps that from happening -- ``strategy/ladder.py`` and
``market/adopt.py`` both import this rather than each hand-rolling the
boundary comparison.

The six values below are `nightwatch.config.TIER_CAPS` /
`TIER_DRAWDOWN_BOUNDS` -- already defined and hardcoded there
(MARTINGALE_RATIO was inert and dropped, so these six values ARE the tier
caps, not a formula to reproduce, per DESIGN §1.4 "Dropped parameters").
This module does not redefine them; it only implements the boundary
comparison against them, once, so nothing else has to.

# TODO(step 4): the rest of this module -- everything besides
# tier_for_drawdown/tier_cap -- remains to be built when strategy/ladder.py
# is ported.
"""

from __future__ import annotations

from ..config import TIER_CAPS, TIER_DRAWDOWN_BOUNDS

__all__ = ["tier_for_drawdown", "tier_cap"]


def tier_for_drawdown(drawdown: float) -> int:
    """Map a SPY drawdown-from-ATH to a tier index (0..5).

    ``drawdown`` is expected as a signed fraction, ``(close - ath) / ath``,
    so it is zero at a new all-time high and increasingly negative below it
    -- never positive, since ``ath`` is by definition the largest close seen
    so far.

    Boundaries, from DESIGN.md §1.4's tier-caps table and
    ``TIER_DRAWDOWN_BOUNDS`` (``-0.10, -0.15, -0.20, -0.25, -0.35``):

    | Tier | Drawdown        |
    |------|-----------------|
    | 0    | > -0.10         |
    | 1    | -0.10 … -0.15   |
    | 2    | -0.15 … -0.20   |
    | 3    | -0.20 … -0.25   |
    | 4    | -0.25 … -0.35   |
    | 5    | <= -0.35        |

    Each boundary belongs to the *more negative* (higher-numbered) tier --
    e.g. a drawdown of exactly ``-0.10`` is tier 1, not tier 0, matching
    "Tier 0 applies while drawdown is above -0.10" (strictly above) in
    ``config.py``'s own comment on ``TIER_DRAWDOWN_BOUNDS``.
    """
    for tier, bound in enumerate(TIER_DRAWDOWN_BOUNDS):
        if drawdown > bound:
            return tier
    return len(TIER_DRAWDOWN_BOUNDS)


def tier_cap(tier: int) -> float:
    """Return the cumulative budget-fraction cap for ``tier`` (0..5)."""
    return TIER_CAPS[tier]
