"""The Tiingo client (DESIGN_market_data.md §2.7, §3.1, §9). Build step 2.

Two capabilities, both confirmed against the live API on 2026-09-05 (US
market closed, weekend -- IEX therefore serves Friday's last session, not a
live tick; this is expected, not a bug, and every field name below was
verified against that real response, not guessed from Tiingo's docs):

* **Daily EOD history** -- ``GET /tiingo/daily/{ticker}/prices``. Returns
  ``close``, ``adjClose``, ``splitFactor``, ``divCash`` per bar, confirmed
  free-tier for SPY/UPRO/SCHG. ``adjClose`` is the series the strategy uses;
  the other three exist so a future build step can detect corporate actions
  without re-deriving them from price ratios (DESIGN_market_data.md §2.2).
* **Live/last tick** -- ``GET /iex/`` (batched, comma-separated tickers).
  ``tngoLast`` is the usable field.

**Critical caveat, load-bearing for preflight gate 10
(DESIGN_market_data.md §5.1, §10.2): do not design around a timestamp.** A
live probe against all three tickers on 2026-09-05 confirmed the free tier's
``timestamp`` is response-generation time, not a trade or quote time, and
both ``lastSaleTimestamp`` and ``quoteTimestamp`` are ``null`` on every
symbol. There is no independent freshness signal on this tier -- only the
price value itself. :class:`IexQuote` keeps ``timestamp`` for audit/debug
logging only; nothing in this module or in ``engine.preflight`` may compare
it against a clock. Freshness is established elsewhere, by comparing this
value against eToro's own candles (DESIGN_market_data.md §5, §6 gate 10).

**Auth.** The key comes from ``config/.secrets.env`` as ``TIINGO_KEY``,
loaded the same way every other secret in this project is (DESIGN
``config.py``'s ``read_dotenv`` + ``TiingoConfig.secrets``). It is sent as
an ``Authorization: Token <key>`` header -- confirmed working live -- never
a URL query parameter, so it cannot leak through a server access log or a
copied URL. The key value is never logged, printed, or embedded in an
exception message: every raised error here is redacted the same way
``etoro.errors`` redacts eToro exceptions (:class:`TiingoError`).

**Retry/backoff.** Ported from :class:`nightwatch.etoro.client.EtoroClient`'s
two-tier shape (itself carried from ``TelegramNotifier``, DESIGN_market_data
§13: "port the shape, drop the yfinance-specific baggage"): a network error
or 5xx retries with bounded exponential backoff; 429 retries honouring
``Retry-After`` if present; any other 4xx (bad ticker, bad/expired key,
malformed params -- all confirmed live, see the client docstrings below) is
permanent and raised immediately, never retried into a tighter hole.

**No on-disk cache.** This module holds no state across calls beyond an
in-memory self-throttle counter (§2.7: Tiingo returns no rate-limit headers
at all, so the budget cannot be read back from the API -- confirmed live,
2026-09-05, a normal 200 response carried no ``X-RateLimit-*`` header of any
kind). Nothing here writes a price to disk; DESIGN_market_data.md §0/§3
forbid accumulating a price store, and every call here is a fresh HTTP GET.

**Every network call carries an explicit per-call timeout.** DESIGN_market_data
§8 records a probe that hung for exactly the opposite mistake. The timeout is
passed on every individual request, not only configured once on the
``httpx.Client``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Final, Mapping, Sequence

import httpx

from ..config import TiingoConfig
from ..etoro.ratelimit import TokenBucket
from ..logging_setup import RedactionFilter

__all__ = [
    "TiingoError",
    "TiingoNetworkError",
    "TiingoTimeoutError",
    "TiingoServerError",
    "TiingoRateLimitError",
    "TiingoValidationError",
    "TiingoBudgetExceededError",
    "EodBar",
    "IexQuote",
    "HOURLY_CALL_BUDGET",
    "DAILY_CALL_BUDGET",
    "TiingoClient",
]

_logger = logging.getLogger("nightwatch.market.source")

_DEFAULT_MAX_ATTEMPTS: Final[int] = 5
_DEFAULT_BASE_DELAY_SECONDS: Final[float] = 1.0
_DEFAULT_MAX_DELAY_SECONDS: Final[float] = 20.0

#: Tiingo's documented free-tier caps (DESIGN_market_data.md §2.7). The API
#: itself reports no remaining-quota header on any response (confirmed live,
#: 2026-09-05: a normal 200 carried none), so this budget is tracked locally,
#: never read back from the server.
HOURLY_CALL_BUDGET: Final[int] = 50
DAILY_CALL_BUDGET: Final[int] = 1000


def _safe(message: str) -> str:
    """Redact secret-shaped substrings, mirroring ``etoro.errors._safe``."""
    return RedactionFilter.redact(message)


class TiingoError(Exception):
    """Base class for every error this client raises."""

    def __init__(self, message: str, *args: Any) -> None:
        super().__init__(_safe(str(message)), *args)


class TiingoNetworkError(TiingoError):
    """DNS/TCP/TLS failure or a request-level connection error. Retried."""


class TiingoTimeoutError(TiingoNetworkError):
    """A request exceeded its explicit per-call timeout. Retried."""


class TiingoServerError(TiingoError):
    """A 5xx response. Retried with bounded exponential backoff."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class TiingoRateLimitError(TiingoError):
    """HTTP 429. Retried, honouring ``Retry-After`` when present."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TiingoValidationError(TiingoError):
    """Any other 4xx -- bad ticker (404, confirmed live), bad/expired key (403,
    confirmed live), malformed params. Never retried: retrying a request that
    cannot succeed unchanged only burns budget on a call that will fail again.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class TiingoBudgetExceededError(TiingoError):
    """The local self-throttle would exceed Tiingo's documented free-tier
    budget (§2.7). Raised *before* any HTTP call is attempted -- this is a
    local guard against our own bug burning the day's quota, not something
    the server ever tells us about (no rate-limit header exists to react
    to, confirmed live).
    """


