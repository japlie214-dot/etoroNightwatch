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


# A real ~14-month historical span with >=265 XNYS sessions, entirely in
# the past so it never depends on "today".
_WINDOW_START = "2024-06-01"
_WINDOW_END = "2025-08-29"


def _full_synthetic_window() -> list[EodBar]:
    sessions = _real_session_window(_WINDOW_START, _WINDOW_END)
    assert len(sessions) >= REQUIRED_HISTORY_BARS  # fixture sanity
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
    )
    assert signal.provisional_close.source == "etoro_candle"
    assert signal.provisional_close.value == candle.close
