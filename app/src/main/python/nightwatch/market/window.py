"""The fetch-derive-discard window assembly (DESIGN_market_data.md §3, §12
build steps 3-4). This is the module the whole design addendum exists to
land: §0's decision in one line -- "Stop accumulating price history on the
phone. Fetch it fresh every session, use it, discard it" -- has nowhere to
live until this module exists, and WARMUP_BARS is 0 in live
(``config.StrategyConfig.warmup_bars``) specifically because this is
expected to supply >=265 SPY bars every session; without it the cone signal
returns ``None`` forever (``strategy.cone``).

**What this module is not.** It does not talk to eToro at all (candles/rates
come from ``etoro.client``, already built, and are passed in already
fetched -- mirroring the boundary ``engine/preflight.py`` already draws for
exactly the same "dev PC cannot reach eToro" reason, DESIGN_market_data.md
§2.8). It does not implement preflight gate 10 item 2 (window completeness)
as a *gate* -- DESIGN_market_data.md §12's build-sequencing table assigns
gates 9/10 to step 5, which depends on this module landing first, not the
other way round. :func:`validate_window_completeness` is the interface step
5 is expected to call; it is exposed here, fully working, and unit-tested,
but nothing in this module turns its result into a halt+alert -- that
orchestration decision belongs to the session engine (``engine/session.py``,
also not yet built) and to gate 10 itself.

**Fetch, derive, discard, stated in code, not just in prose:**

* **[fetch]** :func:`fetch_symbol_window` -- exactly one
  ``TiingoClient.get_eod_history`` call per symbol. One fetch is one
  dividend/split-adjustment epoch (§2.2, §10's table: "impossible -- one
  fetch is one epoch"); this module never issues a second history request
  for the same symbol within one assembly, and never merges two fetches'
  ``adjClose`` values together, which is exactly the mechanism that would
  reintroduce the epoch-contamination bug this design exists to close.
* **[derive]** :func:`assemble_spy_signal` turns the fetched window into
  ``cone_5_z`` (``strategy.cone``) and the SPY ATH/close/drawdown/tier
  context (the same derivation ``market.adopt`` already uses for adopted
  positions, reused here rather than re-implemented, per §3.1: "the same
  field the live session's own signal derivation reads").
* **[splice]** today's provisional close (:func:`resolve_provisional_close`)
  -- Tiingo IEX primary, eToro candle fallback, **never** the eToro rates
  endpoint (§2.9, §3.1; enforced structurally: this module has no code path
  that reads a ``Rate`` at all) -- appended as the series' final element
  before the signal is computed.
* **[discard]** :func:`assemble_spy_signal` holds ``history`` (the raw
  ``EodBar`` list) in a local variable for the duration of one call and
  never attaches it to the object it returns, never passes it to any
  persistence layer, and never caches it across calls. There is no
  module-level state here at all -- nothing to accumulate into, by
  construction, mirroring ``market.source.TiingoClient``'s own "no on-disk
  cache" contract. The only thing about the window that survives the call is
  its :class:`WindowFingerprint` (§3.4) -- a few dozen bytes, not a price
  series -- which a caller may persist via
  ``db.repo.Repo`` (``price_window_fingerprints``, migration 004) to recover
  revision detection without storing prices.

**Untested against a live payload.** Every code path here is exercised in
this task only against hand-built ``EodBar``/``IexQuote``/``Candle`` fixtures
-- the dev PC cannot reach eToro at all (DESIGN_market_data.md §2.8) and this
task's environment cannot reach Tiingo either. ``market/source.py``'s own
field-name mappings were separately confirmed live 2026-09-05 (its
docstring); this module trusts those typed objects and adds no new HTTP
parsing of its own.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Final, Sequence

import exchange_calendars as xcals
import pandas as pd

from ..engine.preflight import ProvisionalClose
from ..etoro.models import Candle
from ..strategy.cone import REQUIRED_HISTORY_BARS, cone_5_z
from ..strategy.params import tier_for_drawdown
from .adopt import SpyContext
from .source import EodBar, IexQuote, TiingoClient

__all__ = [
    "WindowAssemblyError",
    "WindowCompletenessResult",
    "WindowFingerprint",
    "AssembledSignal",
    "DEFAULT_LOOKBACK_CALENDAR_DAYS",
    "DEFAULT_CALENDAR_NAME",
    "expected_sessions",
    "fetch_symbol_window",
    "validate_window_completeness",
    "compute_fingerprint",
    "resolve_provisional_close",
    "assemble_spy_signal",
]

#: Calendar days of history to request per fetch. 400 calendar days
#: comfortably covers >=265 NYSE trading sessions (about 261 trading days
#: occur in a normal 365-calendar-day span; 400 leaves headroom for holiday
#: weeks and a slow start) in exactly one Tiingo call -- the same figure
#: ``market/bars.py``'s now-superseded ``seed(count=400)`` used, kept here
#: for continuity, not because it is independently derived.
DEFAULT_LOOKBACK_CALENDAR_DAYS: Final[int] = 400

#: The exchange this design validates against throughout (DESIGN_market_data
#: .md §2.6, §6 gate 9): NYSE.
DEFAULT_CALENDAR_NAME: Final[str] = "XNYS"


class WindowAssemblyError(Exception):
    """A fetch-derive-discard step could not produce a trustworthy result.

    Raised rather than returning a fabricated/partial value in every case
    below -- DESIGN_market_data.md §3.3: "no data at T-30 means no trade,"
    and this project's repeated "a silent zero is worse than a crash" rule
    (``get_balance``'s docstring). Callers must treat this as an abort.
    """


@dataclass(frozen=True, slots=True)
class WindowCompletenessResult:
    """Gate 10 item 2's verdict (DESIGN_market_data.md §6): >=265 sessions,
    date set matching ``exchange_calendars``' expected sessions for the
    range spanned by the fetched window itself -- **not** against "today",
    which is exactly what would otherwise call today a holiday before its
    bar exists (§6 item 2's explicit warning).

    This is the interface step 5's gate 10 item 2 is expected to call; it
    is not itself a gate (see module docstring) -- it only reports.
    """

    symbol: str
    passed: bool
    bar_count: int
    window_start: str
    window_end: str
    missing_sessions: tuple[str, ...]
    unexpected_dates: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class WindowFingerprint:
    """One row of ``price_window_fingerprints`` (DESIGN_market_data.md §3.4,
    §7, migration 004) -- NOT a price series. Recovers revision detection
    (a provider silently rewriting history becomes undetectable once prices
    are never stored, §3.4) at the cost of a few dozen bytes per symbol per
    session, never the series itself.
    """

    trading_date: str
    symbol: str
    window_start: str
    window_end: str
    bar_count: int
    series_sha256: str
    source: str
    source_fetched_at: str


@dataclass(frozen=True, slots=True)
class AssembledSignal:
    """Everything the strategy needs out of one session's SPY window
    (DESIGN_market_data.md §3.1's [derive] step), and nothing else --
    deliberately no field carries the raw ``EodBar`` list itself (see module
    docstring's [discard] paragraph).
    """

    trading_date: str
    provisional_close: ProvisionalClose
    completeness: WindowCompletenessResult
    fingerprint: WindowFingerprint
    spy_context: SpyContext
    cone_5_z: float | None


@lru_cache(maxsize=4)
def _calendar(calendar_name: str):
    """Cached calendar construction (DESIGN_market_data.md §2.6).

    Returns the ``exchange_calendars`` calendar object. Callers must only
    ever use its ``.sessions``/``.adhoc_holidays`` attributes -- **never**
    ``.is_session()``, which §2.6 (verified live, both 4.11.1 and 4.13.2)
    documents as raising ``DateOutOfBounds`` under pandas 3.0.5 for
    ordinary trading days, not only out-of-range ones. This module calls
    neither ``.is_session()`` nor any other calendar method anywhere below --
    only ``.sessions`` set/vector membership -- so it behaves identically on
    the desktop venv (exchange_calendars 4.13.2, pandas 3.0.5) and on
    Chaquopy (4.11.1, pandas 2.1.3); both expose ``.sessions`` with the same
    semantics (§2.6: "3,186 sessions in both, zero differing dates").
    """
    return xcals.get_calendar(calendar_name)


def expected_sessions(
    start_date: str, end_date: str, *, calendar_name: str = DEFAULT_CALENDAR_NAME
) -> tuple[str, ...]:
    """Every expected trading session in ``[start_date, end_date]``
    (inclusive, both ``YYYY-MM-DD``), per ``exchange_calendars``.

    Uses ``.sessions`` (a ``pandas.DatetimeIndex``) filtered by vectorised
    comparison -- membership, not ``.is_session()`` (see :func:`_calendar`).
    """
    calendar = _calendar(calendar_name)
    sessions = calendar.sessions
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    in_range = sessions[(sessions >= start) & (sessions <= end)]
    return tuple(in_range.strftime("%Y-%m-%d"))


def fetch_symbol_window(
    tiingo: TiingoClient,
    symbol: str,
    *,
    lookback_calendar_days: int = DEFAULT_LOOKBACK_CALENDAR_DAYS,
    today: _date | None = None,
) -> list[EodBar]:
    """[fetch]: one Tiingo EOD history call for ``symbol`` (DESIGN_market_data
    .md §3.1, §10: "one fetch is one epoch").

    Args:
        today: Injectable clock for testability -- defaults to the real
            ``date.today()``. Any bar Tiingo returns dated ``today`` is
            dropped defensively: DESIGN_market_data.md §6 item 2 is explicit
            that today's bar "does not exist until after the close," and
            this fetch must never treat a same-day partial print (were
            Tiingo ever to return one) as a settled history bar.

    Returns:
        Bars in ascending date order (Tiingo's own ordering is not assumed;
        ``market/source.py`` documents that it returns "the order Tiingo
        supplies them," not guaranteed sorted).

    Raises:
        WindowAssemblyError: Tiingo returned no bars at all -- an empty
            window must never read downstream as "still in warmup," it must
            abort loudly (mirrors ``market/bars.py``'s superseded
            ``seed()``, which raised for the same reason).
    """
    reference_today = today if today is not None else _date.today()
    start_date = (reference_today - timedelta(days=lookback_calendar_days)).isoformat()
    bars = tiingo.get_eod_history(symbol, start_date=start_date)
    if not bars:
        raise WindowAssemblyError(
            f"no EOD history returned for {symbol} (start_date={start_date}) "
            f"-- refusing to proceed with an empty window (DESIGN_market_data"
            f".md §3.3: no data means no trade)"
        )
    today_iso = reference_today.isoformat()
    settled = [bar for bar in bars if bar.date != today_iso]
    if not settled:
        raise WindowAssemblyError(
            f"every bar returned for {symbol} was dated today ({today_iso}) "
            f"-- no settled history at all"
        )
    return sorted(settled, key=lambda bar: bar.date)


def validate_window_completeness(
    symbol: str,
    bars: Sequence[EodBar],
    *,
    min_sessions: int = REQUIRED_HISTORY_BARS,
    calendar_name: str = DEFAULT_CALENDAR_NAME,
) -> WindowCompletenessResult:
    """Gate 10 item 2 (DESIGN_market_data.md §6): >=265 sessions, and the
    fetched window's date set must match ``exchange_calendars``' expected
    sessions for **the range the window itself spans** -- comparing against
    the window's own last bar date, never against "today", is what avoids
    the "today looks like a missing holiday" trap §6 item 2 names explicitly.

    Promotes ``market/bars.py``'s ``missing_trading_days()`` from "dead
    diagnostic" (its own docstring) to this check, adapted to validate a
    freshly fetched window rather than a stored one, per §6's closing
    paragraph.

    Raises:
        WindowAssemblyError: ``bars`` is empty (nothing to validate -- a
            caller should not reach this with an empty window;
            :func:`fetch_symbol_window` already raises on that case) or
            contains a duplicate date (a corrupt/duplicated series -- never
            silently deduplicated).
    """
    if not bars:
        raise WindowAssemblyError(
            f"{symbol}: cannot validate completeness of an empty window"
        )
    dates = [bar.date for bar in bars]
    date_set = set(dates)
    if len(date_set) != len(dates):
        raise WindowAssemblyError(
            f"{symbol}: fetched window has duplicate dates -- refusing to "
            f"validate a corrupt series"
        )
    window_start, window_end = min(dates), max(dates)
    expected = expected_sessions(window_start, window_end, calendar_name=calendar_name)
    expected_set = set(expected)
    missing = tuple(sorted(expected_set - date_set))
    unexpected = tuple(sorted(date_set - expected_set))
    bar_count = len(bars)

    reasons: list[str] = []
    if bar_count < min_sessions:
        reasons.append(f"only {bar_count} session(s) fetched, need >= {min_sessions}")
    if missing:
        shown = ", ".join(missing[:5]) + (", ..." if len(missing) > 5 else "")
        reasons.append(f"{len(missing)} expected session(s) missing from the window: {shown}")
    if unexpected:
        shown = ", ".join(unexpected[:5]) + (", ..." if len(unexpected) > 5 else "")
        reasons.append(
            f"{len(unexpected)} date(s) in the window are not expected "
            f"{calendar_name} sessions: {shown}"
        )
    passed = not reasons
    reason = (
        f"{bar_count} sessions, {window_start}..{window_end}, matches "
        f"{calendar_name} exactly"
        if passed
        else "; ".join(reasons)
    )
    return WindowCompletenessResult(
        symbol=symbol,
        passed=passed,
        bar_count=bar_count,
        window_start=window_start,
        window_end=window_end,
        missing_sessions=missing,
        unexpected_dates=unexpected,
        reason=reason,
    )


def compute_fingerprint(
    symbol: str,
    bars: Sequence[EodBar],
    *,
    trading_date: str,
    source: str = "tiingo",
    fetched_at: str | None = None,
) -> WindowFingerprint:
    """DESIGN_market_data.md §3.4's cheap recovery: a SHA-256 over the
    ``(date, adjClose)`` series, never the series itself. A few dozen bytes,
    persisted via ``db.repo.Repo`` (``price_window_fingerprints``, migration
    004), restores revision detection without storing prices.
    """
    if not bars:
        raise WindowAssemblyError(f"{symbol}: cannot fingerprint an empty window")
    ordered = sorted(bars, key=lambda bar: bar.date)
    payload = "\n".join(f"{bar.date},{bar.adj_close!r}" for bar in ordered).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return WindowFingerprint(
        trading_date=trading_date,
        symbol=symbol,
        window_start=ordered[0].date,
        window_end=ordered[-1].date,
        bar_count=len(ordered),
        series_sha256=digest,
        source=source,
        source_fetched_at=fetched_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def resolve_provisional_close(
    symbol: str,
    *,
    iex_quote: IexQuote | None,
    etoro_daily_candle: Candle | None,
) -> ProvisionalClose:
    """[splice]: today's provisional close (DESIGN_market_data.md §3.1).

    Tiingo IEX ``tngoLast`` is primary; eToro's own daily candle close is
    the fallback used only when Tiingo IEX is unreachable. The eToro rates
    endpoint is not a parameter this function accepts at all -- §2.9 proved
    it serves a stale value with no price-based signature distinguishing it
    from a real one, and structurally excluding it here (rather than
    trusting every caller not to pass one) is the actual guarantee.
    :class:`~nightwatch.engine.preflight.ProvisionalClose` independently
    enforces the same rule in its own ``__post_init__``.

    Raises:
        WindowAssemblyError: neither source is available -- DESIGN_market_data
            .md §3.3: no data means no trade, never a fabricated close.
    """
    if iex_quote is not None:
        return ProvisionalClose(symbol=symbol, source="tiingo_iex", value=iex_quote.tngo_last)
    if etoro_daily_candle is not None and etoro_daily_candle.close:
        return ProvisionalClose(
            symbol=symbol, source="etoro_candle", value=etoro_daily_candle.close
        )
    raise WindowAssemblyError(
        f"{symbol}: no provisional close available from Tiingo IEX or eToro "
        f"candles -- refusing to trade on no data (DESIGN_market_data.md §3.3)"
    )


def _spy_context_with_provisional(
    history: Sequence[EodBar], provisional_close: float
) -> SpyContext:
    """SPY ATH/close/drawdown/tier as of *today*, folding in the spliced
    provisional close as the final observation (DESIGN_market_data.md §3.1:
    "adjClose series -> cone_5_z, ath, drawdown, tier").

    Deliberately not ``market.adopt.build_spy_context_index`` as-is: that
    index is keyed by historical trading date and never sees a provisional
    close (§4.3 forbids exactly that re-derivation for an *already-stamped*
    app-created position) -- a live session's "today" context needs the ATH
    running maximum to include today's own (possibly new-high) price, which
    ``history`` alone cannot supply since it stops at yesterday's close
    (§6 item 2: today's bar does not exist yet). Reuses the same
    :class:`~nightwatch.market.adopt.SpyContext` shape and
    ``tier_for_drawdown`` boundary function so a live-derived tier and an
    adopted-position's reconstructed tier are computed by the identical
    rule, per §3.1/§4.7's own stated intent.
    """
    running_ath = float("-inf")
    for bar in history:
        if not math.isfinite(bar.adj_close) or bar.adj_close <= 0:
            raise WindowAssemblyError(
                f"SPY history bar {bar.date} has a non-finite/non-positive "
                f"adjClose {bar.adj_close!r} -- refusing to compute an ATH "
                f"off a corrupt series"
            )
        running_ath = max(running_ath, bar.adj_close)
    if not math.isfinite(provisional_close) or provisional_close <= 0:
        raise WindowAssemblyError(
            f"provisional SPY close {provisional_close!r} is not finite/"
            f"positive -- refusing to compute today's drawdown off it"
        )
    running_ath = max(running_ath, provisional_close)
    drawdown = (provisional_close - running_ath) / running_ath
    return SpyContext(
        ath=running_ath,
        close=provisional_close,
        drawdown=drawdown,
        tier=tier_for_drawdown(drawdown),
    )


def assemble_spy_signal(
    tiingo: TiingoClient,
    *,
    trading_date: str,
    iex_quote: IexQuote | None,
    etoro_daily_candle: Candle | None,
    cone_horizon: int = 5,
    lookback_calendar_days: int = DEFAULT_LOOKBACK_CALENDAR_DAYS,
    min_sessions: int = REQUIRED_HISTORY_BARS,
    calendar_name: str = DEFAULT_CALENDAR_NAME,
    enforce_completeness: bool = True,
    today: _date | None = None,
) -> AssembledSignal:
    """DESIGN_market_data.md §3.1's full session sequence for SPY, the
    symbol ``cone_5_z`` is computed on: [fetch] -> [derive] + [splice] ->
    return, with the raw window discarded (see module docstring).

    Args:
        cone_horizon: ``config.StrategyConfig.cone_horizon`` -- passed
            explicitly rather than imported so this stays a pure function
            of its arguments, matching every other signal/guard module in
            this codebase.
        enforce_completeness: When ``True`` (the default), an incomplete
            window (:func:`validate_window_completeness` failing) raises
            immediately rather than computing a signal off a gappy/short
            series -- gate 10 item 2 does not exist yet to catch this
            downstream (DESIGN_market_data.md §12 step 5), and DESIGN's own
            "never fail silently" rule means this module must not proceed
            past a check it has already run and already knows failed.
            Pass ``False`` only for a caller (e.g. a future gate, or a
            diagnostic) that wants ``completeness`` reported without this
            function aborting on it -- ``cone_5_z``/``spy_context`` are
            still computed in that case, off whatever window was fetched.

    Raises:
        WindowAssemblyError: on any fetch, completeness (when enforced), or
            derivation failure -- see the individual functions this calls.
        ConeSignalError: from ``strategy.cone.cone_5_z`` if the spliced
            series is corrupt in a way that check alone would catch
            (propagated, not caught -- both are already "abort" exceptions
            in this codebase, not different flavors that need reconciling).
    """
    history = fetch_symbol_window(
        tiingo, "SPY", lookback_calendar_days=lookback_calendar_days, today=today
    )
    completeness = validate_window_completeness(
        "SPY", history, min_sessions=min_sessions, calendar_name=calendar_name
    )
    if enforce_completeness and not completeness.passed:
        raise WindowAssemblyError(
            f"SPY window failed completeness validation (gate 10 item 2 "
            f"interface, DESIGN_market_data.md §6): {completeness.reason}"
        )
    fingerprint = compute_fingerprint("SPY", history, trading_date=trading_date)
    provisional = resolve_provisional_close(
        "SPY", iex_quote=iex_quote, etoro_daily_candle=etoro_daily_candle
    )

    # [splice]: the most recent adjClose already equals its own raw close
    # (factor 1.0), so appending today's raw provisional close is
    # consistent at the seam (§8) -- except on an ex-dividend date, a known,
    # documented, ~0.33%-scale residual (§8) this function does not correct
    # for; see the module/task report for that explicit gap.
    closes = [bar.adj_close for bar in history] + [provisional.value]
    z = cone_5_z(closes, horizon=cone_horizon)
    spy_context = _spy_context_with_provisional(history, provisional.value)

    return AssembledSignal(
        trading_date=trading_date,
        provisional_close=provisional,
        completeness=completeness,
        fingerprint=fingerprint,
        spy_context=spy_context,
        cone_5_z=z,
    )