@dataclass(frozen=True, slots=True)
class EodBar:
    """One daily EOD bar (DESIGN_market_data.md §2.7).

    ``adj_close`` is the series the strategy consumes. ``close``,
    ``split_factor`` and ``div_cash`` exist so a future corporate-actions
    detector (DESIGN_market_data.md §2.2, §7 ``corporate_actions`` table) can
    tell a real split/dividend apart from ordinary price movement without
    re-deriving it from adjusted-vs-raw ratios.
    """

    date: str  # 'YYYY-MM-DD'
    close: float
    adj_close: float
    split_factor: float
    div_cash: float


@dataclass(frozen=True, slots=True)
class IexQuote:
    """One IEX last-tick quote (DESIGN_market_data.md §2.7, §5.1, §10.2).

    ``timestamp`` is response-generation time on the free tier, confirmed
    live 2026-09-05 -- NOT a trade or quote time. ``last_sale_timestamp`` and
    ``quote_timestamp`` were both ``null`` for every symbol in that same
    probe. **None of the three may be used as a freshness signal.** They are
    kept only for audit/debug logging. Freshness comes from comparing
    ``tngo_last`` against an independent source's *value*, never from an age
    computed off any field on this class (DESIGN_market_data.md §5.1: "the
    mechanism that worked is value comparison, not a second timestamp").
    """

    ticker: str
    tngo_last: float
    timestamp: str | None = None
    last_sale_timestamp: str | None = None
    quote_timestamp: str | None = None
    prev_close: float | None = None


