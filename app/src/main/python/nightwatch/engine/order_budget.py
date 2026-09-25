"""A latency-derived time gate for the sell-to-target loop's close orders
(DESIGN.md §6.C's `max_orders_per_session`, DESIGN_session.md §4.8/§4.9).

>>> SCAFFOLDING NOTICE -- READ BEFORE CALLING ANYTHING HERE <<<
This module is deliberately SIMPLE SCAFFOLDING. It exists so the session has
somewhere to land Friday 2026-09-11's order-latency measurement
(RUNBOOK_LIVE_WINDOW_2026-09-11.md §8(b), ``probes/order_latency.py``) the
moment it is available. It is NOT settled architecture. It is expected to be
redesigned, possibly completely, once that measurement lands and once
``engine/session.py``'s real orchestration (still an unbuilt TODO stub as of
this writing) exists to call it from. Nothing else in this codebase should be
built to depend on this module's exact shape -- only on the fact that *some*
latency-derived time gate exists here.

**Why a time gate instead of an order-count cap.** DESIGN.md §6.C's
`max_orders_per_session` is an arbitrary integer with no stated basis --
DESIGN_session.md §4.9 explains why that number cannot be chosen honestly: the
book is bimodal (202 adopted lots averaging ~$222 market value; future
$50-origin lots), so the SAME dollar `sell_amount` touches ~2-3 lots today but
will touch ~10 as the book fills with $50-origin lots -- an order count that
is structurally, not transiently, growing. A fixed integer cap sized for
today's book either bites too early once the book matures, or was sized
loosely enough to not bite at all, in which case it was never really a cap.
What actually matters is wall-clock fit inside the T-30 window (DESIGN §4.1,
DESIGN_session.md §3.2), which is a function of *time*, not *count*: how many
close orders fit before the deadline depends on how long each one actually
takes, which is exactly what Friday's harness measures
(``probes/order_latency.py``'s `LegStatistics`/`project_session_budget`).
This module consumes that same measurement shape at the per-order decision
point, one order at a time, instead of pre-multiplying it into a single
session-wide integer the way `project_session_budget` does for the *design*
question ("does this fit at all"). The two are complementary, not
duplicates: `project_session_budget` answers "should we even attempt this
book size," offline, once; `check_order_budget` answers "may THIS order still
go," online, before every submission.

**The mechanism (per the task, applied literally):**

    now + p95_latency_per_order < hard_deadline

If it does not fit, the caller must STOP placing further orders and ACCEPT
the shortfall -- the same fail-soft degradation DESIGN_session.md §4.8's
sell-to-target loop already performs on its own iteration cap and $10-floor
exhaustion ("stop looping, accept the remaining shortfall... phase B re-reads
actual cash and simply buys less"). This module never aborts or unwinds work
already done: it only ever says whether to place ONE MORE order, never
whether to reverse ones already placed. `hard_deadline` must be derived from
the market calendar (`market/calendar.py`'s T-30 machinery, §4.1) -- never a
hardcoded wall-clock time -- see :func:`compute_hard_deadline`.

**The placeholder latency is deliberately loud, not silent (see task's own
"most important requirement").** :data:`PLACEHOLDER_P95_LATENCY_SECONDS` is
NOT Friday's real measurement -- that measurement does not exist yet as of
this writing. Its name says PLACEHOLDER, and its value is derived from
`probes/order_latency.py`'s own :data:`~nightwatch.probes.order_latency.
T30_BUDGET_SECONDS` (double it) so that it is unambiguously larger than the
entire T-30 budget rather than a second, independently-chosen magic number
that could drift out of sync with it. Because the gate is
``now + p95_latency_seconds < hard_deadline`` and no order-placing instant
inside the T-30 window can be more than `T30_BUDGET_SECONDS` before
`hard_deadline`, a placeholder this large makes ``projected_finish`` land
past `hard_deadline` for every order, from the very first one -- i.e. it
fails CLOSED for the *entire* session, not "almost immediately" as an
earlier, smaller placeholder value would have (a 300-second placeholder,
for instance, would have permitted orders for the first ~25 minutes of a
30-minute window and only refused in the final five -- fail-open for most
of the session, the opposite of the intended guarantee). :func:`check_order_budget`
logs an ``ERROR``-level, unmistakable message every single time it is called
with that exact value, so a session run against the unreplaced placeholder
produces a loud, visible signal in the log/Telegram path rather than quietly
degrading. Whoever wires Friday's measurement in must replace the value
passed as `p95_latency_seconds` (e.g. from a `RiskConfig`/config.toml field
the session orchestrator reads, not built here -- see the module's own
`__all__` for what IS exported) with the real p95 from
`LegStatistics.mean_s`/`summarize_leg`'s output for the close leg -- §4.8's
own text: "per-order latency ... never been measured" until Friday.

**No live production caller exists yet.** `engine/session.py` (DESIGN_session
.md §12's module layout) is, as of this writing, a comment-only TODO stub
with no `run_session` implementation, and `run_preflight()` does not exist
either (`engine/preflight.py`'s own module docstring says the same of gates
10/11). This module is therefore reachable and tested, exactly like gates 10
and 11 in `engine/preflight.py`, but **not wired into anything that runs**.
Stated plainly per the task's own instruction, not implied: shipping this
file changes nothing about what a live session does until something calls it.

**Style note.** Pure functions over explicit inputs (a clock reading, a
deadline, a latency estimate) -- no wall-clock sleeping, no broker calls, no
global state -- in the style of `preflight.py`'s gate 10/11 and
`probes/order_latency.py`'s `project_session_budget`, so every branch here is
testable with hand-built inputs, no live account, no `time.sleep`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final

from ..market.calendar import session_close_datetime
from ..probes.order_latency import T30_BUDGET_SECONDS

__all__ = [
    "PLACEHOLDER_P95_LATENCY_SECONDS",
    "BudgetDecision",
    "check_order_budget",
    "compute_hard_deadline",
    "INTENT_COUNT_BUG_CEILING_MULTIPLE",
    "ImplausibleIntentCountError",
    "assert_plausible_intent_count",
]

_logger = logging.getLogger("nightwatch.engine.order_budget")

#: NOT A MEASURED VALUE -- REPLACE BEFORE RELYING ON THIS GATE FOR REAL.
#: Friday 2026-09-11's DEMO-account harness (RUNBOOK_LIVE_WINDOW_2026-09-11.md
#: §8(b), `probes/order_latency.py`) has not run as of this writing, so no
#: real close-leg p95 exists to put here. Deliberately set to DOUBLE
#: `T30_BUDGET_SECONDS` (1800.0s -- i.e. 3600.0s / 60 minutes) rather than to
#: an independently-chosen number, so the value is unambiguously, structurally
#: larger than the entire T-30 budget and the two constants can never drift
#: apart. Because no order-placing instant inside the T-30 window can be more
#: than `T30_BUDGET_SECONDS` before `hard_deadline`, a placeholder this large
#: makes `check_order_budget`'s gate (``now + p95_latency_seconds <
#: hard_deadline``) refuse EVERY order for the WHOLE session -- fail CLOSED
#: from the very first order, not "almost immediately" (a materially weaker,
#: and false, claim: a smaller placeholder such as 300.0s would instead fail
#: OPEN for most of the session, only refusing in the final few minutes).
#: That means a session run against the unreplaced placeholder cannot place
#: any order at all, which is the intended, loud, unmistakable degradation
#: rather than a fabricated "latency is probably fine" assumption. Once
#: Friday's measurement exists, replace the value actually passed as
#: `check_order_budget`'s `p95_latency_seconds` argument (a config-supplied
#: number, not this constant -- this constant is only ever a fallback/default
#: a caller might reach for) with the real measured p95, e.g. from
#: `summarize_leg`'s `LegStatistics.mean_s`/a proper percentile over
#: `LegStatistics.values_s` for the close leg.
PLACEHOLDER_P95_LATENCY_SECONDS: Final[float] = T30_BUDGET_SECONDS * 2.0


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    """Whether one more close order may still be submitted.

    Deliberately smaller than `preflight.py`'s `GateResult` -- this is a
    per-order decision made many times inside one sell-to-target pass, not a
    once-per-session gate, so it carries only what the caller needs to log
    and act on, not a `session_id` or a `details` dict.
    """

    allowed: bool
    reason: str
    now: datetime
    hard_deadline: datetime
    p95_latency_seconds: float
    projected_finish: datetime
    """``now + p95_latency_seconds`` -- the exact instant this order's own
    result is projected to land, compared against `hard_deadline` to produce
    `allowed`. Surfaced so a caller logging a refusal can state the actual
    overrun, not just a boolean."""
    used_placeholder_latency: bool
    """True iff `p95_latency_seconds` was exactly
    :data:`PLACEHOLDER_P95_LATENCY_SECONDS` -- i.e. Friday's measurement has
    not been wired in yet. `check_order_budget` already logs loudly in this
    case; this field lets a caller (or a test) assert on it too."""


def check_order_budget(
    *,
    now: datetime,
    hard_deadline: datetime,
    p95_latency_seconds: float,
) -> BudgetDecision:
    """The gate itself: ``now + p95_latency_per_order < hard_deadline``.

    All three arguments are required, keyword-only, with no defaults --
    exactly board nightwatch/handoff-2026-09-06.md §1's fix for the §3a ATH floor's "silently
    inert" defect (`market/window.py`'s `open_lot_entry_spy_ath`): a caller
    that forgets to pass one of these gets a `TypeError` at the call site,
    never a quietly-wrong default that would let every order through
    ungated.

    Args:
        now: The wall-clock instant this decision is being made at,
            immediately before submitting the next close order --
            DESIGN_session.md §4.8 point 4's "re-checks... immediately before
            submission" principle, applied to time exactly as that section
            already applies it to cash. Must be tz-aware and in the same zone
            family as `hard_deadline` (both should be `America/New_York` via
            `market/calendar.py`, or both UTC -- this function does not
            normalize timezones itself, so a caller mixing an aware and a
            naive datetime, or two different zones, gets whatever Python's
            own datetime subtraction does with them, not a silent fix-up).
        hard_deadline: The instant beyond which no further close order may be
            projected to finish. Build this with :func:`compute_hard_deadline`
            (T-30 minus a configured margin, derived from the market
            calendar) -- never a hardcoded time of day.
        p95_latency_seconds: The measured (or, until Friday, placeholder --
            see :data:`PLACEHOLDER_P95_LATENCY_SECONDS`) p95 time-to-terminal
            for one close order, i.e. the same shape of number
            `probes/order_latency.py`'s `LegStatistics`/`summarize_leg`
            produces for the close leg. Must be >= 0.

    Returns:
        A :class:`BudgetDecision`. ``allowed=False`` means: per DESIGN_session
        .md §4.8's fail-soft pattern, the caller must STOP placing further
        orders in this pass and ACCEPT the remaining shortfall -- never abort
        or unwind orders already placed, and never retry this same check in a
        loop hoping `now` will somehow improve.

    Raises:
        ValueError: if `p95_latency_seconds` is negative -- a negative
            latency is not a measurement, it is a bug in whatever computed
            it, and this gate refuses to reason about it rather than
            silently treating it as "instant."
    """
    if p95_latency_seconds < 0:
        raise ValueError(
            f"p95_latency_seconds must be >= 0, got {p95_latency_seconds!r}"
        )

    used_placeholder = p95_latency_seconds == PLACEHOLDER_P95_LATENCY_SECONDS
    if used_placeholder:
        _logger.error(
            "ORDER BUDGET GATE IS RUNNING ON THE UNREPLACED PLACEHOLDER "
            "LATENCY (%.1fs) -- Friday 2026-09-11's real measurement "
            "(probes/order_latency.py) has not been wired into this call. "
            "This is expected to fail EVERY order this gate is asked about, "
            "for the whole session, by design -- see order_budget.py's "
            "PLACEHOLDER_P95_LATENCY_SECONDS.",
            p95_latency_seconds,
        )

    projected_finish = now + timedelta(seconds=p95_latency_seconds)
    allowed = projected_finish < hard_deadline

    if allowed:
        reason = (
            f"projected finish {projected_finish.isoformat()} is before hard "
            f"deadline {hard_deadline.isoformat()} (p95={p95_latency_seconds:.1f}s) "
            "-- order may proceed"
        )
    else:
        overrun = (projected_finish - hard_deadline).total_seconds()
        reason = (
            f"projected finish {projected_finish.isoformat()} would be "
            f"{overrun:.1f}s past hard deadline {hard_deadline.isoformat()} "
            f"(p95={p95_latency_seconds:.1f}s) -- STOP placing further orders "
            "this pass and accept the remaining shortfall (DESIGN_session.md "
            "§4.8's fail-soft pattern); do not abort or unwind work already done"
        )

    return BudgetDecision(
        allowed=allowed,
        reason=reason,
        now=now,
        hard_deadline=hard_deadline,
        p95_latency_seconds=p95_latency_seconds,
        projected_finish=projected_finish,
        used_placeholder_latency=used_placeholder,
    )


def compute_hard_deadline(trading_date: date, *, margin_minutes: float) -> datetime:
    """``hard_deadline`` = T-30 minus a configured margin, derived from the
    market calendar (DESIGN §4.1) -- never a hardcoded time of day.

    T-30 itself is not re-derived here: it is
    `probes/order_latency.py`'s own :data:`~nightwatch.probes.order_latency.
    T30_BUDGET_SECONDS` (DESIGN_session.md §3.2/§14 item 6/RUNBOOK §8(b)'s
    "the two-phase session's whole margin against the market close"), so this
    module and Friday's measurement harness share one single definition of
    what T-30 means rather than two constants that could silently drift
    apart. `margin_minutes` narrows that window further -- this gate's own
    extra safety margin on top of T-30, not a replacement for it.

    Args:
        trading_date: The NYSE trading date this session is running for.
            `market/calendar.py`'s `session_close_datetime` resolves this to
            the correct 16:00/13:00 ET close, honoring early-close days --
            this function does not special-case half-days itself.
        margin_minutes: Extra margin subtracted beyond T-30, keyword-only
            with no default so a caller must make an explicit choice rather
            than inherit an implicit "0 extra margin" by omission. Must be
            >= 0 -- a negative margin would mean *extending* past T-30, which
            is not what this function is for.

    Returns:
        A tz-aware `America/New_York` datetime: the hard deadline to pass as
        `check_order_budget`'s `hard_deadline`.

    Raises:
        ValueError: if `margin_minutes` is negative.
    """
    if margin_minutes < 0:
        raise ValueError(f"margin_minutes must be >= 0, got {margin_minutes!r}")
    close_dt = session_close_datetime(trading_date)
    return close_dt - timedelta(seconds=T30_BUDGET_SECONDS) - timedelta(minutes=margin_minutes)


# ---------------------------------------------------------------------------
# Bug-catcher circuit breaker -- NOT a tuning knob, NOT max_orders_per_session
# reincarnated under a new name (see module docstring's opening section for
# why this module replaces that config key with a time gate instead).
# ---------------------------------------------------------------------------

#: Sized off the strategy's own `StrategyConfig.max_lots` (60, DESIGN §1.4) --
#: not a fresh number invented for this module. A single sell-to-target pass
#: (DESIGN_session.md §4.8) should never need to touch anywhere near the
#: entire book's lot count in one go; the ~2-3-to-~10 lots §4.9 actually
#: projects is far below this. This multiple exists ONLY to catch a BUG that
#: produces an absurd, unbounded intent count (e.g. a loop that fails to
#: terminate, or a lot-selection defect that keeps re-selecting the same
#: lots) -- it is deliberately set far above any plausible legitimate count
#: so it never fires on real trading activity, and deliberately NOT exposed
#: as a `RiskConfig` field: unlike `max_notional_per_session_usd`/
#: `max_open_lots` (real, intentional risk knobs, DESIGN §6.C), tightening or
#: loosening this number is not a risk decision anyone should be making --
#: making it tunable would invite exactly the "arbitrary integer with no
#: stated basis" problem DESIGN_session.md §4.9 already flagged against
#: `max_orders_per_session`, one module over.
INTENT_COUNT_BUG_CEILING_MULTIPLE: Final[int] = 5


class ImplausibleIntentCountError(Exception):
    """Raised by :func:`assert_plausible_intent_count` -- a bug, not a risk
    event. Distinct from `BudgetDecision.allowed=False`: the time gate's
    "not allowed" is an expected, fail-soft outcome the caller degrades
    gracefully from (DESIGN_session.md §4.8); this exception means the
    intent count itself is nonsensical and something upstream is broken --
    there is no graceful degradation for that, the session must stop and be
    investigated, not quietly cap itself at the ceiling and continue.
    """


def assert_plausible_intent_count(intent_count: int, *, max_lots: int) -> None:
    """Bug-catcher: raise if `intent_count` is not just large but absurd.

    This is explicitly NOT `max_orders_per_session` under a new name and NOT
    a tuning knob (see :data:`INTENT_COUNT_BUG_CEILING_MULTIPLE`'s own
    docstring) -- it exists purely to turn a runaway-loop or lot-selection
    bug into a loud, early exception instead of a session that quietly tries
    to place hundreds of orders. `max_lots` must be passed explicitly by the
    caller (e.g. `StrategyConfig.max_lots`) rather than defaulted here, so
    this function's ceiling visibly tracks whatever the live strategy config
    actually says `MAX_LOTS` is, not a number frozen into this module at
    import time.

    Args:
        intent_count: How many close (or open) order intents the caller is
            about to act on in one pass/session.
        max_lots: The strategy's own lot ceiling (`StrategyConfig.max_lots`,
            default 60, DESIGN §1.4). Required, keyword-only, no default --
            same discipline as `check_order_budget`'s arguments.

    Raises:
        ImplausibleIntentCountError: if `intent_count` exceeds
            `max_lots * INTENT_COUNT_BUG_CEILING_MULTIPLE`.
        ValueError: if `intent_count` or `max_lots` is negative -- neither is
            a meaningful count below zero, and this function fails closed on
            nonsense inputs rather than comparing them anyway.
    """
    if intent_count < 0:
        raise ValueError(f"intent_count must be >= 0, got {intent_count!r}")
    if max_lots <= 0:
        raise ValueError(f"max_lots must be > 0, got {max_lots!r}")
    ceiling = max_lots * INTENT_COUNT_BUG_CEILING_MULTIPLE
    if intent_count > ceiling:
        raise ImplausibleIntentCountError(
            f"intent_count={intent_count} exceeds the bug-catcher ceiling "
            f"{ceiling} (max_lots={max_lots} x "
            f"{INTENT_COUNT_BUG_CEILING_MULTIPLE}) -- this is not a risk cap "
            "to raise, it means something upstream produced an implausible "
            "number of order intents; stop and investigate rather than "
            "continuing"
        )
