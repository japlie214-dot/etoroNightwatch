"""Tests for nightwatch.market.window -- the fetch-derive-discard window
assembly (DESIGN_market_data.md §3, §12 build steps 3-4).

Entirely offline. ``FakeTiingoClient`` below stands in for
``market.source.TiingoClient`` -- these tests never construct a real
``TiingoClient`` or make an HTTP call (the dev PC cannot reach Tiingo from
this task's environment either; see this task's final report). Every window
built here is a synthetic, hand-constructed series, generated to line up
exactly with ``exchange_calendars``' real XNYS session list for a fixed
historical date range -- so these tests exercise the real calendar
dependency (already a project requirement, DESIGN_market_data.md §2.6/§9),
never a fake one, while keeping every input hand-controlled.
"""

from __future__ import annotations

from datetime import date

import pytest

from nightwatch.engine.preflight import ProvisionalClose
from nightwatch.etoro.models import Candle
from nightwatch.market.source import EodBar, IexQuote
from nightwatch.market.window import (
    MIN_HISTORY_BARS,
    REQUIRED_HISTORY_BARS,
    WindowAssemblyError,
    assemble_spy_signal,
    compute_fingerprint,
    expected_sessions,
    fetch_symbol_window,
    resolve_provisional_close,
    validate_window_completeness,
)


class FakeTiingoClient:
    """Duck-types ``market.source.TiingoClient.get_eod_history`` only --
    ``market.window`` never calls anything else on it."""

    def __init__(self, bars: list[EodBar]) -> None:
        self._bars = bars
        self.calls: list[tuple[str, str | None, str | None]] = []

    def get_eod_history(
        self, ticker: str, *, start_date: str | None = None, end_date: str | None = None
    ) -> list[EodBar]:
        self.calls.append((ticker, start_date, end_date))
        return list(self._bars)


def _bar(d: str, adj_close: float, close: float | None = None) -> EodBar:
    return EodBar(
        date=d, close=close if close is not None else adj_close,
        adj_close=adj_close, split_factor=1.0, div_cash=0.0,
    )


def _real_session_window(start: str, end: str) -> list[str]:
    """Every real XNYS session in ``[start, end]`` -- the same oracle
    :func:`~nightwatch.market.window.expected_sessions` itself is, used here
    only to build a synthetic-but-calendar-aligned fixture, not to validate
    the function against itself for every test below (see the negative
    tests, which hand-remove/add specific dates instead)."""
    return list(expected_sessions(start, end))


# A real ~39-month historical span with >=819 XNYS sessions (MIN_HISTORY_BARS,
# HANDOFF_2026-09-05_evening.md §3), entirely in the past so it never depends
# on "today". Verified against a real exchange_calendars XNYS session count
# (825 sessions in this exact range) while writing this fixture.
_WINDOW_START = "2022-05-17"
_WINDOW_END = "2025-08-29"


def _full_synthetic_window() -> list[EodBar]:
    sessions = _real_session_window(_WINDOW_START, _WINDOW_END)
    assert len(sessions) >= MIN_HISTORY_BARS  # fixture sanity
    # Monotonically increasing, non-degenerate closes -- never all-equal
    # (cone_5_z raises on a zero-variance window, by design).
    return [_bar(d, 100.0 + 0.05 * i) for i, d in enumerate(sessions)]


# ---------------------------------------------------------------------------
# expected_sessions
# ---------------------------------------------------------------------------


def test_expected_sessions_excludes_weekends_and_holidays():
    sessions = expected_sessions("2025-12-22", "2025-12-29")
    # 2025-12-25 (Christmas, Thursday) and the weekend (27th/28th) excluded.
    assert "2025-12-25" not in sessions
    assert "2025-12-27" not in sessions
    assert "2025-12-28" not in sessions
    assert "2025-12-24" in sessions  # early-close, but still a session
    assert "2025-12-26" in sessions


def test_expected_sessions_single_day_range():
    assert expected_sessions("2025-12-24", "2025-12-24") == ("2025-12-24",)
    assert expected_sessions("2025-12-25", "2025-12-25") == ()


# ---------------------------------------------------------------------------
# fetch_symbol_window
# ---------------------------------------------------------------------------


