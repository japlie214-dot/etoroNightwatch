"""Tests for nightwatch.market.bars (DESIGN §1.4, §4.2, §6.G).

The eToro client is driven entirely through `httpx.MockTransport`, exactly
like tests/test_etoro_client.py -- no network, no real credentials. These
tests specifically cover the awkward cases DESIGN calls out: seeding to
>=265 bars, a gap in stored history, a provisional bar that gets settled, one
that never settles, and a bad tick that must be rejected rather than stored.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from typing import Any, Iterator

import httpx
import pytest

from nightwatch.config import EtoroConfig, EtoroEnvironment
from nightwatch.db.repo import Bar, Repo
from nightwatch.etoro.client import EtoroClient
from nightwatch.etoro.errors import EtoroValidationError
from nightwatch.etoro.models import Rate
from nightwatch.market import bars

DEMO_PRIVATE_KEY = "demo_private_key_for_tests_1234567890"
DEMO_PUBLIC_KEY = "demo_public_key_for_tests_0987654321"


def _config() -> EtoroConfig:
    return EtoroConfig(
        environment=EtoroEnvironment.DEMO,
        secrets={
            "ETORO_DEMO_PRIVATE_KEY": DEMO_PRIVATE_KEY,
            "ETORO_PUBLIC_KEY": DEMO_PUBLIC_KEY,
        },
    )


class ScriptedTransport:
    """FIFO of canned responses, keyed by nothing -- pops in call order."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("ScriptedTransport ran out of scripted responses")
        item = self.responses.pop(0)
        if callable(item) and not isinstance(item, httpx.Response):
            return item(request)
        return item


def _client(transport: ScriptedTransport, repo: Repo) -> EtoroClient:
    return EtoroClient(
        _config(), transport=httpx.MockTransport(transport.handler), repo=repo
    )


@contextmanager
def _capture_bars_logs() -> Iterator[list[logging.LogRecord]]:
    """Attach a handler directly to ``nightwatch.market.bars`` rather than
    relying on caplog's root-attached handler: another test module may have
    set ``nightwatch`` logger ``propagate = False`` via ``configure_logging()``
    (see test_notify_telegram.py), which would otherwise silently stop
    records from this module reaching caplog when the full suite runs."""
    logger = logging.getLogger("nightwatch.market.bars")
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)


def _search_response(instrument_id: int = 1001, symbol: str = "SPY") -> httpx.Response:
    return httpx.Response(
        200,
        json={"items": [{"instrumentId": instrument_id, "internalSymbolFull": symbol}]},
    )