class TiingoClient:
    """Tiingo HTTP client. Construction never touches the network (mirrors
    :class:`~nightwatch.etoro.client.EtoroClient` and
    :class:`~nightwatch.notify.telegram.TelegramNotifier`'s "safe to
    construct" pattern).
    """

    def __init__(
        self,
        config: TiingoConfig,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        base_delay_seconds: float = _DEFAULT_BASE_DELAY_SECONDS,
        max_delay_seconds: float = _DEFAULT_MAX_DELAY_SECONDS,
        hourly_budget: int = HOURLY_CALL_BUDGET,
        daily_budget: int = DAILY_CALL_BUDGET,
    ) -> None:
        self._config = config
        self._owns_client = client is None
        self._http = client or httpx.Client(
            base_url=config.base_url, timeout=config.timeout_seconds
        )
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._base_delay = base_delay_seconds
        self._max_delay = max_delay_seconds
        # Local self-throttle only -- Tiingo supplies no remaining-quota
        # header to reconcile against (confirmed live, §2.7). Session usage
        # is ~6 calls (3 EOD + 1 batched IEX, generously double-counted);
        # comfortably inside 50/hour, 1000/day even run several times an hour
        # during development.
        self._hourly_bucket = TokenBucket(hourly_budget, 3600.0, clock=clock)
        self._daily_bucket = TokenBucket(daily_budget, 86400.0, clock=clock)

    # -- lifecycle -------------------------------------------------------

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> "TiingoClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- internals ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        # Authorization header, never a query parameter (module docstring;
        # confirmed working live 2026-09-05).
        return {
            "Authorization": f"Token {self._config.api_key}",
            "Content-Type": "application/json",
        }

    def _backoff(self, attempt: int) -> float:
        return min(self._base_delay * (2 ** (attempt - 1)), self._max_delay)

    def _consume_budget(self) -> None:
        if not self._hourly_bucket.try_acquire():
            raise TiingoBudgetExceededError(
                "local hourly Tiingo call budget exhausted "
                f"({self._hourly_bucket.capacity}/hour) -- refusing to call; "
                "Tiingo itself supplies no remaining-quota header to check"
            )
        if not self._daily_bucket.try_acquire():
            raise TiingoBudgetExceededError(
                "local daily Tiingo call budget exhausted "
                f"({self._daily_bucket.capacity}/day) -- refusing to call"
            )

    def _request(self, path: str, params: Mapping[str, Any] | None) -> httpx.Response:
        """One GET, with rate limiting, retries and typed errors.

        Retry policy ported from :class:`~nightwatch.etoro.client.EtoroClient`
        (DESIGN_market_data.md §13): network error / 5xx retry with bounded
        backoff; 429 retries honouring ``Retry-After``; every other 4xx is
        raised immediately, never retried.
        """
        self._consume_budget()
        last_error: Exception | str = "unknown error"
        for attempt in range(1, self._max_attempts + 1):
            try:
                # Explicit per-call timeout, in addition to the client's own
                # configured default -- DESIGN_market_data.md §8's hung probe
                # is exactly the mistake this guards against.
                response = self._http.get(
                    path,
                    params=params,
                    headers=self._headers(),
                    timeout=self._config.timeout_seconds,
                )
            except httpx.TimeoutException as exc:
                last_error = exc
                if attempt == self._max_attempts:
                    raise TiingoTimeoutError(f"timed out calling {path}: {exc}") from exc
                self._sleep(self._backoff(attempt))
                continue
            except httpx.RequestError as exc:
                last_error = exc
                if attempt == self._max_attempts:
                    raise TiingoNetworkError(f"network error calling {path}: {exc}") from exc
                self._sleep(self._backoff(attempt))
                continue

            if response.status_code == 200:
                return response

            if response.status_code == 429:
                retry_after = self._retry_after(response)
                last_error = f"429 rate limited (retry_after={retry_after}s)"
                if attempt == self._max_attempts:
                    raise TiingoRateLimitError(str(last_error), retry_after=retry_after)
                self._sleep(retry_after if retry_after is not None else self._backoff(attempt))
                continue

            if 500 <= response.status_code < 600:
                last_error = f"{response.status_code} server error: {response.text}"
                if attempt == self._max_attempts:
                    raise TiingoServerError(str(last_error), status_code=response.status_code)
                self._sleep(self._backoff(attempt))
                continue

            # Any other 4xx -- confirmed live: 404 "Ticker '...' not found",
            # 403 "Invalid token." -- is permanent, never retried.
            raise TiingoValidationError(
                f"{response.status_code} from Tiingo {path}: {response.text}",
                status_code=response.status_code,
            )

        raise TiingoError(str(last_error))  # pragma: no cover - unreachable

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        header = response.headers.get("Retry-After")
        if header:
            try:
                return float(header)
            except ValueError:
                pass
        return None

    # -- public API ----------------------------------------------------------

    def get_eod_history(
        self,
        ticker: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[EodBar]:
        """``GET /tiingo/daily/{ticker}/prices`` -- confirmed live 2026-09-05.

        Returns bars in the order Tiingo supplies them (ascending by date in
        every observed response); this module does not re-sort, since
        DESIGN_market_data.md §0 forbids accumulating or otherwise treating
        this as a persisted series -- callers use it once, per session.
        """
        params: dict[str, Any] = {}
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date
        response = self._request(f"/tiingo/daily/{ticker}/prices", params or None)
        rows = response.json()
        return [
            EodBar(
                date=str(row["date"])[:10],
                close=float(row["close"]),
                adj_close=float(row["adjClose"]),
                split_factor=float(row.get("splitFactor", 1.0)),
                div_cash=float(row.get("divCash", 0.0)),
            )
            for row in rows
        ]

    def get_iex_quotes(self, tickers: Sequence[str]) -> dict[str, IexQuote]:
        """``GET /iex/?tickers=...`` -- one batched call for every symbol
        (confirmed live 2026-09-05: SPY,UPRO,SCHG in one request returned all
        three rows), rather than one call per symbol.

        A ticker Tiingo has no current quote for is simply absent from the
        result -- callers must treat a missing key as "no data", never as a
        zero price (DESIGN_market_data.md §3.3: no data means no trade).
        """
        response = self._request("/iex/", {"tickers": ",".join(tickers)})
        rows = response.json()
        result: dict[str, IexQuote] = {}
        for row in rows:
            symbol = str(row.get("ticker", "")).upper()
            tngo_last = row.get("tngoLast")
            if not symbol or tngo_last is None:
                continue
            result[symbol] = IexQuote(
                ticker=symbol,
                tngo_last=float(tngo_last),
                timestamp=row.get("timestamp"),
                last_sale_timestamp=row.get("lastSaleTimestamp"),
                quote_timestamp=row.get("quoteTimestamp"),
                prev_close=(
                    float(row["prevClose"]) if row.get("prevClose") is not None else None
                ),
            )
        return result

    def get_iex_quote(self, ticker: str) -> IexQuote | None:
        """Convenience single-symbol wrapper over :meth:`get_iex_quotes`."""
        return self.get_iex_quotes([ticker]).get(ticker.upper())