def test_fetch_symbol_window_sorts_and_drops_todays_partial_bar():
    bars = [
        _bar("2025-01-03", 101.0),
        _bar("2025-01-02", 100.0),
        _bar("2025-01-06", 999.0),  # pretend "today" -- must be dropped
    ]
    tiingo = FakeTiingoClient(bars)
    result = fetch_symbol_window(tiingo, "SPY", today=date(2025, 1, 6))
    assert [b.date for b in result] == ["2025-01-02", "2025-01-03"]
    # Exactly one fetch (DESIGN_market_data.md §2.2/§3.1: one fetch = one
    # dividend/split-adjustment epoch).
    assert len(tiingo.calls) == 1


def test_fetch_symbol_window_raises_on_empty_response():
    tiingo = FakeTiingoClient([])
    with pytest.raises(WindowAssemblyError):
        fetch_symbol_window(tiingo, "SPY", today=date(2025, 1, 6))


def test_fetch_symbol_window_raises_when_every_bar_is_todays():
    tiingo = FakeTiingoClient([_bar("2025-01-06", 100.0)])
    with pytest.raises(WindowAssemblyError):
        fetch_symbol_window(tiingo, "SPY", today=date(2025, 1, 6))


# ---------------------------------------------------------------------------
# validate_window_completeness
# ---------------------------------------------------------------------------


def test_validate_window_completeness_passes_on_a_full_real_window():
    bars = _full_synthetic_window()
    result = validate_window_completeness("SPY", bars)
    assert result.passed, result.reason
    assert result.bar_count == len(bars)
    assert result.missing_sessions == ()
    assert result.unexpected_dates == ()


def test_validate_window_completeness_fails_below_min_sessions():
    bars = _full_synthetic_window()[:100]
    result = validate_window_completeness("SPY", bars)
    assert not result.passed
    assert "100" in result.reason


def test_validate_window_completeness_detects_a_missing_session():
    bars = _full_synthetic_window()
    # Drop one bar from the middle -- a real trading day now has no bar.
    del bars[150]
    result = validate_window_completeness("SPY", bars)
    assert not result.passed
    assert len(result.missing_sessions) == 1


def test_validate_window_completeness_detects_an_unexpected_date():
    bars = _full_synthetic_window()
    # 2024-12-25 is Christmas -- not a real XNYS session -- inject it as a
    # bogus extra bar (e.g. a hypothetical provider data error).
    bars.append(_bar("2024-12-25", 123.45))
    result = validate_window_completeness("SPY", bars)
    assert not result.passed
    assert result.unexpected_dates == ("2024-12-25",)


def test_validate_window_completeness_raises_on_duplicate_dates():
    bars = _full_synthetic_window()
    bars.append(_bar(bars[0].date, 999.0))
    with pytest.raises(WindowAssemblyError):
        validate_window_completeness("SPY", bars)


def test_validate_window_completeness_raises_on_empty_window():
    with pytest.raises(WindowAssemblyError):
        validate_window_completeness("SPY", [])


# ---------------------------------------------------------------------------
# compute_fingerprint
# ---------------------------------------------------------------------------


def test_compute_fingerprint_is_order_independent_and_deterministic():
    bars = _full_synthetic_window()
    reversed_bars = list(reversed(bars))
    fp1 = compute_fingerprint("SPY", bars, trading_date="2025-08-29")
    fp2 = compute_fingerprint("SPY", reversed_bars, trading_date="2025-08-29")
    assert fp1.series_sha256 == fp2.series_sha256
    assert fp1.window_start == bars[0].date
    assert fp1.window_end == bars[-1].date
    assert fp1.bar_count == len(bars)
    assert fp1.source == "tiingo"


def test_compute_fingerprint_changes_when_a_close_changes():
    bars = _full_synthetic_window()
    fp1 = compute_fingerprint("SPY", bars, trading_date="2025-08-29")
    mutated = list(bars)
    mutated[10] = _bar(mutated[10].date, mutated[10].adj_close + 0.01)
    fp2 = compute_fingerprint("SPY", mutated, trading_date="2025-08-29")
    assert fp1.series_sha256 != fp2.series_sha256


def test_compute_fingerprint_raises_on_empty_window():
    with pytest.raises(WindowAssemblyError):
        compute_fingerprint("SPY", [], trading_date="2025-08-29")


# ---------------------------------------------------------------------------
# resolve_provisional_close
# ---------------------------------------------------------------------------


