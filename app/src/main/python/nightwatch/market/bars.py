"""Bar seeding, provisional-write, and settle (DESIGN §1.4, §4.2).

Every function here is a thin layer over :class:`~nightwatch.db.repo.Repo` and
:class:`~nightwatch.etoro.client.EtoroClient` — no SQL, and no HTTP parsing of
its own. The still-unverified live candle/rate response shape (see
:meth:`EtoroClient.get_candles`'s docstring) is handled entirely inside
``etoro.client``/``etoro.models``; this module only ever reads the typed
fields those already expose (``Candle.close``, ``Rate.last_execution``, ...),
so a field-name correction there is a one-place change, never a change here.

**The provisional/settle mechanism (DESIGN §4.2), stated plainly.** Today's
official close does not exist yet at T-30min, so :func:`write_provisional`
samples the live rate as a stand-in and marks the bar ``is_provisional=1``.
Because ``cone_5_z`` uses a 260-bar rolling window, that provisional value
would otherwise sit inside every future signal's window forever if it were
never corrected. :func:`settle_previous` is the other half: every session's
step [2] overwrites the *previous* provisional bar with the real official
candle and flips the flag to 0, before anything reads it as history. A bar
that is still provisional after settlement is a preflight gate 10 failure
(built in a later step) — this module only performs the settle, it does not
decide what a still-provisional bar means for the session.
"""

from __future__ import annotations

import logging
import math
from datetime import date as _date
from datetime import timedelta
from typing import Final

from ..db.repo import Bar, Repo
from ..etoro.client import EtoroClient
from ..etoro.errors import EtoroValidationError
from ..etoro.models import Rate
from . import calendar

__all__ = [
    "BadTickError",
    "MAX_TICK_DEVIATION_PCT",
    "LEVERAGED_TICK_DEVIATION_PCT",
    "LEVERAGED_SYMBOLS",
    "MIN_PLAUSIBLE_PRICE",
    "STALE_REFERENCE_TRADING_DAYS",
    "is_plausible_price",
    "max_tick_deviation_pct",
    "compounded_deviation_pct",
    "sessions_elapsed",
    "seed",
    "write_provisional",
    "settle_previous",
    "missing_trading_days",
]

_logger = logging.getLogger("nightwatch.market.bars")

#: DESIGN §6.G bad-tick guard: reject a live price further than this from the
#: previous *settled* close. The entry signal fires at ``cone_5_z < -2``, so a
#: single bad tick on the downside could manufacture a spurious buy off one
#: data point — DESIGN calls this "a real failure mode ... not a theoretical
#: one," not decoration. This default is for **unleveraged** instruments
#: (SPY, SCHG) — see :data:`LEVERAGED_TICK_DEVIATION_PCT` for why UPRO cannot
#: share it.
MAX_TICK_DEVIATION_PCT: Final[float] = 0.20

#: UPRO is a **3x-leveraged** ETF (DESIGN §6.A), so a genuine single-day move
#: routinely exceeds the unleveraged default above — this is not a
#: theoretical concern, it is the historical record: UPRO's worst real
#: session was **-35.0% on 2020-03-16** (the day SPY itself fell -12.0%) and
#: its best was **+28.0% on 2020-03-24**, both far past 20%. A flat 20%
#: threshold applied to UPRO would have raised a false "bad tick" abort on
#: exactly the highest-volatility days the cone signal is designed to react
#: to — the worst possible time to silently halt trading.
#:
#: Rather than pick a number that merely "feels safe," this scales the base
#: threshold by UPRO's own stated leverage (0.20 * 3 = 0.60): comfortable
#: headroom above both historical extremes (35%/28%) while still catching
#: the kind of gross fat-finger or decimal-point error the guard exists to
#: catch (a >60% single-day move has no precedent in UPRO's trading history).
LEVERAGED_TICK_DEVIATION_PCT: Final[float] = 0.60

#: Symbols the guard above applies to. Deliberately separate from DESIGN
#: §1.4's "managed set" ({UPRO, SCHG}) — this is about leverage, not trading
#: eligibility, and SPY (signal-only, never traded) would need the same
#: treatment if it were ever leveraged.
LEVERAGED_SYMBOLS: Final[frozenset[str]] = frozenset({"UPRO"})


