"""Probe support code (DESIGN_market_data.md, board nightwatch/handoff-2026-09-05.md §11's ask).

This package holds only *pure*, off-device-testable logic used by the live
probe under ``tools/probe_live_2026-09-11.py`` — arithmetic, key-set diffing,
partitioning. It must never import ``httpx``-calling code or anything that
touches the network; that all lives in the probe script itself, which is not
imported by the test suite. See :mod:`nightwatch.probes.live_window`.
"""

from __future__ import annotations

__all__: list[str] = []