def test_resolve_provisional_close_prefers_tiingo_iex():
    iex = IexQuote(ticker="SPY", tngo_last=123.45)
    candle = Candle(date="2025-08-29", open=1, high=2, low=1, close=100.0)
    result = resolve_provisional_close("SPY", iex_quote=iex, etoro_daily_candle=candle)
    assert isinstance(result, ProvisionalClose)
    assert result.source == "tiingo_iex"
    assert result.value == 123.45


def test_resolve_provisional_close_falls_back_to_etoro_candle():
    candle = Candle(date="2025-08-29", open=1, high=2, low=1, close=151.19)
    result = resolve_provisional_close("SPY", iex_quote=None, etoro_daily_candle=candle)
    assert result.source == "etoro_candle"
    assert result.value == 151.19


def test_resolve_provisional_close_raises_when_neither_source_available():
    with pytest.raises(WindowAssemblyError):
        resolve_provisional_close("SPY", iex_quote=None, etoro_daily_candle=None)


def test_resolve_provisional_close_never_reads_a_rate():
    # Structural guarantee, not just a docstring claim: the function's
    # signature has no parameter through which a Rate could even be passed.
    import inspect

    params = inspect.signature(resolve_provisional_close).parameters
    assert set(params) == {"symbol", "iex_quote", "etoro_daily_candle"}


# ---------------------------------------------------------------------------
# assemble_spy_signal (end-to-end, offline)
# ---------------------------------------------------------------------------


def test_assemble_spy_signal_end_to_end_produces_a_signal():
    bars = _full_synthetic_window()
    tiingo = FakeTiingoClient(bars)
    iex = IexQuote(ticker="SPY", tngo_last=bars[-1].adj_close + 1.0)

    signal = assemble_spy_signal(
        tiingo,
        trading_date="2025-09-02",
        iex_quote=iex,
        etoro_daily_candle=None,
        today=date(2025, 9, 2),
        open_lot_entry_spy_ath=[],
    )

    assert signal.completeness.passed
    assert signal.provisional_close.source == "tiingo_iex"
    assert signal.provisional_close.value == iex.tngo_last
    assert signal.cone_5_z is not None
    # A new high spliced on top of a monotonically increasing series must
    # itself be the new ATH, at tier 0 (no drawdown).
    assert signal.spy_context.ath == iex.tngo_last
    assert signal.spy_context.tier == 0
    assert signal.fingerprint.symbol == "SPY"
    assert signal.fingerprint.bar_count == len(bars)
    # [discard]: the raw bars are nowhere on the returned object.
    assert not hasattr(signal, "bars")
    assert not hasattr(signal, "history")


def test_assemble_spy_signal_raises_on_incomplete_window_by_default():
    bars = _full_synthetic_window()[:100]  # far short of 265
    tiingo = FakeTiingoClient(bars)
    iex = IexQuote(ticker="SPY", tngo_last=bars[-1].adj_close + 1.0)
    with pytest.raises(WindowAssemblyError):
        assemble_spy_signal(
            tiingo,
            trading_date="2025-09-02",
            iex_quote=iex,
            etoro_daily_candle=None,
            today=date(2025, 9, 2),
            open_lot_entry_spy_ath=[],
        )


def test_assemble_spy_signal_can_report_incompleteness_without_raising():
    bars = _full_synthetic_window()[:100]
    tiingo = FakeTiingoClient(bars)
    iex = IexQuote(ticker="SPY", tngo_last=bars[-1].adj_close + 1.0)
    signal = assemble_spy_signal(
        tiingo,
        trading_date="2025-09-02",
        iex_quote=iex,
        etoro_daily_candle=None,
        today=date(2025, 9, 2),
        enforce_completeness=False,
        open_lot_entry_spy_ath=[],
    )
    assert not signal.completeness.passed
    # Still short of 265 bars, so the signal itself is honestly None, not a
    # fabricated number computed off insufficient history.
    assert signal.cone_5_z is None


def test_assemble_spy_signal_raises_when_no_provisional_close_available():
    bars = _full_synthetic_window()
    tiingo = FakeTiingoClient(bars)
    with pytest.raises(WindowAssemblyError):
        assemble_spy_signal(
            tiingo,
            trading_date="2025-09-02",
            iex_quote=None,
            etoro_daily_candle=None,
            today=date(2025, 9, 2),
            open_lot_entry_spy_ath=[],
        )