def max_tick_deviation_pct(symbol: str) -> float:
    """The bad-tick guard's *per-session* deviation ceiling for ``symbol``
    (DESIGN §6.G).

    :data:`LEVERAGED_TICK_DEVIATION_PCT` for a 3x-leveraged instrument
    (:data:`LEVERAGED_SYMBOLS`), :data:`MAX_TICK_DEVIATION_PCT` otherwise.

    This is the *single-session* tolerance. When the reference close being
    compared against is more than one trading session old, use
    :func:`compounded_deviation_pct` instead — comparing a multi-week-old
    reference against this flat per-session number is exactly the defect
    this module used to have (DESIGN §6.G): a legitimate multi-week
    drawdown (SPY -30%/UPRO -75%, Feb-Mar 2020) would blow straight through
    a single-session band and get rejected as a "bad tick" at the worst
    possible moment — precisely when the strategy exists to act.
    """
    if symbol in LEVERAGED_SYMBOLS:
        return LEVERAGED_TICK_DEVIATION_PCT
    return MAX_TICK_DEVIATION_PCT


#: Compounding the per-session tolerance across N missed sessions without
#: any ceiling would let the "bad tick" guard's band grow without bound and
#: stop being a sanity check at all -- eventually *any* positive price would
#: pass. This is the ceiling on that growth, expressed as a multiple of the
#: symbol's own per-session tolerance: 10x. For SPY/SCHG (20% base) that is
#: a 200% band; for UPRO (60% base) a 600% band. Both are already reached
#: after roughly a week of compounding (``1.2**7 - 1 ~= 2.6``,
#: ``1.6**5 - 1 ~= 9.5``), so for any realistically long outage the ceiling
#: -- not the raw compounding formula -- is what is actually doing the
#: work. 200%/600% is chosen to sit comfortably above every real drawdown
#: either instrument class has ever produced even measured start-to-finish
#: across a multi-week crash (SPY's steepest ~monthly stretch in 2020 was
#: about -30%; UPRO's was about -75%, i.e. deviation 0.75 -- both an order
#: of magnitude under the ceiling) while still catching the kind of
#: fat-finger/decimal-point/unit error the guard exists for, which by
#: definition produces a jump with no market cause and typically several
#: hundred percent or more.
_DEVIATION_CEILING_MULTIPLE: Final[float] = 10.0

#: Trading-session gap beyond which a reference close is stale enough that
#: passing the (still-capped, see above) deviation check is not actually
#: reassuring -- it mostly means the band has grown wide enough to let
#: almost anything through. DESIGN's bad-data philosophy (§6.G, §5) is that
#: a failure must surface, never pass silently, so past this point
#: write_provisional/settle_previous still run the (capped) check -- a
#: gross fat-finger error must still be caught no matter how stale the
#: reference is -- but they also log loudly, because a human should verify
#: this symbol's history (and likely re-seed it) rather than trust a
#: multi-week-old comparison at face value. 10 trading sessions is about
#: two calendar weeks: long enough that an ordinary weekend/holiday gap
#: cannot explain it, and consistent with the real outages this project is
#: exposed to (a dead battery, a lost Private DNS setting, a
#: background-killed service on vivo).
STALE_REFERENCE_TRADING_DAYS: Final[int] = 10


def sessions_elapsed(reference_date: str, target_date: str) -> int:
    """Trading sessions strictly after ``reference_date`` up to and
    including ``target_date`` (both ``YYYY-MM-DD``), per
    :mod:`nightwatch.market.calendar`.

    This is "how many sessions of tolerance need to be compounded" — see
    :func:`compounded_deviation_pct`. Two adjacent trading days (the normal
    case: settle runs every session before the next write) give exactly 1,
    so the compounded tolerance collapses to the plain per-session pct in
    the common case. Always at least 1 (a same-day or out-of-order
    comparison is not a zero-session gap for tolerance purposes — there is
    still exactly one comparison being made).
    """
    start = _date.fromisoformat(reference_date)
    end = _date.fromisoformat(target_date)
    if end <= start:
        return 1
    sessions = 0
    current = start + timedelta(days=1)
    while current <= end:
        if calendar.is_trading_day(current):
            sessions += 1
        current += timedelta(days=1)
    return max(sessions, 1)


