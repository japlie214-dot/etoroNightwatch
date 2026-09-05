"""Tests for nightwatch.market.source (DESIGN_market_data.md §2.7, §3.1, §9).

Entirely offline: every test drives ``TiingoClient`` through an injected
``httpx.MockTransport``/scripted transport, so no test here ever makes a
real network call. Field shapes below (EOD, IEX, error bodies) mirror what
was actually observed from a live Tiingo call on 2026-09-05 -- see the
task's final report for the raw responses -- not guessed from documentation.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from nightwatch.config import TiingoConfig
from nightwatch.market.source import (
    TiingoBudgetExceededError,
    TiingoClient,
    TiingoNetworkError,
    TiingoRateLimitError,
    TiingoServerError,
    TiingoTimeoutError,
    TiingoValidationError,
)

TEST_KEY = "test_tiingo_key_1234567890abcdef1234"


def _config(**overrides: Any) -> TiingoConfig:
    return TiingoConfig(secrets={"TIINGO_KEY": TEST_KEY}, **overrides)


class ScriptedTransport:
    """FIFO queue of canned responses (or exception-raising callables)."""

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


def _no_sleep(_seconds: float) -> None:
    pass


class Counter:
    def __init__(self, start: float = 0.0, step: float = 1.0) -> None:
        self.value = start
        self.step = step

    def __call__(self) -> float:
        v = self.value
        self.value += self.step
        return v


def _client(transport: ScriptedTransport, **kwargs: Any) -> TiingoClient:
    http = httpx.Client(
        base_url="https://api.tiingo.com",
        transport=httpx.MockTransport(transport.handler),
    )
    return TiingoClient(
        _config(),
        client=http,
        sleep=_no_sleep,
        clock=Counter(),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Auth: header, never a query parameter; key never leaks into an exception
# ---------------------------------------------------------------------------


def test_api_key_sent_as_authorization_header_not_query_param() -> None:
    transport = ScriptedTransport(
        [httpx.Response(200, json=[{"date": "2026-08-25", "close": 1.0, "adjClose": 1.0}])]
    )
    client = _client(transport)
    client.get_eod_history("SPY")
    request = transport.requests[0]
    assert request.headers["Authorization"] == f"Token {TEST_KEY}"
    assert TEST_KEY not in str(request.url)


def test_key_never_appears_in_a_raised_exception_message() -> None:
    """A 403 body containing nothing key-shaped is fine either way, but the
    exception constructor must redact regardless -- confirm the mechanism is
    wired, not merely that this one fixture happens not to trigger it."""
    transport = ScriptedTransport(
        [httpx.Response(403, json={"detail": f"Invalid token: {TEST_KEY}"})]
    )
    client = _client(transport)
    with pytest.raises(TiingoValidationError) as exc_info:
        client.get_eod_history("SPY")
    assert TEST_KEY not in str(exc_info.value)


# ---------------------------------------------------------------------------
# EOD history -- confirmed live field shape, 2026-09-05
# ---------------------------------------------------------------------------


def test_get_eod_history_parses_confirmed_live_field_shape() -> None:
    transport = ScriptedTransport(
        [
            httpx.Response(
                200,
                json=[
                    {
                        "date": "2026-08-25T00:00:00.000Z",
                        "close": 765.91,
                        "high": 766.78,
                        "low": 763.0501,
                        "open": 766.16,
                        "volume": 27422263,
                        "adjClose": 765.91,
                        "adjHigh": 766.78,
                        "adjLow": 763.0501,
                        "adjOpen": 766.16,
                        "adjVolume": 27422263,
                        "divCash": 0.0,
                        "splitFactor": 1.0,
                    }
                ],
            )
        ]
    )
    client = _client(transport)
    bars = client.get_eod_history("SPY", start_date="2026-08-25", end_date="2026-09-04")
    assert len(bars) == 1
    bar = bars[0]
    assert bar.date == "2026-08-25"
    assert bar.close == 765.91
    assert bar.adj_close == 765.91
    assert bar.split_factor == 1.0
    assert bar.div_cash == 0.0
    request = transport.requests[0]
    assert request.url.path == "/tiingo/daily/SPY/prices"
    assert "startDate=2026-08-25" in str(request.url)
    assert "endDate=2026-09-04" in str(request.url)


def test_get_eod_history_applies_split_factor_and_div_cash_when_present() -> None:
    transport = ScriptedTransport(
        [
            httpx.Response(
                200,
                json=[
                    {
                        "date": "2024-10-11",
                        "close": 26.29,
                        "adjClose": 26.29,
                        "splitFactor": 0.25,
                        "divCash": 0.15,
                    }
                ],
            )
        ]
    )
    client = _client(transport)
    bars = client.get_eod_history("SCHG")
    assert bars[0].split_factor == 0.25
    assert bars[0].div_cash == 0.15


def test_bad_ticker_is_a_permanent_validation_error_not_retried() -> None:
    """Confirmed live 2026-09-05: a bad ticker returns 404 with
    ``{"detail": "Error: Ticker '...' not found"}``."""
    transport = ScriptedTransport(
        [httpx.Response(404, json={"detail": "Error: Ticker 'NOTATICKERXYZ' not found"})]
    )
    client = _client(transport)
    with pytest.raises(TiingoValidationError) as exc_info:
        client.get_eod_history("NOTATICKERXYZ")
    assert exc_info.value.status_code == 404
    assert len(transport.requests) == 1  # never retried


def test_bad_key_is_a_permanent_validation_error() -> None:
    """Confirmed live 2026-09-05: an invalid key returns 403 with
    ``{"detail": "Invalid token."}``."""
    transport = ScriptedTransport([httpx.Response(403, json={"detail": "Invalid token."})])
    client = _client(transport)
    with pytest.raises(TiingoValidationError) as exc_info:
        client.get_eod_history("SPY")
    assert exc_info.value.status_code == 403
    assert len(transport.requests) == 1


# ---------------------------------------------------------------------------
# IEX -- confirmed live field shape and null-timestamp caveat, 2026-09-05
# ---------------------------------------------------------------------------


def test_get_iex_quotes_batches_multiple_tickers_in_one_call() -> None:
    """Confirmed live: SPY,UPRO,SCHG in one ``/iex/?tickers=...`` call
    returns all three rows -- this is why the session budgets one IEX call,
    not three."""
    transport = ScriptedTransport(
        [
            httpx.Response(
                200,
                json=[
                    {
                        "ticker": "SPY",
                        "timestamp": "2026-09-04T20:00:00+00:00",
                        "lastSaleTimestamp": None,
                        "quoteTimestamp": None,
                        "tngoLast": 770.19,
                        "prevClose": 773.17,
                    },
                    {
                        "ticker": "UPRO",
                        "timestamp": "2026-09-04T20:00:00+00:00",
                        "lastSaleTimestamp": None,
                        "quoteTimestamp": None,
                        "tngoLast": 151.88,
                        "prevClose": 153.73,
                    },
                    {
                        "ticker": "SCHG",
                        "timestamp": "2026-09-04T20:00:00+00:00",
                        "lastSaleTimestamp": None,
                        "quoteTimestamp": None,
                        "tngoLast": 35.53,
                        "prevClose": 35.84,
                    },
                ],
            )
        ]
    )
    client = _client(transport)
    quotes = client.get_iex_quotes(["SPY", "UPRO", "SCHG"])
    assert len(transport.requests) == 1
    assert set(quotes) == {"SPY", "UPRO", "SCHG"}
    assert quotes["UPRO"].tngo_last == 151.88
    assert quotes["UPRO"].prev_close == 153.73
    # The caveat this whole module exists to enforce: null on the free tier.
    assert quotes["UPRO"].last_sale_timestamp is None
    assert quotes["UPRO"].quote_timestamp is None


def test_get_iex_quote_missing_ticker_is_absent_not_zero() -> None:
    """A ticker with no current quote must be a missing key, never a
    fabricated 0.0 price (DESIGN_market_data.md §3.3: no data means no
    trade)."""
    transport = ScriptedTransport(
        [httpx.Response(200, json=[{"ticker": "SPY", "tngoLast": 770.19}])]
    )
    client = _client(transport)
    quotes = client.get_iex_quotes(["SPY", "UPRO"])
    assert "SPY" in quotes
    assert "UPRO" not in quotes


# ---------------------------------------------------------------------------
# Retry/backoff shape (ported from EtoroClient, DESIGN_market_data.md §13)
# ---------------------------------------------------------------------------


def test_network_error_retries_then_succeeds() -> None:
    attempts = {"n": 0}

    def flaky(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json=[{"ticker": "SPY", "tngoLast": 770.19}])

    transport = ScriptedTransport([flaky, flaky, flaky])
    client = _client(transport)
    quotes = client.get_iex_quotes(["SPY"])
    assert quotes["SPY"].tngo_last == 770.19
    assert attempts["n"] == 3


def test_network_error_exhausting_retries_raises_network_error() -> None:
    def always_fails(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    transport = ScriptedTransport([always_fails] * 5)
    client = _client(transport, max_attempts=5)
    with pytest.raises(TiingoNetworkError):
        client.get_iex_quotes(["SPY"])
    assert len(transport.requests) == 5


def test_timeout_is_retried_then_raises_tiingo_timeout_error() -> None:
    def times_out(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    transport = ScriptedTransport([times_out] * 5)
    client = _client(transport, max_attempts=5)
    with pytest.raises(TiingoTimeoutError):
        client.get_iex_quotes(["SPY"])


def test_5xx_is_retried_then_raises_server_error() -> None:
    transport = ScriptedTransport(
        [httpx.Response(503, text="upstream unavailable")] * 5
    )
    client = _client(transport, max_attempts=5)
    with pytest.raises(TiingoServerError):
        client.get_iex_quotes(["SPY"])
    assert len(transport.requests) == 5


def test_429_honours_retry_after_header_then_raises_rate_limit_error() -> None:
    sleeps: list[float] = []
    transport = ScriptedTransport(
        [httpx.Response(429, headers={"Retry-After": "7"}, text="slow down")] * 5
    )
    http = httpx.Client(
        base_url="https://api.tiingo.com", transport=httpx.MockTransport(transport.handler)
    )
    client = TiingoClient(
        _config(), client=http, sleep=sleeps.append, clock=Counter(), max_attempts=5
    )
    with pytest.raises(TiingoRateLimitError) as exc_info:
        client.get_iex_quotes(["SPY"])
    assert exc_info.value.retry_after == 7.0
    assert sleeps == [7.0, 7.0, 7.0, 7.0]


# ---------------------------------------------------------------------------
# Local self-throttle (§2.7: Tiingo supplies no rate-limit header at all)
# ---------------------------------------------------------------------------


def test_local_hourly_budget_refuses_before_any_http_call() -> None:
    transport = ScriptedTransport([])  # must never be reached
    client = _client(transport, hourly_budget=1)
    ok_response = httpx.Response(200, json=[{"ticker": "SPY", "tngoLast": 1.0}])
    transport.responses.append(ok_response)
    client.get_iex_quotes(["SPY"])  # consumes the single hourly token
    with pytest.raises(TiingoBudgetExceededError):
        client.get_iex_quotes(["SPY"])
    assert len(transport.requests) == 1


def test_local_daily_budget_refuses_before_any_http_call() -> None:
    transport = ScriptedTransport(
        [httpx.Response(200, json=[{"ticker": "SPY", "tngoLast": 1.0}])]
    )
    client = _client(transport, hourly_budget=100, daily_budget=1)
    client.get_iex_quotes(["SPY"])
    with pytest.raises(TiingoBudgetExceededError):
        client.get_iex_quotes(["SPY"])
    assert len(transport.requests) == 1