def test_assemble_spy_signal_uses_etoro_candle_fallback():
    bars = _full_synthetic_window()
    tiingo = FakeTiingoClient(bars)
    candle = Candle(
        date="2025-09-02", open=1, high=2, low=1, close=bars[-1].adj_close + 2.0
    )
    signal = assemble_spy_signal(
        tiingo,
        trading_date="2025-09-02",
        iex_quote=None,
        etoro_daily_candle=candle,
        today=date(2025, 9, 2),
        open_lot_entry_spy_ath=[],
    )
    assert signal.provisional_close.source == "etoro_candle"
    assert signal.provisional_close.value == candle.close


# ---------------------------------------------------------------------------
# FIX 1 (HANDOFF_2026-09-05_evening.md §3): the 39-month / 819-bar window
# ---------------------------------------------------------------------------


def test_assemble_spy_signal_finds_a_peak_that_would_have_aged_out_of_a_13_month_window():
    """The whole reason to fetch 819 bars (39 months) instead of 276
    (13 months, FINDINGS_window_live_2026-09-05.md §2): the OLD design's
    bug was not a tail-window computation (there isn't one -- the ATH scans
    every bar in whatever was fetched) but the fetch itself only ever
    requesting ~276 days of history, so an older peak was never even
    downloaded. This builds one full 819+-bar window with an early peak
    more than 276 sessions before the window's last bar and confirms
    today's assembled ATH is that peak, not a lower recent price -- proving
    the *fetch* now reaches far enough back to see it at all.
    """
    sessions = _real_session_window(_WINDOW_START, _WINDOW_END)
    assert len(sessions) >= MIN_HISTORY_BARS  # fixture sanity

    peak_index = 5
    peak_value = 500.0
    # Confirm the peak really would have aged out of a 276-bar/13-month
    # fetch ending on the same last session -- otherwise this test would
    # not distinguish the fix from the old behaviour.
    assert len(sessions) - peak_index > 276

    bars = []
    for i, d in enumerate(sessions):
        if i == peak_index:
            value = peak_value
        else:
            # Oscillates well below the peak (never zero-variance, never
            # anywhere near peak_value) so cone_5_z's guard is satisfied.
            value = 200.0 + (i % 7) * 0.37
        bars.append(_bar(d, value))

    tiingo = FakeTiingoClient(bars)
    iex = IexQuote(ticker="SPY", tngo_last=205.0)  # far below the aged-out peak

    signal = assemble_spy_signal(
        tiingo,
        trading_date="2025-09-02",
        iex_quote=iex,
        etoro_daily_candle=None,
        today=date(2025, 9, 2),
        open_lot_entry_spy_ath=[],
    )

    assert signal.completeness.passed
    assert signal.spy_context.ath == peak_value
    # A real, non-zero drawdown against the true (old) peak -- a 276-bar
    # fetch would have silently reported tier 0 here instead.
    assert signal.spy_context.tier > 0


# ---------------------------------------------------------------------------
# FIX 2a (HANDOFF_2026-09-05_evening.md §3a Case 2): the ATH floor
# ---------------------------------------------------------------------------


def test_assemble_spy_signal_ath_floor_uses_stamped_open_lot_ath_above_rolling_max():
    bars = _full_synthetic_window()
    tiingo = FakeTiingoClient(bars)
    iex = IexQuote(ticker="SPY", tngo_last=bars[-1].adj_close + 1.0)
    # A hypothetical lot stamped at a SPY level far above anything the
    # fetched window (or today's provisional close) contains -- the
    # scenario Case 2 describes: the lot's own entry peak has aged out of
    # the window, so the rolling max alone would sit below a level SPY is
    # known to have actually traded at.
    stamped_ath = bars[-1].adj_close + 1000.0

    signal = assemble_spy_signal(
        tiingo,
        trading_date="2025-09-02",
        iex_quote=iex,
        etoro_daily_candle=None,
        today=date(2025, 9, 2),
        open_lot_entry_spy_ath=[stamped_ath],
    )

    assert signal.spy_context.ath == stamped_ath
    assert signal.spy_context.tier > 0


def test_assemble_spy_signal_ath_floor_degrades_to_rolling_ath_with_no_open_lots():
    bars = _full_synthetic_window()
    tiingo = FakeTiingoClient(bars)
    iex = IexQuote(ticker="SPY", tngo_last=bars[-1].adj_close + 1.0)

    signal = assemble_spy_signal(
        tiingo,
        trading_date="2025-09-02",
        iex_quote=iex,
        etoro_daily_candle=None,
        today=date(2025, 9, 2),
        open_lot_entry_spy_ath=[],  # explicit empty -- no open lots to floor against
    )

    assert signal.spy_context.ath == iex.tngo_last
    assert signal.spy_context.tier == 0