def compounded_deviation_pct(symbol: str, sessions: int) -> float:
    """DESIGN §6.G's per-session bad-tick tolerance, compounded across a
    reference gap of ``sessions`` trading sessions, and capped (see
    :data:`_DEVIATION_CEILING_MULTIPLE`).

    **Why not ``sessions * pct``.** The guard compares two *prices*, and a
    price is the result of *multiplying* by (1 +/- daily return) each
    session, not adding a flat percentage each session. A linear band would
    both under- and over-state the true tolerance: two sessions each near
    the +20% edge compound to +44% (``1.2**2 - 1``), not +40%, and by ten
    sessions the gap between linear (200%) and multiplicative (compounded,
    then capped at 200% here) becomes enormous in the other direction. The
    multiplicative form is what "N independent sessions, each at most
    ``pct``" actually implies, so that is what is used:
    ``(1 + pct) ** sessions - 1``, then capped at ``pct *
    _DEVIATION_CEILING_MULTIPLE`` so the band cannot grow without bound
    forever (see that constant's docstring for why 10x is a defensible
    stopping point).
    """
    base = max_tick_deviation_pct(symbol)
    if sessions <= 1:
        return base
    compounded = (1.0 + base) ** sessions - 1.0
    ceiling = base * _DEVIATION_CEILING_MULTIPLE
    return min(compounded, ceiling)

#: A price at or below this is nonsense regardless of history — rejected
#: before any comparison against a previous close. Also catches a `0.0`
#: silently read off an empty/stale rates payload (DESIGN §1.1: the rates
#: endpoint carries no market-open flag, so a closed-market zero must not be
#: mistaken for a real quote).
MIN_PLAUSIBLE_PRICE: Final[float] = 0.01


class BadTickError(ValueError):
    """A live price failed the bad-tick / bad-data guard (DESIGN §6.G).

    Callers (the session engine, built in a later step) must treat this as an
    abort — never as a silently-skipped bar write, and never caught and
    retried with the same tick.
    """


def is_plausible_price(
    price: float | None,
    previous_close: float | None,
    *,
    max_deviation_pct: float = MAX_TICK_DEVIATION_PCT,
) -> bool:
    """DESIGN §6.G's bad-tick guard, as a pure predicate.

    Kept free of any DB/HTTP dependency so it is trivially unit-testable
    against hand-built numbers, independent of :class:`Repo`/:class:`EtoroClient`.

    Rejects a missing, non-finite, non-positive, or below-
    :data:`MIN_PLAUSIBLE_PRICE` price outright. With a known previous
    *settled* close, additionally rejects a deviation larger than
    ``max_deviation_pct``. With no previous close at all (the very first bar
    this symbol has ever seen), only the absolute sanity check applies —
    there is nothing yet to deviate from.
    """
    if price is None or not math.isfinite(price) or price < MIN_PLAUSIBLE_PRICE:
        return False
    if previous_close is not None and previous_close > 0:
        deviation = abs(price - previous_close) / previous_close
        if deviation > max_deviation_pct:
            return False
    return True


def _rate_price(rate: Rate) -> float | None:
    """The single number DESIGN §4.2 calls "today's provisional close".

    Prefers ``lastExecution``. eToro's rates endpoint carries no market-open
    flag (DESIGN §1.1) and can plausibly omit ``lastExecution`` outside
    trading hours, so the bid/ask midpoint is the fallback — never a silent
    ``None`` treated as zero.
    """
    if rate.last_execution is not None:
        return rate.last_execution
    if rate.bid and rate.ask:
        return (rate.bid + rate.ask) / 2.0
    return None