def _candles_response(rows: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(200, json={"candles": rows})


def _daily_dates(n: int, start_year: int = 2024) -> list[str]:
    """`n` ascending weekday-ish calendar dates -- fine for a fake API's
    'trading days', since these tests never validate the calendar module."""
    from datetime import date, timedelta

    out: list[str] = []
    d = date(start_year, 1, 1)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


# ===========================================================================
# seed() -- DESIGN §1.4: >=265 SPY bars mandatory, seed loads 400
# ===========================================================================


def test_seed_writes_400_settled_bars(repo: Repo) -> None:
    dates = _daily_dates(400)
    rows = [
        {"date": d, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0 + i,
         "volume": 1000.0}
        for i, d in enumerate(dates)
    ]
    transport = ScriptedTransport([_search_response(), _candles_response(rows)])
    client = _client(transport, repo)

    written = bars.seed(client, repo, "SPY", count=400)

    assert written == 400
    assert repo.count_bars("SPY", settled_only=True) == 400
    stored = repo.get_bars("SPY")
    assert all(b.is_provisional is False for b in stored)
    assert all(b.source == "seed" for b in stored)


def test_seed_meets_the_265_bar_mandatory_minimum_for_the_cone_signal(repo: Repo) -> None:
    """DESIGN §1.4: fewer than 265 settled SPY bars means cone_5_z is None
    forever. Seeding the documented 400 must clear that bar with headroom."""
    dates = _daily_dates(400)
    rows = [{"date": d, "close": 100.0} for d in dates]
    transport = ScriptedTransport([_search_response(), _candles_response(rows)])
    client = _client(transport, repo)

    bars.seed(client, repo, "SPY", count=400)

    assert repo.count_bars("SPY", settled_only=True) >= 265


def test_seed_raises_on_an_empty_candle_response_rather_than_silently_seeding_zero(
    repo: Repo,
) -> None:
    transport = ScriptedTransport([_search_response(), _candles_response([])])
    client = _client(transport, repo)
    with pytest.raises(EtoroValidationError):
        bars.seed(client, repo, "SPY", count=400)
    assert repo.count_bars("SPY", settled_only=False) == 0


def test_seed_drops_an_implausible_candle_rather_than_storing_it(repo: Repo) -> None:
    dates = _daily_dates(5)
    rows = [{"date": d, "close": 100.0} for d in dates]
    rows[2]["close"] = -5.0  # nonsense price buried in the middle of the batch
    transport = ScriptedTransport([_search_response(), _candles_response(rows)])
    client = _client(transport, repo)

    written = bars.seed(client, repo, "SPY", count=5)

    assert written == 4
    assert repo.get_bar("SPY", dates[2]) is None


def test_seed_drops_a_corrupt_but_positive_mid_series_candle_via_deviation_check(
    repo: Repo,
) -> None:
    """DESIGN §1.4/§6.G: before this fix, seed() only ever ran the absolute
    sanity check (`is_plausible_price(candle.close, None)`), so a positive
    but corrupt price anywhere in the seeded history (a decimal-point
    error, a units mixup) was accepted outright and would poison the
    260-bar cone window. It must now be caught by the bar-to-bar deviation
    check against the previous candle *in the series*."""
    dates = _daily_dates(10)
    rows = [{"date": d, "close": 100.0} for d in dates]
    rows[5]["close"] = 1500.0  # positive, passes the absolute check, but a
    # 1400% jump from its neighbours -- not a real single-session move.
    transport = ScriptedTransport([_search_response(), _candles_response(rows)])
    client = _client(transport, repo)

    with _capture_bars_logs() as records:
        written = bars.seed(client, repo, "SPY", count=10)

    assert written == 9
    assert repo.get_bar("SPY", dates[5]) is None
    # The bars either side of the dropped one must still be present -- one
    # bad candle must not cascade into rejecting its correct neighbours.
    assert repo.get_bar("SPY", dates[4]) is not None
    assert repo.get_bar("SPY", dates[4]).close == 100.0
    assert repo.get_bar("SPY", dates[6]) is not None
    assert repo.get_bar("SPY", dates[6]).close == 100.0
    assert any(
        r.levelno >= logging.ERROR and "hole" in r.getMessage().lower()
        for r in records
    )


def test_seed_still_drops_the_first_candle_on_an_absolute_failure(repo: Repo) -> None:
    """The first candle in the series has no predecessor to compare against
    -- same as write_provisional's first-ever-bar case -- so only the
    absolute check applies to it, and a bad first candle must not disable
    validation for the rest of the series."""
    dates = _daily_dates(5)
    rows = [{"date": d, "close": 100.0} for d in dates]
    rows[0]["close"] = -1.0
    transport = ScriptedTransport([_search_response(), _candles_response(rows)])
    client = _client(transport, repo)

    written = bars.seed(client, repo, "SPY", count=5)

    assert written == 4
    assert repo.get_bar("SPY", dates[0]) is None
    assert repo.get_bar("SPY", dates[1]) is not None


# ===========================================================================
# missing_trading_days() -- a gap in the bar history
# ===========================================================================


def test_missing_trading_days_reports_a_gap(repo: Repo) -> None:
    # A contiguous run of trading days with one business day removed in the
    # middle -- 2026-03-09 (Mon) .. 2026-03-13 (Fri), dropping 2026-03-11.
    for d in ("2026-03-09", "2026-03-10", "2026-03-12", "2026-03-13"):
        repo.upsert_bar(Bar(symbol="SPY", date=d, close=100.0, source="seed"))

    gaps = bars.missing_trading_days(repo, "SPY", "2026-03-09", "2026-03-13")

    assert gaps == ["2026-03-11"]


def test_missing_trading_days_never_flags_a_weekend_or_holiday_as_missing(
    repo: Repo,
) -> None:
    # No bars stored at all for a window spanning a weekend and a holiday
    # (2026-01-01 New Year's Day, a Thursday) -- only the *trading* days in
    # that window should be reported missing, not the closed days.
    gaps = bars.missing_trading_days(repo, "SPY", "2025-12-31", "2026-01-04")
    assert "2026-01-01" not in gaps  # holiday
    assert "2026-01-03" not in gaps  # Saturday
    assert "2026-01-04" not in gaps  # Sunday
    assert "2025-12-31" in gaps  # a normal trading Wednesday, genuinely missing


def test_missing_trading_days_is_empty_when_history_is_complete(repo: Repo) -> None:
    for d in ("2026-03-09", "2026-03-10", "2026-03-11", "2026-03-12", "2026-03-13"):
        repo.upsert_bar(Bar(symbol="SPY", date=d, close=100.0, source="seed"))
    assert bars.missing_trading_days(repo, "SPY", "2026-03-09", "2026-03-13") == []


# ===========================================================================
# write_provisional() -- today's live-rate bar, and the bad-tick guard
# ===========================================================================


def test_write_provisional_stores_a_provisional_bar_from_last_execution(
    repo: Repo,
) -> None:
    repo.upsert_bar(Bar(symbol="SPY", date="2026-03-10", close=500.0, source="candles",
                         is_provisional=False))
    rate = Rate(instrument_id=1001, bid=505.9, ask=506.1, last_execution=506.0)

    bar = bars.write_provisional(repo, "SPY", "2026-03-11", rate)

    assert bar.is_provisional is True
    assert bar.source == "live_rate"
    assert bar.close == 506.0
    stored = repo.get_bar("SPY", "2026-03-11")
    assert stored is not None
    assert stored.is_provisional is True


def test_write_provisional_falls_back_to_bid_ask_midpoint_when_no_last_execution(
    repo: Repo,
) -> None:
    rate = Rate(instrument_id=1001, bid=100.0, ask=102.0, last_execution=None)
    bar = bars.write_provisional(repo, "SPY", "2026-03-11", rate)
    assert bar.close == pytest.approx(101.0)


def test_write_provisional_accepts_the_first_ever_bar_with_no_history_to_compare(
    repo: Repo,
) -> None:
    """No previous settled bar exists yet -- the bad-tick guard must not
    reject the very first bar for lack of something to compare against."""
    rate = Rate(instrument_id=1001, bid=99.0, ask=101.0, last_execution=100.0)
    bar = bars.write_provisional(repo, "SPY", "2026-03-11", rate)
    assert bar.close == 100.0


def test_write_provisional_rejects_a_bad_tick_far_from_the_settled_close(
    repo: Repo,
) -> None:
    repo.upsert_bar(Bar(symbol="SPY", date="2026-03-10", close=500.0, source="candles",
                         is_provisional=False))
    # A "fat finger" style price, 90% off the last settled close.
    bad_rate = Rate(instrument_id=1001, bid=49.0, ask=51.0, last_execution=50.0)

    with pytest.raises(bars.BadTickError):
        bars.write_provisional(repo, "SPY", "2026-03-11", bad_rate)

    # And critically: nothing was written for that date.
    assert repo.get_bar("SPY", "2026-03-11") is None


def test_write_provisional_rejects_a_zero_or_negative_price(repo: Repo) -> None:
    rate = Rate(instrument_id=1001, bid=0.0, ask=0.0, last_execution=0.0)
    with pytest.raises(bars.BadTickError):
        bars.write_provisional(repo, "SPY", "2026-03-11", rate)


def test_write_provisional_accepts_a_large_genuine_upro_move_a_flat_threshold_would_reject(
    repo: Repo,
) -> None:
    """UPRO is 3x-leveraged (DESIGN §6.A) and has genuinely moved -35.0% in a
    single real session (2020-03-16). The unleveraged 20% guard would
    wrongly reject that as a bad tick; the per-symbol threshold
    (bars.max_tick_deviation_pct) must let it through."""
    repo.upsert_bar(Bar(symbol="UPRO", date="2020-03-13", close=20.0,
                         source="candles", is_provisional=False))
    # A 30% single-session drop -- past the unleveraged 20% ceiling, but well
    # inside UPRO's own historical range and inside LEVERAGED_TICK_DEVIATION_PCT.
    rate = Rate(instrument_id=2002, bid=13.9, ask=14.1, last_execution=14.0)

    bar = bars.write_provisional(repo, "UPRO", "2020-03-16", rate)

    assert bar.close == 14.0


def test_write_provisional_still_rejects_a_gross_bad_tick_on_upro(repo: Repo) -> None:
    """The wider UPRO threshold is not a blank check -- a fat-finger price far
    beyond even UPRO's historical extremes must still be rejected."""
    repo.upsert_bar(Bar(symbol="UPRO", date="2020-03-13", close=20.0,
                         source="candles", is_provisional=False))
    rate = Rate(instrument_id=2002, bid=1.9, ask=2.1, last_execution=2.0)  # -90%

    with pytest.raises(bars.BadTickError):
        bars.write_provisional(repo, "UPRO", "2020-03-16", rate)


def test_max_tick_deviation_pct_is_wider_for_upro_than_the_default() -> None:
    assert bars.max_tick_deviation_pct("UPRO") == bars.LEVERAGED_TICK_DEVIATION_PCT
    assert bars.max_tick_deviation_pct("UPRO") > bars.MAX_TICK_DEVIATION_PCT
    assert bars.max_tick_deviation_pct("SPY") == bars.MAX_TICK_DEVIATION_PCT
    assert bars.max_tick_deviation_pct("SCHG") == bars.MAX_TICK_DEVIATION_PCT


def test_write_provisional_accepts_a_legitimate_crash_move_after_a_multi_week_gap(
    repo: Repo,
) -> None:
    """DESIGN §6.G's own scenario: the app misses a stretch of sessions
    during a crash (dead battery, lost Private DNS, a background-killed
    service) and comes back to find SPY has genuinely fallen far more than
    a single-session 20% band -- Feb-Mar 2020 SPY fell ~34% top-to-bottom.
    A flat single-session threshold would wrongly reject this as a bad
    tick, refusing to trade precisely when the strategy exists to act."""
    repo.upsert_bar(Bar(symbol="SPY", date="2020-02-19", close=340.0,
                         source="candles", is_provisional=False))
    # ~34% down, and 2020-03-23 is ~23 trading sessions after 2020-02-19 --
    # a realistic multi-week outage, not an ordinary overnight gap.
    rate = Rate(instrument_id=1001, bid=224.0, ask=226.0, last_execution=225.0)

    bar = bars.write_provisional(repo, "SPY", "2020-03-23", rate)

    assert bar.close == 225.0


def test_write_provisional_accepts_a_legitimate_upro_crash_move_after_a_multi_week_gap(
    repo: Repo,
) -> None:
    """Same scenario as above, but for UPRO -- the leveraged instrument's
    own worse real drawdown (~-75% over the same window) must also survive
    a multi-week gap, not just the unleveraged case."""
    repo.upsert_bar(Bar(symbol="UPRO", date="2020-02-19", close=20.0,
                         source="candles", is_provisional=False))
    rate = Rate(instrument_id=2002, bid=4.9, ask=5.1, last_execution=5.0)  # -75%

    bar = bars.write_provisional(repo, "UPRO", "2020-03-23", rate)

    assert bar.close == 5.0


def test_write_provisional_still_rejects_a_fat_finger_after_a_multi_week_gap(
    repo: Repo,
) -> None:
    """The gap-aware tolerance is capped (bars._DEVIATION_CEILING_MULTIPLE)
    precisely so that a long gap does not become a blank check: a gross
    fat-finger error (an order-of-magnitude / decimal-point style mistake)
    must still be rejected no matter how stale the reference is."""
    repo.upsert_bar(Bar(symbol="SPY", date="2020-02-19", close=340.0,
                         source="candles", is_provisional=False))
    # 10x the reference close -- no real SPY session, over any gap, does this.
    rate = Rate(instrument_id=1001, bid=3390.0, ask=3410.0, last_execution=3400.0)

    with pytest.raises(bars.BadTickError):
        bars.write_provisional(repo, "SPY", "2020-03-23", rate)


def test_write_provisional_still_rejects_a_fat_finger_after_a_short_gap(
    repo: Repo,
) -> None:
    """The existing short-gap fat-finger rejection (single adjacent trading
    day) must keep working now that the tolerance calculation goes through
    the gap-aware code path for every call, not just the long-gap one."""
    repo.upsert_bar(Bar(symbol="SPY", date="2026-03-10", close=500.0,
                         source="candles", is_provisional=False))
    rate = Rate(instrument_id=1001, bid=49.0, ask=51.0, last_execution=50.0)  # -90%

    with pytest.raises(bars.BadTickError):
        bars.write_provisional(repo, "SPY", "2026-03-11", rate)


def test_write_provisional_logs_loudly_past_the_stale_reference_threshold(
    repo: Repo,
) -> None:
    """DESIGN's never-fail-silently philosophy (§5, §6.G): once the
    reference is stale enough that the (capped) tolerance is doing most of
    the work rather than a meaningful comparison, that must be visible in
    the logs even though the session is allowed to proceed."""
    repo.upsert_bar(Bar(symbol="SPY", date="2020-02-19", close=340.0,
                         source="candles", is_provisional=False))
    rate = Rate(instrument_id=1001, bid=224.0, ask=226.0, last_execution=225.0)

    with _capture_bars_logs() as records:
        bars.write_provisional(repo, "SPY", "2020-03-23", rate)

    assert any(
        r.levelno >= logging.ERROR and "stale" in r.getMessage().lower()
        for r in records
    )


def test_sessions_elapsed_is_one_for_adjacent_trading_days() -> None:
    assert bars.sessions_elapsed("2026-03-10", "2026-03-11") == 1
    # Friday -> Monday, weekend in between, still exactly one session.
    assert bars.sessions_elapsed("2020-03-13", "2020-03-16") == 1


def test_compounded_deviation_pct_matches_base_pct_at_a_single_session_gap() -> None:
    assert bars.compounded_deviation_pct("SPY", 1) == bars.MAX_TICK_DEVIATION_PCT
    assert bars.compounded_deviation_pct("UPRO", 1) == bars.LEVERAGED_TICK_DEVIATION_PCT


def test_compounded_deviation_pct_grows_then_is_capped() -> None:
    two_session = bars.compounded_deviation_pct("SPY", 2)
    assert two_session == pytest.approx(1.2 ** 2 - 1)
    assert two_session > bars.MAX_TICK_DEVIATION_PCT
    # Far enough out, the ceiling (10x the base pct) binds.
    assert bars.compounded_deviation_pct("SPY", 100) == pytest.approx(
        bars.MAX_TICK_DEVIATION_PCT * 10.0
    )
    assert bars.compounded_deviation_pct("UPRO", 100) == pytest.approx(
        bars.LEVERAGED_TICK_DEVIATION_PCT * 10.0
    )


def test_is_plausible_price_pure_predicate_boundaries() -> None:
    assert bars.is_plausible_price(100.0, 100.0) is True
    assert bars.is_plausible_price(119.0, 100.0) is True  # 19% move, within 20%
    assert bars.is_plausible_price(121.0, 100.0) is False  # 21% move, over the line
    assert bars.is_plausible_price(None, 100.0) is False
    assert bars.is_plausible_price(float("nan"), 100.0) is False
    assert bars.is_plausible_price(float("inf"), 100.0) is False
    assert bars.is_plausible_price(0.0, None) is False
    assert bars.is_plausible_price(5.0, None) is True  # no history to compare


# ===========================================================================
# settle_previous() -- DESIGN §4.2's provisional -> settled transition
# ===========================================================================


def test_settle_previous_overwrites_the_provisional_bar_and_flips_the_flag(
    repo: Repo,
) -> None:
    repo.upsert_bar(
        Bar(symbol="SPY", date="2026-03-11", close=506.0, source="live_rate",
            is_provisional=True)
    )
    transport = ScriptedTransport(
        [
            _search_response(),
            _candles_response(
                [{"date": "2026-03-11", "open": 504.0, "high": 507.0, "low": 503.5,
                  "close": 505.75, "volume": 12345.0}]
            ),
        ]
    )
    client = _client(transport, repo)

    settled = bars.settle_previous(client, repo, "SPY")

    assert settled is True
    stored = repo.get_bar("SPY", "2026-03-11")
    assert stored is not None
    assert stored.is_provisional is False
    assert stored.close == 505.75
    assert stored.source == "candles"


def test_settle_previous_is_a_no_op_when_nothing_is_provisional(repo: Repo) -> None:
    repo.upsert_bar(
        Bar(symbol="SPY", date="2026-03-10", close=500.0, source="candles",
            is_provisional=False)
    )
    transport = ScriptedTransport([])  # must not even resolve the instrument
    client = _client(transport, repo)

    settled = bars.settle_previous(client, repo, "SPY")

    assert settled is False
    assert transport.requests == []


def test_settle_previous_leaves_a_bar_provisional_when_no_official_candle_exists_yet(
    repo: Repo,
) -> None:
    """The 'provisional bar that never settles' case: the official candle
    series simply does not have that date yet (e.g. settle ran before the
    exchange published it). The bar must stay provisional, not error out."""
    repo.upsert_bar(
        Bar(symbol="SPY", date="2026-03-11", close=506.0, source="live_rate",
            is_provisional=True)
    )
    transport = ScriptedTransport(
        [
            _search_response(),
            _candles_response(
                [{"date": "2026-03-10", "close": 500.0}]  # no 2026-03-11 row
            ),
        ]
    )
    client = _client(transport, repo)

    settled = bars.settle_previous(client, repo, "SPY")

    assert settled is False
    stored = repo.get_bar("SPY", "2026-03-11")
    assert stored is not None
    assert stored.is_provisional is True
    assert stored.close == 506.0  # untouched


def test_settle_previous_settles_multiple_backlogged_bars_in_date_order(
    repo: Repo,
) -> None:
    repo.upsert_bar(Bar(symbol="SPY", date="2026-03-10", close=500.0,
                         source="live_rate", is_provisional=True))
    repo.upsert_bar(Bar(symbol="SPY", date="2026-03-11", close=506.0,
                         source="live_rate", is_provisional=True))
    transport = ScriptedTransport(
        [
            _search_response(),
            _candles_response(
                [
                    {"date": "2026-03-10", "close": 499.5},
                    {"date": "2026-03-11", "close": 505.75},
                ]
            ),
        ]
    )
    client = _client(transport, repo)

    settled = bars.settle_previous(client, repo, "SPY")

    assert settled is True
    assert repo.get_bar("SPY", "2026-03-10").close == 499.5
    assert repo.get_bar("SPY", "2026-03-11").close == 505.75
    assert repo.count_bars("SPY", settled_only=True) == 2


def test_settle_previous_does_not_settle_a_different_symbols_provisional_bar(
    repo: Repo,
) -> None:
    repo.upsert_bar(Bar(symbol="UPRO", date="2026-03-11", close=80.0,
                         source="live_rate", is_provisional=True))
    transport = ScriptedTransport([])  # SPY has nothing provisional to settle
    client = _client(transport, repo)

    settled = bars.settle_previous(client, repo, "SPY")

    assert settled is False
    assert transport.requests == []
    # UPRO's own provisional bar is untouched by settling a different symbol.
    assert repo.get_bar("UPRO", "2026-03-11").is_provisional is True


def test_settle_previous_leaves_bar_provisional_on_an_implausible_official_candle(
    repo: Repo,
) -> None:
    repo.upsert_bar(Bar(symbol="SPY", date="2026-03-11", close=506.0,
                         source="live_rate", is_provisional=True))
    transport = ScriptedTransport(
        [_search_response(), _candles_response([{"date": "2026-03-11", "close": -1.0}])]
    )
    client = _client(transport, repo)

    settled = bars.settle_previous(client, repo, "SPY")

    assert settled is False
    assert repo.get_bar("SPY", "2026-03-11").is_provisional is True


def test_settle_previous_leaves_bar_provisional_on_an_official_candle_that_deviates_too_far(
    repo: Repo,
) -> None:
    """Previously settle_previous validated the official candle only with
    `is_plausible_price(candle.close, None)` -- no reference at all -- so a
    positive-but-wildly-deviated official close would have settled without
    complaint as long as it cleared the absolute floor. With a real prior
    settled close available, the same bad-tick guard as write_provisional
    must apply here too."""
    repo.upsert_bar(Bar(symbol="SPY", date="2026-03-10", close=500.0,
                         source="candles", is_provisional=False))
    repo.upsert_bar(Bar(symbol="SPY", date="2026-03-11", close=506.0,
                         source="live_rate", is_provisional=True))
    transport = ScriptedTransport(
        [_search_response(),
         _candles_response([{"date": "2026-03-11", "close": 5000.0}])]  # +900%
    )
    client = _client(transport, repo)

    settled = bars.settle_previous(client, repo, "SPY")

    assert settled is False
    stored = repo.get_bar("SPY", "2026-03-11")
    assert stored.is_provisional is True
    assert stored.close == 506.0  # untouched


def test_settle_previous_settles_a_legitimate_crash_move_after_a_multi_week_gap(
    repo: Repo,
) -> None:
    """The same gap-aware compounding used by write_provisional must apply
    here: a genuinely large move following a long stretch without a
    settled bar (e.g. a missed backlog) must still settle."""
    repo.upsert_bar(Bar(symbol="SPY", date="2020-02-19", close=340.0,
                         source="candles", is_provisional=False))
    repo.upsert_bar(Bar(symbol="SPY", date="2020-03-23", close=225.0,
                         source="live_rate", is_provisional=True))
    transport = ScriptedTransport(
        [_search_response(),
         _candles_response([{"date": "2020-03-23", "close": 224.5}])]
    )
    client = _client(transport, repo)

    settled = bars.settle_previous(client, repo, "SPY")

    assert settled is True
    assert repo.get_bar("SPY", "2020-03-23").close == 224.5