def testspy_context_from_history_floor_arithmetic():
    from nightwatch.market.window import spy_context_from_history

    history = [_bar("2024-01-02", 100.0), _bar("2024-06-03", 90.0)]

    # No open lots: floor is just the rolling max including the
    # provisional close (zero open lots degrades cleanly) -- but the caller
    # must say so explicitly (see testspy_context_from_history_
    # requires_explicit_open_lot_ath below); there is no default.
    ctx = spy_context_from_history(history, 95.0, open_lot_entry_spy_ath=[])
    assert ctx.ath == 100.0
    assert ctx.drawdown == pytest.approx((95.0 - 100.0) / 100.0)

    # One open lot stamped above the rolling max: the floor wins.
    ctx2 = spy_context_from_history(history, 95.0, open_lot_entry_spy_ath=[150.0])
    assert ctx2.ath == 150.0
    assert ctx2.drawdown == pytest.approx((95.0 - 150.0) / 150.0)

    # Multiple open lots: the floor is the max across all of them and the
    # rolling ATH -- a lot stamped BELOW the rolling ATH must not pull it down.
    ctx3 = spy_context_from_history(history, 95.0, open_lot_entry_spy_ath=[80.0, 120.0])
    assert ctx3.ath == 120.0


def testspy_context_from_history_raises_on_bad_open_lot_ath():
    from nightwatch.market.window import spy_context_from_history

    history = [_bar("2024-01-02", 100.0)]
    with pytest.raises(WindowAssemblyError):
        spy_context_from_history(history, 95.0, open_lot_entry_spy_ath=[float("nan")])
    with pytest.raises(WindowAssemblyError):
        spy_context_from_history(history, 95.0, open_lot_entry_spy_ath=[-10.0])


# ---------------------------------------------------------------------------
# FIX 3 (HANDOFF_2026-09-05_evening.md §4): raw close for ATH/tier, never
# adjClose
# ---------------------------------------------------------------------------


def test_spy_context_uses_raw_close_not_adj_close_for_ath():
    from nightwatch.market.window import spy_context_from_history

    # A bar whose adjClose is materially lower than its raw close, as a
    # real post-ex-dividend-adjustment SPY bar would be (FINDINGS_window_
    # live_2026-09-05.md §3: ~0.95% apart on the oldest bar of a real
    # payload). ATH/drawdown/tier must track the raw value.
    history = [_bar("2024-01-02", adj_close=90.0, close=100.0)]
    ctx = spy_context_from_history(history, 100.0, open_lot_entry_spy_ath=[])
    assert ctx.ath == 100.0  # raw close, not the lower adjClose (90.0)
    assert ctx.drawdown == 0.0


# ---------------------------------------------------------------------------
# The "silently inert" defect (task brief, 2026-09-05): open_lot_entry_spy_ath
# used to default to () in both spy_context_from_history and
# assemble_spy_signal, so a caller could omit it and get a plain
# rolling-window ATH with no indication the floor was never engaged. No
# production caller (the session engine is still unbuilt) ever passed a
# non-empty value, so the floor was tested and looked done but was never
# live. Fixed by removing the default entirely -- omitting the argument is
# now a TypeError, not a silent no-op. These two tests are the enforcement:
# if a future edit reintroduces a default, they fail.
# ---------------------------------------------------------------------------


def testspy_context_from_history_requires_explicit_open_lot_ath():
    from nightwatch.market.window import spy_context_from_history

    history = [_bar("2024-01-02", 100.0)]
    with pytest.raises(TypeError):
        spy_context_from_history(history, 95.0)  # type: ignore[call-arg]


def test_assemble_spy_signal_requires_explicit_open_lot_ath():
    bars = _full_synthetic_window()
    tiingo = FakeTiingoClient(bars)
    iex = IexQuote(ticker="SPY", tngo_last=bars[-1].adj_close + 1.0)
    with pytest.raises(TypeError):
        assemble_spy_signal(  # type: ignore[call-arg]
            tiingo,
            trading_date="2025-09-02",
            iex_quote=iex,
            etoro_daily_candle=None,
            today=date(2025, 9, 2),
        )