def seed(client: EtoroClient, repo: Repo, symbol: str, *, count: int = 400) -> int:
    """Seed up to ``count`` historical daily bars for ``symbol`` (DESIGN §1.4).

    DESIGN §1.4: the cone signal needs >=265 settled SPY bars in the database
    or it returns ``None`` forever; seeding 400 keeps the 260-bar rolling
    window full from day one. Every seeded bar is written already settled
    (``is_provisional=False``, ``source="seed"``) — these are official closed
    daily candles from the history endpoint, never a live sample.

    Each candle is validated against the *previous candle in this series*
    (candles are sorted into ascending date order first, since the API's own
    ordering is not something this module assumes) using the same
    gap-aware, symbol-aware tolerance as :func:`write_provisional`
    (:func:`compounded_deviation_pct`) — not just the absolute sanity check.
    Before this, seeding only ever compared each close in isolation
    (``is_plausible_price(candle.close, None)``), so a corrupt-but-positive
    price anywhere in the seeded history was accepted outright and would
    silently poison the 260-bar window DESIGN §1.4's cone signal depends on
    for the next year. The very first candle in the series has no
    predecessor within the batch, so — same as the first bar a symbol has
    ever seen in :func:`write_provisional` — only the absolute check applies
    to it.

    A rejected candle does not advance the "previous close" used for the
    next comparison — the next candle is compared against the last
    *accepted* close (with the session gap widened accordingly), so one bad
    candle drops a single bar rather than cascading into rejecting an
    entire correct tail of the series just because it now looks like a
    large move from the corrupt one.

    A candle rejected on the absolute check (non-finite, non-positive, or
    below :data:`MIN_PLAUSIBLE_PRICE`) is logged at ``warning`` — that is
    unambiguously bad data. A candle rejected only on the *deviation* check
    (a plausible-looking positive price that is still a corrupt outlier
    relative to its neighbours) is logged at ``error``: it is a hole opened
    in the exact history the cone signal depends on, and DESIGN's
    never-fail-silently philosophy (§5, §6.G) means that deserves louder
    visibility than a routine drop, not quieter.

    Returns:
        The number of bars actually written (after dropping any implausible
        candle).

    Raises:
        EtoroValidationError: if the API returned no candles at all — an
            empty seed must be a loud failure, never a silently-empty bar
            store that later reads as "still in warmup" with no obvious cause.
    """
    instrument_id = client.resolve_instrument_id(symbol)
    candles = client.get_candles(
        instrument_id, direction="backward", interval="OneDay", count=count
    )
    if not candles:
        raise EtoroValidationError(
            f"no candles returned while seeding {symbol} (instrument "
            f"{instrument_id}) — refusing to leave the bar store silently empty"
        )
    ordered = sorted(candles, key=lambda c: c.date)
    bars = []
    dropped_absolute = 0
    dropped_deviation = 0
    previous_close: float | None = None
    previous_date: str | None = None
    for candle in ordered:
        if previous_close is not None and previous_date is not None:
            sessions = sessions_elapsed(previous_date, candle.date)
            deviation_pct = compounded_deviation_pct(symbol, sessions)
        else:
            sessions = 0
            deviation_pct = MAX_TICK_DEVIATION_PCT  # unused: no predecessor yet
        if not is_plausible_price(candle.close, previous_close, max_deviation_pct=deviation_pct):
            close = candle.close
            is_absolute_failure = (
                close is None or not math.isfinite(close) or close < MIN_PLAUSIBLE_PRICE
            )
            if is_absolute_failure or previous_close is None:
                dropped_absolute += 1
                _logger.warning(
                    "seed(%s): dropped candle for %s with an implausible "
                    "close %r",
                    symbol, candle.date, candle.close,
                )
            else:
                dropped_deviation += 1
                _logger.error(
                    "seed(%s): dropped candle for %s (close=%r) — deviates "
                    "more than %.0f%% from the last accepted close %r on "
                    "%s (%d session gap). This opens a hole in the seeded "
                    "history the cone signal depends on (DESIGN §1.4) — "
                    "verify %s's history and consider a targeted re-seed.",
                    symbol, candle.date, candle.close, deviation_pct * 100,
                    previous_close, previous_date, sessions, symbol,
                )
            # Deliberately do not advance previous_close/previous_date: the
            # next candle is compared against the last *good* close, not
            # this rejected one, so a single bad point cannot cascade into
            # rejecting an otherwise-correct tail of the series.
            continue
        bars.append(
            Bar(
                symbol=symbol,
                date=candle.date,
                close=candle.close,
                open=candle.open,
                high=candle.high,
                low=candle.low,
                volume=candle.volume,
                is_provisional=False,
                source="seed",
            )
        )
        previous_close = candle.close
        previous_date = candle.date
    dropped = dropped_absolute + dropped_deviation
    if dropped:
        _logger.warning(
            "seed(%s): dropped %d of %d candles (%d absolute, %d deviation)",
            symbol, dropped, len(candles), dropped_absolute, dropped_deviation,
        )
    written = repo.upsert_bars(bars)
    _logger.info(
        "seeded %d/%d requested bars for %s (instrument %d)",
        written, count, symbol, instrument_id,
    )
    return written


