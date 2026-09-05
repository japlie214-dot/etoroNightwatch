"""The twelve preflight gates (DESIGN §5, DESIGN_market_data.md §6).

Only **gate 10's freshness check** is implemented here (build step 5 of
DESIGN_market_data.md §12, described there as "the highest-value item in
this entire sequence" -- it is the check that would have caught §2.9's live
UPRO rates-freeze bug outright). Gate 9 and the other ten gates remain
comment-only stubs; do not infer they are implemented because this file grew.

# TODO(step 6, DESIGN §5): implement each remaining gate as a function
# returning a db.repo.PreflightCheck, run in order, ALL must pass:
#   1. Halt switch          - host.halt_flag_path() exists, or /halt was set
#   2. Power & permissions   - battery >=40% or charging; battery-optimization
#      exemption granted; AlarmManager.canScheduleExactAlarms(); wake lock
#      acquired (host.power_status())
#   3. Network               - DNS/HTTPS to public-api.etoro.com within 90s
#   4. Clock                 - local vs API Date header, <= 60s skew
#   5. Database              - opens; schema_version matches; no unfinished
#      prior session (db.repo.Repo.unfinished_sessions())
#   6. Auth                  - GET /balances == 200
#   7. Environment match     - config says real/demo and the key is bound the
#      same way, PROBED not trusted
#   8. Instruments           - UPRO/SCHG/SPY resolve; allowOpenPosition;
#      minPositionExposure <= order_size
#   9. Calendar (DESIGN_market_data.md §6 gate 9) - exchange_calendars XNYS
#      via `.sessions` membership, NOT `.is_session()` (raises
#      DateOutOfBounds under pandas 3.0.5, DESIGN_market_data.md §2.6).
#      market/calendar.py stays a cross-check, not the authority.
#  11. Reconciliation        - broker positions vs sidecar agree (DESIGN_
#      market_data.md §4.5's inverted gate 11; delegates to
#      engine/reconcile.py, not yet built)
#  12. Risk caps             - session order count/notional vs config.risk
#
# Any failure: persist ALL attempted PreflightCheck rows via
# db.repo.Repo.record_preflight_check, send the Telegram abort message naming
# the gate, and -- per DESIGN §5 -- hold the wake lock rather than returning
# to Doze, so run_preflight()'s caller must not release on failure. That
# orchestration function (run_preflight) does not exist yet; gate 10 below is
# a pure function over already-fetched data for exactly that reason -- it has
# no opinion on wake locks, sessions, or how its inputs were obtained.

Gate 10, per DESIGN_market_data.md §6, has five numbered sub-checks. Scope
of this module:

    1. Freshness of the provisional close                    -- IMPLEMENTED
       (whichever source supplies it -- Tiingo IEX primary or eToro candles
       fallback -- plus the free eToro-internal rates-vs-candles cross-check,
       for every managed symbol including SPY, per §5's "free internal
       cross-check" and §6 item 1's explicit SPY carve-in). As of 2026-09-05,
       this also validates the candle's *own* freshness (`Candle.timestamp`,
       populated by `EtoroClient._parse_candle`) before trusting it as the
       reference for either provisional-close path -- previously the candle
       was the trusted reference by construction, with no check on its own
       age; see `_check_candle_freshness` and `_check_provisional_close`.
    2. Window completeness (>=265 sessions, date-set matches the calendar)
       -- TODO(step 3-4, DESIGN_market_data.md §6 item 2, §3, market/bars.py
       missing_trading_days()). Depends on the fetch-derive-discard window
       assembly, not yet built.
    3. Sanity: live rate present and non-zero for every managed symbol
       -- IMPLEMENTED (trivial and adjacent to item 1; both read the same
       `Rate` objects).
    4. Corporate actions: no unprocessed splitFactor!=1.0 or unexpected
       divCash since the last session -- TODO(step 3, DESIGN_market_data.md
       §6 item 4, §7 `corporate_actions` table). Depends on that table and
       the prior-session comparison it needs, neither built yet.
    5. Signal sanity: |cone_5_z| > 10 aborts -- TODO(DESIGN_market_data.md §6
       item 5, retained from DESIGN.md §6.G). Depends on the cone signal
       itself being computed, which depends on item 2's window, so it is
       blocked by the same prerequisite.

Items 2, 4 and 5 are out of scope here because each depends on
build-sequencing steps 3-4 (drop `bars`, fetch-derive-discard window
assembly) which this task explicitly does not build.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping, Sequence

from ..config import MarketDataConfig
from ..etoro.models import Candle, Rate
from ..notify.telegram import TelegramNotifier

__all__ = [
    "GateResult",
    "ProvisionalClose",
    "INTRADAY_CANDLE_INTERVALS",
    "gate_10_price_data",
    "notify_gate_10_failure",
]

_logger = logging.getLogger("nightwatch.engine.preflight")

#: The eToro ``history/candles`` interval values that are intraday, i.e. that
#: ``MarketDataConfig.max_candle_age_minutes_intraday`` is an honest bound
#: for (DESIGN_market_data.md §2.11.B lists exactly these as the granularities
#: eToro's candles endpoint serves, all confirmed live and current). ``OneDay``
#: is deliberately excluded -- it is handled as its own case in
#: :func:`_check_candle_freshness`, not folded into this set, because a flat
#: minutes-based bound is not a meaningful freshness signal for it (added
#: 2026-09-05).
INTRADAY_CANDLE_INTERVALS: frozenset[str] = frozenset(
    {
        "OneMinute",
        "FiveMinutes",
        "TenMinutes",
        "FifteenMinutes",
        "ThirtyMinutes",
        "OneHour",
        "FourHours",
    }
)

#: eToro's rate timestamp has been observed with 7 fractional-second digits
#: (``2026-09-03T19:59:54.8409896Z``, DESIGN_market_data.md §2.9) -- one more
#: digit than Python's ``datetime.fromisoformat`` tolerates. This trims any
#: run of more than 6 fractional digits down to 6 (microseconds) before
#: parsing; the discarded sub-microsecond precision is irrelevant to an
#: hours-scale freshness check.
_FRACTION_RE = re.compile(r"(\.\d{6})\d+")


@dataclass(frozen=True, slots=True)
class GateResult:
    """One gate's verdict -- the minimal scaffold every gate returns.

    Deliberately smaller than :class:`nightwatch.db.repo.PreflightCheck`
    (which needs a ``session_id`` this module has no reason to know about):
    the session orchestrator, once built, turns one of these into a
    ``PreflightCheck`` row per gate per session. ``details`` is the
    structured payload an alert needs (e.g. which symbol was stale and by
    how much) kept separate from ``reason`` so a human-readable summary and
    a machine-readable payload never have to be parsed out of each other.
    """

    gate: int
    name: str
    passed: bool
    reason: str
    details: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProvisionalClose:
    """The one number the session actually used as today's provisional
    close for ``symbol`` (DESIGN_market_data.md §3.1), plus enough
    provenance for gate 10 to validate it.

    ``source`` must be ``"tiingo_iex"`` (primary) or ``"etoro_candle"``
    (fallback) -- **never** ``"etoro_rate"``. §2.9 proved the rates endpoint
    serves a stale value with no price-based signature distinguishing it
    from a real one, and §3.1 is explicit the provisional close is never
    read from it. Constructing one with any other source string is refused
    outright, not merely undocumented, because that mistake is exactly the
    live bug this gate exists to catch.
    """

    symbol: str
    source: str
    value: float

    def __post_init__(self) -> None:
        if self.source not in ("tiingo_iex", "etoro_candle"):
            raise ValueError(
                f"ProvisionalClose.source must be 'tiingo_iex' or "
                f"'etoro_candle', got {self.source!r} -- the eToro rates "
                f"endpoint must never supply the provisional close "
                f"(DESIGN_market_data.md §2.9, §3.1)"
            )


def _rate_price(rate: Rate) -> float | None:
    """The single number DESIGN_market_data.md §6 calls "today's live rate".

    Prefers ``last_execution``; falls back to the bid/ask midpoint, since
    eToro's rates endpoint carries no market-open flag and can plausibly
    omit ``lastExecution`` outside trading hours (DESIGN §1.1). Deliberately
    reimplemented here rather than imported from ``market/bars.py``'s
    identical ``_rate_price``: that module (the `bars` table, provisional/
    settle mechanism) is slated for deletion at DESIGN_market_data.md §3.2 /
    §12 build step 3, and gate 10 must not depend on code already marked for
    removal.
    """
    if rate.last_execution is not None:
        return rate.last_execution
    if rate.bid and rate.ask:
        return (rate.bid + rate.ask) / 2.0
    return None


def _parse_iso(timestamp: str | None) -> datetime | None:
    """Best-effort ISO-8601 parse, tolerant of a trailing ``Z`` and of more
    than 6 fractional-second digits (both observed live, DESIGN_market_data.md
    §2.9). Returns ``None`` (never raises) on anything unparseable -- an
    unparseable timestamp is treated as "age unknown", not as an error that
    could crash a preflight gate that exists to keep the app safe.
    """
    if not timestamp:
        return None
    text = timestamp.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    text = _FRACTION_RE.sub(r"\1", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _check_rate_sanity(
    symbols: Sequence[str], rates: Mapping[str, Rate]
) -> list[str]:
    """Gate 10 item 3: live rate present and non-zero for every managed
    symbol (DESIGN_market_data.md §6). Returns a list of failure messages,
    empty when everything passes."""
    failures: list[str] = []
    for symbol in symbols:
        rate = rates.get(symbol)
        if rate is None:
            failures.append(f"{symbol}: no live rate returned")
            continue
        price = _rate_price(rate)
        if price is None or not math.isfinite(price) or price <= 0:
            failures.append(
                f"{symbol}: live rate present but not a usable positive price "
                f"(got {price!r})"
            )
    return failures


def _check_rates_vs_candles(
    symbols: Sequence[str],
    rates: Mapping[str, Rate],
    candles: Mapping[str, Candle],
    *,
    now: datetime,
    max_age_minutes: float,
    max_deviation_pct: float,
) -> tuple[list[str], list[str]]:
    """The free internal cross-check (DESIGN_market_data.md §5, §6 gate 10
    item 1): eToro's own rates path against eToro's own candles path, for
    every managed symbol independently -- including SPY, since the stale
    symbol rotates (§2.9: SCHG stale with the market closed on the earlier
    probe, UPRO stale with the market **open** on 2026-09-04) and SPY drives
    ``cone_5_z`` even though it is never bought.

    Two independent tells, either one sufficient to fail a symbol:

    * **Age.** ``Rate.timestamp`` (already parsed by
      :meth:`EtoroClient.get_rates`, currently dropped downstream) older
      than ``max_age_minutes``. This is exactly what would have caught
      §2.9's UPRO evidence directly: ``date=2026-09-03T19:59:54.8409896Z``
      against a probe taken with the market open the next day is ~19.0
      hours old.
    * **Value.** The rate's price deviates from the candle's close by more
      than ``max_deviation_pct``. This is the fallback for the case a
      timestamp is missing or unparseable, and it independently confirms
      the age-based finding on the UPRO fixture (153.67 vs a concurrent,
      current candle close of ~151, a +1.3%-scale gap -- DESIGN_market_data
      .md §2.9's quantified error).

    Returns ``(failures, stale_symbols)``.
    """
    failures: list[str] = []
    stale_symbols: list[str] = []
    for symbol in symbols:
        rate = rates.get(symbol)
        candle = candles.get(symbol)
        if rate is None or candle is None:
            # Absence is handled by _check_rate_sanity / the provisional-close
            # check; this cross-check needs both sides present to compare.
            continue

        age_minutes: float | None = None
        parsed = _parse_iso(rate.timestamp)
        if parsed is not None:
            age_minutes = (now - parsed).total_seconds() / 60.0

        if age_minutes is not None and age_minutes > max_age_minutes:
            failures.append(
                f"{symbol}: eToro rate is {age_minutes / 60.0:.1f}h old "
                f"(timestamp={rate.timestamp}), exceeding the "
                f"{max_age_minutes:.0f}-minute freshness bound -- eToro "
                f"candles show a current price"
            )
            stale_symbols.append(symbol)
            continue

        rate_price = _rate_price(rate)
        if rate_price is not None and candle.close:
            deviation = abs(rate_price - candle.close) / candle.close
            if deviation > max_deviation_pct:
                failures.append(
                    f"{symbol}: eToro rate ({rate_price}) disagrees with "
                    f"eToro's own candle close ({candle.close}) by "
                    f"{deviation * 100:.2f}%, exceeding the "
                    f"{max_deviation_pct * 100:.2f}% tolerance"
                )
                stale_symbols.append(symbol)

    return failures, stale_symbols


def _check_candle_freshness(
    symbol: str,
    candle: Candle | None,
    *,
    now: datetime,
    max_age_minutes_intraday: float,
) -> str | None:
    """Establish the candle's *own* age -- the hole closed 2026-09-05.

    Before this, the candle was treated as the trusted reference by
    construction: ``_check_provisional_close`` cross-checked values against
    ``candle.close`` but never asked how old the candle itself was. That
    matters twice over (DESIGN_market_data.md §5.1, §10.2):

    * On the ``etoro_candle`` fallback path, the candle *is* the provisional
      close, so a stale candle sails through ungated.
    * On the ``tiingo_iex`` primary path, Tiingo's free-tier IEX response has
      **no independent freshness signal at all** (§10.2: ``timestamp`` is
      response-generation time; ``lastSaleTimestamp``/``quoteTimestamp`` are
      both ``null``) -- agreement with a stale candle is two stale numbers
      agreeing, not a freshness check.

    Extended 2026-09-05 to branch on ``candle.interval`` (added to
    :class:`~nightwatch.etoro.models.Candle` the same day) rather than
    applying one flat bound to every bar:

    * **``OneDay``** -- timestamp age is not a meaningful freshness signal
      here (DESIGN_market_data.md §2.11: eToro's daily bar's *close* tracks
      the live price intraday while its date field may carry no intraday
      time component at all, so an hours-old-looking timestamp can be
      functionally current). This function does **not** apply an age bound
      for ``OneDay`` and returns ``None`` unconditionally -- freshness for
      this case is left entirely to :func:`_check_provisional_close`'s
      value-agreement check against ``candle.close``. **This is a known,
      weaker guarantee, stated plainly rather than hidden:** when the
      provisional close itself came from the same ``OneDay`` candle
      (``source == "etoro_candle"``, the Tiingo-unreachable fallback), there
      is no independent value left to compare either, and a stale ``OneDay``
      candle sails through both checks ungated. That residual gap is
      accepted here rather than papered over with a numeric bound that would
      be honest for no real bar shape.
    * **A recognised intraday interval** (:data:`INTRADAY_CANDLE_INTERVALS`)
      -- parse ``Candle.timestamp`` with :func:`_parse_iso`, compare to
      ``now`` against ``max_age_minutes_intraday``, exactly as before.
    * **Missing or unrecognised** (``None``, a typo, a future interval this
      module does not yet know about) -- fails closed. There is no bound to
      honestly apply when the gate cannot tell what kind of bar it was
      handed, and guessing (e.g. silently defaulting to the intraday bound)
      would reintroduce the exact "one flat bound for every interval"
      mistake this change exists to close.

    Independent of interval, this still fails closed (returns a message,
    never ``None``) when the candle is missing entirely, or -- for an
    intraday interval -- its timestamp is missing/unparseable
    (DESIGN_market_data.md §3.3: no data means no trade, and an unusable
    freshness signal is not a fresh one).
    """
    if candle is None:
        return f"{symbol}: no eToro candle available to establish freshness"
    interval = candle.interval
    if interval == "OneDay":
        # No honest minutes-based bound exists for a daily bar (see
        # docstring) -- freshness is left to the value-agreement check.
        return None
    if interval not in INTRADAY_CANDLE_INTERVALS:
        return (
            f"{symbol}: eToro candle interval {interval!r} is missing or "
            f"unrecognised -- cannot select a freshness bound, failing closed"
        )
    parsed = _parse_iso(candle.timestamp)
    if parsed is None:
        return (
            f"{symbol}: eToro candle timestamp missing or unparseable "
            f"(timestamp={candle.timestamp!r}) -- cannot establish freshness, "
            f"failing closed"
        )
    age_minutes = (now - parsed).total_seconds() / 60.0
    if age_minutes > max_age_minutes_intraday:
        return (
            f"{symbol}: eToro candle is {age_minutes:.1f} minutes old "
            f"(timestamp={candle.timestamp}), exceeding the "
            f"{max_age_minutes_intraday:.0f}-minute candle freshness bound"
        )
    return None


def _check_provisional_close(
    provisional_closes: Mapping[str, ProvisionalClose],
    candles: Mapping[str, Candle],
    *,
    now: datetime,
    max_deviation_pct: float,
    max_candle_age_minutes_intraday: float,
) -> list[str]:
    """Gate 10 item 1's core clause: validate whichever source actually
    supplied the provisional close (DESIGN_market_data.md §3.1), cross-
    checked against eToro's own candle when one is available -- and, as of
    2026-09-05, against the candle's *own* freshness first.

    Both branches now gate on :func:`_check_candle_freshness`:

    * ``source == "etoro_candle"`` (fallback): the candle IS the provisional
      close, so there is nothing independent left to check its *value*
      against -- only its freshness, which is checked here. This is a real,
      known residual limitation, not silently passed over: a fresh-but-wrong
      candle would still clear this gate, because single-sourced means
      single-sourced (§5.1's "no second opinion" case is not fully closed by
      this change, just no longer ungated on the freshness axis).
    * ``source == "tiingo_iex"`` (primary): the candle is the *only*
      freshness signal available anywhere in this check, because Tiingo IEX
      carries none of its own (§10.2). A stale candle now fails this symbol
      even if the Tiingo value agrees with it -- agreement with a stale
      number is not freshness.
    """
    failures: list[str] = []
    for symbol, provisional in provisional_closes.items():
        candle = candles.get(symbol)
        freshness_failure = _check_candle_freshness(
            symbol,
            candle,
            now=now,
            max_age_minutes_intraday=max_candle_age_minutes_intraday,
        )
        if freshness_failure is not None:
            failures.append(freshness_failure)
            continue
        assert candle is not None  # freshness check above already excluded None

        if provisional.source == "etoro_candle":
            # Single-sourced: freshness is the only guard available (see
            # docstring above) -- no independent value to cross-check.
            continue
        if not candle.close:
            continue
        deviation = abs(provisional.value - candle.close) / candle.close
        if deviation > max_deviation_pct:
            failures.append(
                f"{symbol}: provisional close from {provisional.source} "
                f"({provisional.value}) disagrees with eToro's candle close "
                f"({candle.close}) by {deviation * 100:.2f}%, exceeding the "
                f"{max_deviation_pct * 100:.2f}% tolerance"
            )
    return failures


def gate_10_price_data(
    *,
    now: datetime,
    managed_symbols: Sequence[str],
    provisional_closes: Mapping[str, ProvisionalClose],
    rates: Mapping[str, Rate],
    candles: Mapping[str, Candle],
    config: MarketDataConfig,
) -> GateResult:
    """Gate 10, items 1 and 3 only (DESIGN_market_data.md §6) -- items 2, 4
    and 5 are out of scope; see the module docstring.

    This is a pure function over already-fetched data, not a fetcher: the
    session orchestration that decides *how* to obtain ``rates``/``candles``/
    ``provisional_closes`` (which client to call, in what order, whether to
    fall back) is DESIGN_market_data.md build steps 3-4 and is not built by
    this task. Keeping the boundary here matters twice over: it keeps this
    module testable without a live eToro connection (the dev PC cannot reach
    eToro at all -- DESIGN_market_data.md §2.8), and it keeps this gate
    honest about not owning fetch/retry/fallback policy that belongs
    upstream.

    On failure: the caller must halt, hold the wake lock, and alert over
    Telegram (DESIGN §5; see :func:`notify_gate_10_failure`) -- an ``ERROR``
    log alone is insufficient, per DESIGN_market_data.md §6's explicit
    "this phone lives away from the user."
    """
    failures: list[str] = []
    details: dict[str, object] = {
        "sanity_failures": [],
        "rate_vs_candle_failures": [],
        "provisional_close_failures": [],
        "stale_symbols": [],
    }

    sanity_failures = _check_rate_sanity(managed_symbols, rates)
    if sanity_failures:
        failures.extend(sanity_failures)
        details["sanity_failures"] = sanity_failures

    rate_candle_failures, stale_symbols = _check_rates_vs_candles(
        managed_symbols,
        rates,
        candles,
        now=now,
        max_age_minutes=config.max_rate_age_minutes,
        max_deviation_pct=config.rate_vs_candle_deviation_pct,
    )
    if rate_candle_failures:
        failures.extend(rate_candle_failures)
        details["rate_vs_candle_failures"] = rate_candle_failures
        details["stale_symbols"] = stale_symbols

    provisional_failures = _check_provisional_close(
        provisional_closes,
        candles,
        now=now,
        max_deviation_pct=config.provisional_close_deviation_pct,
        max_candle_age_minutes_intraday=config.max_candle_age_minutes_intraday,
    )
    if provisional_failures:
        failures.extend(provisional_failures)
        details["provisional_close_failures"] = provisional_failures

    passed = not failures
    reason = (
        "provisional close and live rates fresh and mutually consistent "
        f"for all managed symbols ({', '.join(managed_symbols)})"
        if passed
        else "; ".join(failures)
    )
    return GateResult(gate=10, name="Price data", passed=passed, reason=reason, details=details)


def notify_gate_10_failure(
    notifier: TelegramNotifier,
    result: GateResult,
    *,
    trading_date: str,
    session_id: int | None = None,
) -> None:
    """Send the Telegram alert for a failed gate 10 (DESIGN_market_data.md
    §6: "halt, hold the wake lock, alert over Telegram... naming the stale
    symbol and its age").

    This wraps the existing, already-built notify layer
    (:class:`nightwatch.notify.telegram.TelegramNotifier`) -- it is not a
    stub. ``result.reason`` already names the stale symbol(s) and their age
    (see :func:`_check_rates_vs_candles`); ``result.details`` carries the
    same information structured for any future dashboard/log consumer.

    **What this function deliberately does NOT do:** hold the wake lock,
    or decide the session should actually halt. Those are the calling
    session orchestrator's responsibility (``run_preflight()``, not yet
    built -- DESIGN §5's "the caller must not release [the wake lock] on
    failure"). Raising this function's scope to cover that orchestration
    would mean inventing gates 1-9/11/12 and the session loop just to give
    gate 10 somewhere to be called from, which is explicitly out of scope.
    """
    if result.passed:
        raise ValueError("notify_gate_10_failure called with a passing GateResult")
    notifier.preflight_result(
        trading_date=trading_date,
        passed=False,
        gate=result.gate,
        gate_name=result.name,
        reason=result.reason,
        session_id=session_id,
    )
