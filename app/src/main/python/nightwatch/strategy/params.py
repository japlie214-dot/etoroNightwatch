"""Tier caps and drawdown-tier lookup (DESIGN §1.4). Built in step 4.

# TODO(step 4): Implement tier_for_drawdown(drawdown: float) -> int using
# nightwatch.config.TIER_CAPS / TIER_DRAWDOWN_BOUNDS (already defined and
# hardcoded there — MARTINGALE_RATIO was inert and dropped, so these six
# values ARE the tier caps, not a formula to reproduce). This module is the
# single place that maps a SPY drawdown-from-ATH to a tier index (0..5) and
# the corresponding cumulative budget cap, so strategy/ladder.py never
# hand-rolls the boundary comparison twice.
"""