def write_provisional(repo: Repo, symbol: str, trading_date: str, rate: Rate) -> Bar:
    """Step [4] of the session: write today's provisional bar from a live rate.

    DESIGN §4.1/§4.2: at T-30min today's official close does not exist yet, so
    the live last price stands in as a provisional close for bar ``t`` —
    ``is_provisional=1``, ``source="live_rate"``. :func:`settle_previous` is
    what corrects this on a later session.

    Raises:
        BadTickError: if the live price is implausible against the previous
            *settled* close (DESIGN §6.G). Callers must abort the session on
            this — it is never safe to fall back to some other price and
            proceed, since the whole point is that this one data point cannot
            be trusted.

    The deviation tolerance is compounded across however many trading
    sessions actually separate the reference close from ``trading_date``
    (:func:`compounded_deviation_pct`) — a flat single-session band applied
    across a multi-day outage would reject a legitimate crash-recovery price
    at exactly the moment the strategy is meant to act on it (DESIGN §6.G).
    Past :data:`STALE_REFERENCE_TRADING_DAYS`, the check still runs (capped,
    so a gross fat-finger error is still caught) but also logs loudly —
    a comparison against a reference this old is not something to trust
    silently.
    """
    price = _rate_price(rate)
    previous_settled = repo.get_bars(symbol, limit=1, settled_only=True)
    previous_close = previous_settled[-1].close if previous_settled else None
    previous_date = previous_settled[-1].date if previous_settled else None
    sessions = sessions_elapsed(previous_date, trading_date) if previous_date else 1
    deviation_pct = compounded_deviation_pct(symbol, sessions)
    if previous_date and sessions > STALE_REFERENCE_TRADING_DAYS:
        _logger.error(
            "write_provisional(%s): reference settled close is from %s, "
            "%d trading sessions before %s (> %d) — bad-tick guard is "
            "comparing against a stale reference; band capped at %.0f%% "
            "rather than trusted at face value. Verify %s's bar history and "
            "consider re-seeding (DESIGN §6.G).",
            symbol, previous_date, sessions, trading_date,
            STALE_REFERENCE_TRADING_DAYS, deviation_pct * 100, symbol,
        )
    if not is_plausible_price(price, previous_close, max_deviation_pct=deviation_pct):
        raise BadTickError(
            f"{symbol} live price {price!r} on {trading_date} failed the "
            f"bad-tick guard against previous settled close {previous_close!r} "
            f"from {previous_date!r} ({sessions} session gap, DESIGN §6.G, "
            f"max deviation {deviation_pct:.0%})"
        )
    assert price is not None  # is_plausible_price already excluded None
    bar = Bar(
        symbol=symbol,
        date=trading_date,
        close=price,
        is_provisional=True,
        source="live_rate",
    )
    repo.upsert_bar(bar)
    return bar


