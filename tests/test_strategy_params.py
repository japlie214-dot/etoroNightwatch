"""Tests for nightwatch.strategy.params.tier_for_drawdown (DESIGN.md §1.4).

Boundaries under test, from DESIGN.md §1.4's tier-caps table and
``nightwatch.config.TIER_DRAWDOWN_BOUNDS``:

    tier 0: drawdown > -0.10
    tier 1: -0.10 >= drawdown > -0.15
    tier 2: -0.15 >= drawdown > -0.20
    tier 3: -0.20 >= drawdown > -0.25
    tier 4: -0.25 >= drawdown > -0.35
    tier 5: drawdown <= -0.35
"""

from __future__ import annotations

import pytest

from nightwatch.config import TIER_CAPS, TIER_DRAWDOWN_BOUNDS
from nightwatch.strategy.params import tier_cap, tier_for_drawdown


@pytest.mark.parametrize(
    ("drawdown", "expected_tier"),
    [
        (0.0, 0),  # a new ATH itself
        (-0.01, 0),
        (-0.0999, 0),
        (-0.10, 1),  # boundary belongs to the MORE negative tier
        (-0.12, 1),
        (-0.15, 2),
        (-0.18, 2),
        (-0.20, 3),
        (-0.22, 3),
        (-0.25, 4),
        (-0.30, 4),
        (-0.35, 5),  # "at or below -0.35"
        (-0.50, 5),
        (-1.0, 5),
    ],
)
def test_tier_for_drawdown_boundaries(drawdown: float, expected_tier: int) -> None:
    assert tier_for_drawdown(drawdown) == expected_tier


def test_tier_for_drawdown_matches_config_bounds_length() -> None:
    # Six tiers (0..5) from five boundaries -- if TIER_DRAWDOWN_BOUNDS ever
    # changes shape, this fails loudly rather than silently truncating.
    assert len(TIER_DRAWDOWN_BOUNDS) == 5
    assert len(TIER_CAPS) == 6


def test_tier_cap_returns_config_value() -> None:
    for tier in range(6):
        assert tier_cap(tier) == TIER_CAPS[tier]