def settle_previous(client: EtoroClient, repo: Repo, symbol: str) -> bool:
    """Step [2] of the session: settle ``symbol``'s provisional bar(s).

    DESIGN §4.2: overwrite each provisional row with the official ``OneDay``
    candle for that date and flip ``is_provisional`` to 0. Any bar still
    provisional after this call is a preflight gate 10 failure — that check
    lives in ``engine/preflight.py`` (a later step); this function only
    performs the settle and reports whether it made progress.

    Normally there is at most one provisional bar per symbol, since this runs
    every session before the next :func:`write_provisional`. Every
    provisional bar found is attempted, oldest first, so a missed run (e.g. a
    multi-day outage) is repaired in one call instead of needing N separate
    calls to catch up.

    A bar whose date has no matching official candle yet (or whose candle
    fails the bad-tick guard, DESIGN §6.G) is left provisional and logged —
    this is the "provisional bar that never settles" case. A caller must not
    treat a ``False`` return as an error by itself; only preflight gate 10's
    "still provisional" check makes that a hard failure for the session.

    The bad-tick check here uses the same gap-aware, symbol-aware
    compounded tolerance as :func:`write_provisional`
    (:func:`compounded_deviation_pct`), compared against the last *settled*
    close before each bar being settled — previously this compared against
    no reference at all (``is_plausible_price(candle.close, None)``), so an
    official candle could carry an arbitrarily large, non-absolute-implausible
    error and still settle. Bars in a backlog are settled oldest-first and
    each successfully-settled close becomes the reference for the next one
    in the same call, so a multi-bar backlog is validated bar-to-bar rather
    than only the first bar being checked against real history.

    Returns:
        True if at least one bar was settled, False otherwise.
    """
    provisional = sorted(
        (b for b in repo.provisional_bars() if b.symbol == symbol),
        key=lambda b: b.date,
    )
    if not provisional:
        return False

    instrument_id = client.resolve_instrument_id(symbol)
    # A handful of recent candles covers any realistic backlog without
    # re-fetching the whole seeded history on every settle.
    lookback = max(5, len(provisional) + 2)
    candles_by_date = {
        candle.date: candle
        for candle in client.get_candles(
            instrument_id, direction="backward", interval="OneDay", count=lookback
        )
    }

    reference_settled = repo.get_bars(symbol, limit=1, settled_only=True)
    reference_close = reference_settled[-1].close if reference_settled else None
    reference_date = reference_settled[-1].date if reference_settled else None

    settled_any = False
    for bar in provisional:
        candle = candles_by_date.get(bar.date)
        if candle is None:
            _logger.warning(
                "settle_previous(%s): no official candle yet for %s — still "
                "provisional",
                symbol, bar.date,
            )
            continue
        if reference_date is not None:
            sessions = sessions_elapsed(reference_date, bar.date)
            deviation_pct = compounded_deviation_pct(symbol, sessions)
            if sessions > STALE_REFERENCE_TRADING_DAYS:
                _logger.error(
                    "settle_previous(%s): reference settled close is from "
                    "%s, %d trading sessions before %s (> %d) — bad-tick "
                    "guard is comparing against a stale reference; band "
                    "capped at %.0f%%. Verify %s's bar history (DESIGN "
                    "§6.G).",
                    symbol, reference_date, sessions, bar.date,
                    STALE_REFERENCE_TRADING_DAYS, deviation_pct * 100, symbol,
                )
        else:
            deviation_pct = MAX_TICK_DEVIATION_PCT  # unused: no reference yet
        if not is_plausible_price(candle.close, reference_close, max_deviation_pct=deviation_pct):
            _logger.warning(
                "settle_previous(%s): official candle for %s has close %r, "
                "implausible against reference close %r from %r — leaving "
                "provisional rather than settling on bad data",
                symbol, bar.date, candle.close, reference_close, reference_date,
            )
            continue
        repo.settle_bar(
            symbol,
            bar.date,
            close=candle.close,
            open_=candle.open,
            high=candle.high,
            low=candle.low,
            volume=candle.volume,
            source="candles",
        )
        settled_any = True
        reference_close = candle.close
        reference_date = bar.date
    return settled_any


def missing_trading_days(repo: Repo, symbol: str, start: str, end: str) -> list[str]:
    """Trading days (per :mod:`nightwatch.market.calendar`) between ``start``
    and ``end`` (inclusive, ``YYYY-MM-DD``) for which ``symbol`` has no
    stored bar at all.

    A gap here is a real failure mode for a rolling 260-bar signal:
    :meth:`Repo.count_bars` counting rows cannot distinguish "265 contiguous
    bars" from "265 bars with a 20-day hole in the middle", and those are not
    equivalent inputs to ``cone_5_z``. This function is diagnostic only — it
    does not repair anything and does not decide what a caller (a future
    preflight gate, or a targeted re-seed) should do about a gap it finds.
    """
    existing = {b.date for b in repo.get_bars(symbol)}
    start_date = _date.fromisoformat(start)
    end_date = _date.fromisoformat(end)
    missing: list[str] = []
    current = start_date
    while current <= end_date:
        if calendar.is_trading_day(current):
            iso = current.isoformat()
            if iso not in existing:
                missing.append(iso)
        current += timedelta(days=1)
    return missing
