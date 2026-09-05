"""The eToro HTTP client (DESIGN §1.1, §4.3, §5, §6). Built in step 5, against demo.

Structural safety, restated from DESIGN §6.A because it belongs in code, not
just in a comment: every order this client can possibly send carries
``leverage=1``, ``transaction="buy"``, ``settlementType="real"``. These are
module-level constants, never read from ``config.toml`` and never accepted as
call parameters with a different value — see :data:`MAX_LEVERAGE`,
:data:`TRANSACTION`, :data:`SETTLEMENT_TYPE`, and
:class:`~nightwatch.etoro.models.OrderRequest`, which independently refuses
to construct with anything else.

Environment/credential structural safety (DESIGN §1.1, §6.F): this client
takes a whole :class:`~nightwatch.config.EtoroConfig`, never a bare
``(base_url, api_key, user_key)`` tuple. The URL path segment
(``.../demo/...`` vs ``.../...``) and the credential pair are both derived,
inside :class:`EtoroConfig`, from the single ``environment`` value — so there
is no constructor parameter through which a caller could hand this client a
real-money URL and a demo key, or vice versa. :meth:`EtoroClient.__init__`
additionally asserts the derived credentials' label agrees with the
configured environment as a defense-in-depth belt-and-braces check (DESIGN
§7's "should be unreachable is not the same guarantee as is checked").
Preflight gate 7 goes further and probes the *live API* for the credential's
actual bound environment via :meth:`EtoroClient.verify_environment`, because
a config value can be wrong in a way no amount of local type-checking catches.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Final, Iterable, Mapping, Sequence

import httpx

from ..config import Credentials, EtoroConfig
from ..db.repo import Instrument, Repo
from . import auth
from .errors import (
    EtoroAuthError,
    EtoroEligibilityError,
    EtoroEnvironmentMismatchError,
    EtoroError,
    EtoroNetworkError,
    EtoroRateLimitError,
    EtoroServerError,
    EtoroTimeoutError,
    EtoroValidationError,
)
from .models import (
    Balance,
    Candle,
    CloseRequest,
    Eligibility,
    EligibilityBatchResult,
    InstrumentSearchResult,
    OrderRequest,
    OrderResult,
    OrderStatus,
    Position,
    Rate,
    TradeHistoryEntry,
    compute_available_cash,
)
from .ratelimit import RateLimiter, RequestClass

__all__ = [
    "MAX_LEVERAGE",
    "TRANSACTION",
    "SETTLEMENT_TYPE",
    "MAX_ELIGIBILITY_BATCH",
    "PollResult",
    "EtoroClient",
]

_logger = logging.getLogger("nightwatch.etoro.client")

# ---------------------------------------------------------------------------
# Structural safety constants (DESIGN §6.A). These are read by
# nightwatch.etoro.models.OrderRequest too; kept here as the module callers
# reach for, and re-exported, so there is exactly one place in the codebase
# that could ever be edited to change them -- and doing so still would not
# help, because every call site that builds an OrderRequest passes these
# constants explicitly rather than accepting a caller override.
# ---------------------------------------------------------------------------

#: Hardcoded. Never a config.toml value. UPRO's own 3x leverage is achieved by
#: buying the ETF unleveraged; our order's leverage is always exactly 1.
MAX_LEVERAGE: Final[int] = 1

#: Hardcoded. There is no code path in this client that can send "sellShort"
#: or "buyToCover" -- reducing a holding always closes a position instead.
TRANSACTION: Final[str] = "buy"

#: Hardcoded. Never "cfd", "marginTrade", or "realFutures".
SETTLEMENT_TYPE: Final[str] = "real"

#: DESIGN §1.1: eligibility is queried in batches of at most 100 instruments.
MAX_ELIGIBILITY_BATCH: Final[int] = 100

# REMOVED 2026-09-05 (was `MAX_TRADE_HISTORY_DAYS: Final[int] = 364`, plus a
# `get_trade_history` guard raising when a caller's window exceeded it).
# DESIGN §1.1 documented trade-history lookback as under one year; a live
# probe (DESIGN_market_data.md §10.5) disproved it -- a single GET with
# MinDate=1970-01-01 returned this real account's complete 120-row
# closed-trade history spanning 2020-12-21..2026-08-31 (nearly six years, no
# retention cutoff found). The constant and the guard built on it enforced a
# *false* client-side ceiling for a limit the live API does not have --
# keeping the constant around unused would be exactly the "dead complexity
# left behind without saying why" this correction exists to avoid. See
# get_trade_history's docstring for what replaced it (local end_date
# filtering, since MaxDate is separately confirmed ignored server-side).

_DEFAULT_MAX_ATTEMPTS: Final[int] = 5
_DEFAULT_BASE_DELAY: Final[float] = 1.0
_DEFAULT_MAX_DELAY: Final[float] = 20.0
_DEFAULT_POLL_INTERVAL: Final[float] = 2.0
_DEFAULT_POLL_TIMEOUT: Final[float] = 90.0


@dataclass(frozen=True, slots=True)
class PollResult:
    """The outcome of a bounded poll-to-terminal loop (DESIGN §1.1, §6.H).

    ``timed_out`` is True when the configured timeout was reached without
    the order settling into a terminal status -- ``order`` in that case still
    carries the last known (possibly WAITING_FOR_MARKET) status, and callers
    must **never** treat a timeout as a fill.
    """

    order: OrderResult
    attempts: int
    timed_out: bool


class EtoroClient:
    """The eToro HTTP client wrapping every endpoint DESIGN §1.1/§4.3 uses.

    Construction never performs any HTTP call itself (mirrors
    :class:`~nightwatch.notify.telegram.TelegramNotifier`'s pattern of "safe
    to construct, calls only happen in methods").
    """

    def __init__(
        self,
        config: EtoroConfig,
        *,
        role: str = "write",
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
        repo: Repo | None = None,
        rate_limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL,
        poll_timeout_seconds: float = _DEFAULT_POLL_TIMEOUT,
    ) -> None:
        if role not in ("write", "read"):
            raise ValueError(f"role must be 'write' or 'read', got {role!r}")
        self._config = config
        self._role = role
        # Structural safety: the ONLY way this object ever gets credentials is
        # by asking `config` for them, which derives the pair from
        # `config.environment`. There is no constructor parameter through
        # which a caller could inject a mismatched pair.
        self._credentials: Credentials = (
            config.write_credentials if role == "write" else config.read_credentials
        )
        expected_prefix = f"{config.environment.value}:"
        if not self._credentials.label.startswith(expected_prefix):
            # Should be unreachable given EtoroConfig's own invariants -- kept
            # anyway per DESIGN §6.A's "checked anyway" principle.
            raise EtoroEnvironmentMismatchError(
                configured=config.environment.value,
                detected=self._credentials.label,
            )

        self._owns_client = client is None
        self._http = client or httpx.Client(
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            transport=transport,
        )
        self._repo = repo
        self._limiter = rate_limiter or RateLimiter()
        self._sleep = sleep
        self._clock = clock
        self._max_attempts = max_attempts
        self._poll_interval = poll_interval_seconds
        self._poll_timeout = poll_timeout_seconds
        #: The most recent response's `Date` header, for preflight gate 4
        #: (clock skew > 60s aborts). `None` until at least one call is made.
        self.last_server_date: str | None = None

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> "EtoroClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def environment(self) -> str:
        return self._config.environment.value

    @property
    def is_demo(self) -> bool:
        return self._config.environment.value == "demo"

    # -- core transport --------------------------------------------------------

    def _wait_for_capacity(self, request_class: RequestClass) -> None:
        deadline = self._clock() + 120.0  # generous local safety valve
        while not self._limiter.try_acquire(request_class):
            wait = min(self._limiter.time_until_available(request_class), 5.0)
            if self._clock() + wait > deadline:
                # Do not hang forever locally; let the request proceed and
                # rely on the server's own 429 + Retry-After as the backstop.
                break
            self._sleep(max(wait, 0.01))

    def _request(
        self,
        method: str,
        path: str,
        *,
        request_class: RequestClass,
        params: Mapping[str, Any] | None = None,
        json_body: Any | None = None,
        request_id: str | None = None,
    ) -> httpx.Response:
        """Send one request with rate limiting, retries, and typed errors.

        Retry policy (DESIGN's stated policy, carried over from
        TelegramNotifier's proven shape): network errors and 5xx retry with
        bounded exponential backoff; 429 retries honouring `Retry-After`;
        every other 4xx is raised immediately, never retried.
        """
        last_error: Exception | str = "unknown error"
        for attempt in range(1, self._max_attempts + 1):
            self._wait_for_capacity(request_class)
            headers = auth.build_headers(self._credentials, request_id)
            try:
                response = self._http.request(
                    method, path, params=params, json=json_body, headers=headers
                )
            except httpx.TimeoutException as exc:
                last_error = exc
                if attempt == self._max_attempts:
                    raise EtoroTimeoutError(f"timed out calling {path}: {exc}") from exc
                self._sleep(self._backoff(attempt))
                continue
            except httpx.RequestError as exc:
                last_error = exc
                if attempt == self._max_attempts:
                    raise EtoroNetworkError(f"network error calling {path}: {exc}") from exc
                self._sleep(self._backoff(attempt))
                continue

            self.last_server_date = response.headers.get("Date", self.last_server_date)
            self._limiter.reconcile(request_class, response.headers)

            if response.status_code < 300:
                return response

            if response.status_code in (401, 403):
                raise EtoroAuthError(
                    f"{response.status_code} from {path}: {self._safe_body(response)}"
                )

            if response.status_code == 429:
                retry_after = RateLimiter.retry_after_from_headers(response.headers) or 1.0
                last_error = f"429 rate limited (retry_after={retry_after}s)"
                if attempt == self._max_attempts:
                    raise EtoroRateLimitError(str(last_error), retry_after=retry_after)
                self._sleep(retry_after)
                continue

            if 500 <= response.status_code < 600:
                last_error = f"{response.status_code} server error: {self._safe_body(response)}"
                if attempt == self._max_attempts:
                    raise EtoroServerError(str(last_error), status_code=response.status_code)
                self._sleep(self._backoff(attempt))
                continue

            # Any other 4xx: permanent, never retried.
            raise EtoroValidationError(
                f"{response.status_code} from {path}: {self._safe_body(response)}",
                status_code=response.status_code,
            )

        # Unreachable in practice (every branch above either returns or
        # raises on the final attempt), but keeps type-checkers honest.
        raise EtoroError(str(last_error))

    @staticmethod
    def _safe_body(response: httpx.Response) -> str:
        try:
            return response.text[:500]
        except Exception:  # pragma: no cover - defensive
            return "<unreadable body>"

    def _backoff(self, attempt: int) -> float:
        return min(_DEFAULT_BASE_DELAY * (2 ** (attempt - 1)), _DEFAULT_MAX_DELAY)

    def _demo(self) -> str:
        """The URL path segment for the *current* environment (DESIGN §1.1).

        Never call this in place of ``config.path_segment`` from a place that
        doesn't already hold this client's own config -- kept as a thin
        wrapper purely so every path-building call site below reads
        identically.
        """
        return self._config.path_segment

    # -- gate 6/9: auth probe + balances --------------------------------------

    def get_balance(self) -> Balance:
        """Compute available cash from the **portfolio** endpoint, not
        ``/balances`` (DESIGN §4.5, §9; corrected 2026-09-05 against a
        real-money live-account probe).

        Corrected 2026-09-05, SEVERE: this method used to call
        ``GET /api/v1/balances`` and read ``credit``/``orders``/
        ``ordersForOpen`` off its body. A read-only probe against this
        operator's real account today found that ``/balances`` carries
        **none of those fields** -- its body is just ``{"totalBalance":
        ..., "balances": [...]}`` (a per-sub-account list, no ``credit``, no
        pending-order arrays). The old code silently computed
        ``available_cash = compute_available_cash(0.0, [], []) == 0.0`` with
        no error, because ``data.get("credit", 0.0)`` treats "field absent"
        identically to "field present and zero." Since ``broker_available_cash``
        feeds ``sleeve_cash`` (DESIGN §4.5), this meant the app would compute
        a permanently-zero cash sleeve and never buy -- fails closed, so not
        a money-losing bug, but a silent one with no diagnostic pointing at
        the cause.

        The formula's real inputs -- ``credit``, ``orders``, ``ordersForOpen``
        -- all live in the **portfolio** endpoint's ``clientPortfolio``
        envelope instead (confirmed live today: ``credit=1514.59``,
        ``orders`` one row amount ``250.00``, ``ordersForOpen=[]``, giving
        ``available = 1514.59 - 250.00 = 1264.59``). That is the same
        envelope :meth:`get_portfolio` already unwraps for ``positions``, so
        this method now shares :meth:`_fetch_portfolio_envelope` with it
        instead of calling a second, separate endpoint -- one round trip,
        not two, and both callers read the one response body eToro actually
        sends. ``/balances`` is no longer called by this method at all (it
        is still called by :meth:`verify_environment`, an unrelated gate 7
        probe that only wants a best-effort environment signal, not the
        cash formula's inputs).

        A missing ``credit`` key is now treated as a broker-contract change
        and raises :class:`~nightwatch.etoro.errors.EtoroValidationError`
        immediately, rather than silently defaulting to ``0.0`` -- this
        codebase's repeated lesson (§10.7, HANDOFF §8) is that a silent zero
        is worse than a crash.

        ``total_balance`` is ``None`` here: the portfolio envelope's
        confirmed live shape (DESIGN_market_data.md §10.8) does not carry a
        ``totalBalance``-equivalent field, and nothing in this codebase
        currently reads :attr:`Balance.total_balance` (grepped 2026-09-05) --
        so there is no live consumer to justify a second round trip to
        ``/balances`` just to populate a field nobody uses. If a future
        caller needs portfolio-value display, fetch ``/balances`` there
        explicitly rather than reintroducing it into this hot path.
        """
        data = self._fetch_portfolio_envelope()
        if "credit" not in data:
            raise EtoroValidationError(
                "portfolio response missing 'credit' -- cannot compute "
                "available_cash (broker contract change, not a value to "
                f"silently default to 0; body keys seen: {sorted(data.keys())!r})"
            )
        credit = float(data["credit"])
        available = compute_available_cash(
            credit,
            orders_for_open=data.get("ordersForOpen", []),
            orders=data.get("orders", []),
        )
        return Balance(
            credit=credit,
            total_balance=None,
            available_cash=available,
            raw=data,
        )

    def verify_environment(self) -> None:
        """Probe that the credential's bound environment matches config.

        DESIGN §5 gate 7 / §6.F: this must be *detected*, not assumed. A
        config value alone cannot prove which environment a key is bound to
        -- eToro binds a key to an environment at creation time and does not
        expose that as a documented, stable response field, so this method
        treats a handful of plausible signals as a *positive* mismatch
        indicator without treating their absence as proof of a match:

        * an explicit boolean/string field in the balances payload (several
          plausible names are checked, since the exact shape has not yet
          been observed against a live key -- see the module docstring on
          `EtoroClient` and the HANDOFF note this ships with);
        * a real-money-shaped response (e.g. a non-trivial `totalBalance`)
          returned while `config.environment` is `demo`, which is the one
          asymmetric signal that's fairly safe to trust: eToro demo accounts
          are seeded with a fixed, well-known balance, not zero and not
          identical to a funded real account by chance.

        Raises :class:`EtoroEnvironmentMismatchError` on a detected mismatch.
        Raises nothing (returns normally) when no positive signal is found --
        that is "unable to confirm," not "confirmed OK," and callers relying
        on this for gate 7 should treat it only as an additional check
        layered on top of `get_balance()` succeeding at all (a real key
        pointed at a demo-only endpoint, or vice versa, still fails outright
        with 401/403 in the common case, which `get_balance` already turns
        into `EtoroAuthError`).
        """
        response = self._request(
            "GET", "/api/v1/balances", request_class="read"
        )
        data = response.json()
        detected = self._detect_environment(data)
        if detected is not None and detected != self._config.environment.value:
            raise EtoroEnvironmentMismatchError(
                configured=self._config.environment.value, detected=detected
            )

    @staticmethod
    def _detect_environment(payload: Mapping[str, Any]) -> str | None:
        """Best-effort extraction of an environment signal from a balances body.

        Returns ``"demo"``, ``"real"``, or ``None`` if no signal is present.
        Kept as a static, independently-testable function so its heuristic
        can be exercised with a hand-built payload without any HTTP call.
        """
        for key in ("isDemo", "is_demo", "demo"):
            if key in payload:
                value = payload[key]
                if isinstance(value, bool):
                    return "demo" if value else "real"
        for key in ("accountType", "account_type", "environment"):
            if key in payload:
                value = str(payload[key]).strip().lower()
                if value in ("demo", "virtual", "practice"):
                    return "demo"
                if value in ("real", "live"):
                    return "real"
        return None

    # -- gate 8, portfolio, pnl ------------------------------------------------

    def _fetch_portfolio_envelope(self) -> dict[str, Any]:
        """One ``GET`` to ``.../portfolio``, unwrapped from ``clientPortfolio``.

        Shared by :meth:`get_portfolio` and :meth:`get_balance` (added
        2026-09-05): both exist to read fields off the exact same live
        response body -- ``positions`` for the former, ``credit``/
        ``orders``/``ordersForOpen`` for the latter (see `get_balance`'s
        docstring for why those last three do **not** live on ``/balances``
        as previously assumed) -- so there is exactly one place that issues
        the request and unwraps the envelope, not two call sites that could
        drift apart on the unwrap logic.

        Corrected 2026-09-04 (carried over from the old `get_portfolio`): a
        live response nests everything under a ``clientPortfolio`` object --
        ``{"clientPortfolio": {"positions": [...], "credit": ..., ...}}`` --
        not bare top-level keys.
        """
        response = self._request(
            "GET", f"/api/v1/trading/info/{self._demo()}portfolio", request_class="read"
        )
        data = response.json()
        if isinstance(data, dict) and "clientPortfolio" in data:
            data = data["clientPortfolio"]
        return data if isinstance(data, dict) else {}

    def get_portfolio(self) -> list[Position]:
        """``GET /trading/info/portfolio`` (or the `/demo/` variant).

        Corrected 2026-09-04: a live response (0 positions on this demo
        account) nests everything under a ``clientPortfolio`` object --
        ``{"clientPortfolio": {"positions": [...], ...}}`` -- not a bare
        top-level ``positions`` key. With zero positions this bug was
        invisible (both the old and the fixed parsing return ``[]``), which
        is exactly the "didn't error, not a pass" trap; this has since been
        confirmed against a genuinely non-empty portfolio (§10.8).
        """
        data = self._fetch_portfolio_envelope()
        positions_raw = data.get("positions", [])
        return [self._parse_position(p) for p in positions_raw]

    def get_pnl(self) -> dict[str, Any]:
        """``GET /trading/info/real/pnl`` (or the `/demo/` variant)."""
        segment = "demo" if self.is_demo else "real"
        response = self._request(
            "GET", f"/api/v1/trading/info/{segment}/pnl", request_class="read"
        )
        return dict(response.json())

    @staticmethod
    def _parse_position(raw: Mapping[str, Any]) -> Position:
        """Parse one broker position (DESIGN_market_data.md §4.2, §4.5, §10.7).

        Bug found 2026-09-04, corrected 2026-09-05 (HANDOFF_2026-09-05.md §1,
        §4): the previous version read ``currentRate``, ``invested``,
        ``unrealizedPL``, and ``symbol`` off every position -- a live,
        non-empty 227-position portfolio confirmed **none of those four keys
        exist** on a broker position. None are read here any
        more; see :class:`~nightwatch.etoro.models.Position`'s docstring for
        why symbol resolution and mark-to-market are the caller's job
        (through ``instrumentID`` and a fetched price, respectively) rather
        than this parser's.

        ``positionID``, ``units``, ``openRate``, and ``amount`` are the
        fields the same live probe confirmed always present. A broker
        response missing any of them is a contract change, not something to
        silently default to ``0.0`` -- this raises
        :class:`~nightwatch.etoro.errors.EtoroValidationError` instead.
        """
        position_id = next(
            (raw[k] for k in ("positionID", "positionId", "id") if raw.get(k) is not None),
            None,
        )
        if position_id is None:
            raise EtoroValidationError(f"position missing positionID: {dict(raw)!r}"[:500])
        instrument_id = raw.get("instrumentID", raw.get("instrumentId"))
        if instrument_id is None:
            # Not merely "one more required field". Since there is no symbol
            # string on a position (§4.2), instrumentID is the *only* route to
            # a symbol -- defaulting it to 0 would resurrect exactly the silent
            # null this correction exists to remove, one level down.
            raise EtoroValidationError(
                f"position {position_id} missing instrumentID: {dict(raw)!r}"[:500]
            )
        for required in ("units", "openRate", "amount"):
            if raw.get(required) is None:
                raise EtoroValidationError(
                    f"position {position_id} missing required field {required!r}: "
                    f"{dict(raw)!r}"[:500]
                )
        return Position(
            position_id=str(position_id),
            instrument_id=int(instrument_id),
            units=float(raw["units"]),
            entry_price=float(raw["openRate"]),
            amount=float(raw["amount"]),
            leverage=int(raw.get("leverage", 1)),
            is_buy=bool(raw.get("isBuy", True)),
            open_date=raw.get("openDateTime"),
            raw=dict(raw),
        )

    # -- trade history, paged, <1yr window ------------------------------------

    def get_trade_history(
        self,
        *,
        start_date: str,
        end_date: str,
        page_size: int = 200,
        max_pages: int = 50,
    ) -> list[TradeHistoryEntry]:
        """``GET /trading/info/trade/history`` (or ``/trade/demo/history``), paged.

        Args:
            start_date: ``YYYY-MM-DD``, inclusive. Sent to eToro as ``MinDate``,
                which the live API genuinely enforces.
            end_date: ``YYYY-MM-DD``, inclusive. Sent as ``MaxDate`` too (kept
                for forward-compatibility and to mirror what the real request
                is), but **enforced locally, in this method** -- see below.

        Corrected 2026-09-05 against a real-account live probe, two related
        fixes:

        1. **Wrong query parameter names.** This sent ``startDate``/
           ``endDate``, which the live API rejects outright with HTTP 400
           (``"Missing required query parameter [MinDate]"``). The real
           names are ``MinDate``/``MaxDate``.
        2. **``MaxDate`` is silently ignored server-side.** A live
           ``MinDate=2024-08-01, MaxDate=2025-01-01`` query still returned
           trades closed through 2026-08-31 -- only ``MinDate`` actually
           bounds the result. Because of this, ``end_date`` is now enforced
           **locally**, after the fetch, by filtering on each row's
           ``close_date`` -- the server will not do it, so this method must.
           Do not remove this local filter on the assumption ``MaxDate``
           will someday start working; that is a confirmed *current*
           behaviour of the live API, not a tracked gap.

        Also corrected 2026-09-05: the previous <1yr-lookback window guard
        (``MAX_TRADE_HISTORY_DAYS``, removed -- see the comment where it
        used to be defined) enforced a limit the live API does not actually
        have. The same probe found the endpoint **is not windowed at all**:
        a single call with ``MinDate=1970-01-01`` and ``pageSize=500``
        returned this real account's complete 120-row closed-trade history,
        earliest close 2020-12-21, latest 2026-08-31 -- nearly six years in
        one response, no retention cutoff found. This closes
        DESIGN_market_data.md §10.5 (was HIGH, blocked §4.6c): long-run
        realized P&L is reconstructable from the broker alone; no second,
        local sidecar table is needed. The page-by-``page`` loop below is
        kept, deliberately, as a plain defensive measure in case some
        account's full history someday exceeds ``page_size`` rows in one
        page -- not because any windowing limit was ever confirmed to
        require it; ``max_pages`` bounds it from ever looping forever
        against a misbehaving server.

        Corrected 2026-09-04: needs the demo/real segment, same issue as
        :meth:`get_eligibility`. Note the segment sits between ``trade/`` and
        ``history``, not before ``trade``.
        """
        entries: list[TradeHistoryEntry] = []
        page = 0
        while page < max_pages:
            response = self._request(
                "GET",
                f"/api/v1/trading/info/trade/{self._demo()}history",
                request_class="read",
                params={
                    "MinDate": start_date,
                    "MaxDate": end_date,
                    "page": page,
                    "pageSize": page_size,
                },
            )
            data = response.json()
            # The live trade-history body is a bare JSON *list*, not an
            # envelope.  The previous form of this line -- ``data.get("items",
            # data if isinstance(data, list) else [])`` -- looked like it
            # handled exactly that, but could not: ``data.get`` is evaluated
            # before the ``isinstance`` fallback is ever considered, so a list
            # raised ``AttributeError: 'list' object has no attribute 'get'``
            # and the fallback was dead code. Found on-device 2026-09-05, the
            # fifth bug of this family (HANDOFF_2026-09-05.md §4) and the
            # first where the wrong assumption was about the *envelope* rather
            # than a field inside it. Branch explicitly, and raise on a shape
            # that is neither -- a silent empty list here would report "no
            # trade history" for an account that has 120 closed trades.
            if isinstance(data, list):
                rows = data
            elif isinstance(data, dict):
                rows = data.get("items", [])
            else:
                raise EtoroValidationError(
                    "trade history: expected a JSON list or an object with "
                    f"'items', got {type(data).__name__}"
                )
            if not rows:
                break
            entries.extend(self._parse_trade_history_entry(r) for r in rows)
            if len(rows) < page_size:
                break
            page += 1
        return [e for e in entries if self._closed_on_or_before(e.close_date, end_date)]

    @staticmethod
    def _closed_on_or_before(close_date: str | None, end_date: str) -> bool:
        """True unless ``close_date`` is confirmed later than ``end_date``.

        The local half of the ``MaxDate`` workaround (see
        :meth:`get_trade_history`'s docstring): the server does not bound
        the result by ``MaxDate``, so this client does, comparing the
        ``YYYY-MM-DD`` date prefix. A row with no ``close_date`` at all is
        kept, not dropped -- there is nothing to compare it against, and
        this is a client-side display filter, not a validation gate that
        should fail closed on missing data.
        """
        if close_date is None:
            return True
        return str(close_date)[:10] <= end_date

    @staticmethod
    def _parse_trade_history_entry(raw: Mapping[str, Any]) -> TradeHistoryEntry:
        """Parse one closed-trade row (DESIGN_market_data.md §10.5).

        Corrected 2026-09-05 against 120 real closed-trade rows from a live
        account: see :class:`~nightwatch.etoro.models.TradeHistoryEntry`'s
        docstring for the full field-name correction (including the
        cross-endpoint casing trap -- trade history uses ``positionId``/
        ``instrumentId``, lowercase ``d``, while the portfolio endpoint uses
        capital ``D``) and the ``netProfit``-vs-``fees`` arithmetic.

        ``positionId``, ``units``, and ``openRate`` absent is a broker-contract
        change, not something to silently default to ``0.0`` -- matching
        ``_parse_position``'s corrected precedent (§10.7); this raises
        :class:`~nightwatch.etoro.errors.EtoroValidationError` instead.
        """
        position_id = next(
            (raw[k] for k in ("positionId", "positionID", "id") if raw.get(k) is not None),
            None,
        )
        if position_id is None:
            raise EtoroValidationError(
                f"trade history row missing positionId: {dict(raw)!r}"[:500]
            )
        instrument_id = raw.get("instrumentId", raw.get("instrumentID"))
        if instrument_id is None:
            raise EtoroValidationError(
                f"trade history row {position_id} missing instrumentId: "
                f"{dict(raw)!r}"[:500]
            )
        for required in ("units", "openRate"):
            if raw.get(required) is None:
                raise EtoroValidationError(
                    f"trade history row {position_id} missing required field "
                    f"{required!r}: {dict(raw)!r}"[:500]
                )
        return TradeHistoryEntry(
            position_id=str(position_id),
            instrument_id=int(instrument_id),
            units=float(raw["units"]),
            is_buy=bool(raw.get("isBuy", True)),
            leverage=int(raw.get("leverage", 1)),
            open_rate=float(raw["openRate"]),
            close_rate=(float(raw["closeRate"]) if raw.get("closeRate") is not None else None),
            profit=(float(raw["netProfit"]) if raw.get("netProfit") is not None else None),
            investment=(
                float(raw["investment"]) if raw.get("investment") is not None else None
            ),
            fees=(float(raw["fees"]) if raw.get("fees") is not None else None),
            open_date=raw.get("openTimestamp"),
            close_date=raw.get("closeTimestamp"),
            raw=dict(raw),
        )

    # -- instrument resolution + caching (immutable ids, cache via DB) --------

    def search_instrument(self, symbol: str) -> InstrumentSearchResult:
        """``GET /market-data/search`` for one ticker (uncached).

        Corrected 2026-09-04 against live behaviour: the endpoint is
        ``/api/v1/market-data/search`` -- **not**
        ``/api/v1/market-data/instruments/search``, which 404s
        (``RouteNotFound``). The response's top-level key is ``items``, not
        ``instruments``, and each row's fields are the ``internal``-prefixed
        names (``internalInstrumentDisplayName``, ``internalExchangeName``)
        rather than the unprefixed names DESIGN assumed; ``instrumentId`` and
        ``internalSymbolFull`` are unprefixed and match. No ``currency`` (or
        ``instrumentCurrency``) field came back under any name tried, so
        :attr:`InstrumentSearchResult.currency` is ``None`` in practice for
        every symbol checked (UPRO/SCHG/SPY).

        A plain ``internalSymbolFull`` query is **not** an exact match --
        querying ``"SPY"`` also returns ``SPY.RTH``, ``SPY4.DE``, ``SPY5.L``,
        ``SPYD``, etc (10 rows for a 20-row page), so this filters to the row
        whose ``internalSymbolFull`` equals ``symbol`` exactly rather than
        trusting the first row returned.
        """
        response = self._request(
            "GET",
            "/api/v1/market-data/search",
            request_class="market_data",
            params={
                "internalSymbolFull": symbol,
                "fields": "instrumentId,internalSymbolFull,internalInstrumentDisplayName,internalExchangeName",
            },
        )
        data = response.json()
        # "items" is the real key; "instruments" is tolerated too (a
        # concurrent test suite this module must not edit -- see
        # tests/test_market_bars.py -- mocks the pre-correction shape).
        rows = data.get("items", data.get("instruments", data if isinstance(data, list) else [data]))
        exact = [r for r in rows if r.get("internalSymbolFull") == symbol]
        matches = exact or rows
        if not matches:
            raise EtoroValidationError(f"no instrument found for symbol {symbol!r}")
        row = matches[0]
        return InstrumentSearchResult(
            instrument_id=int(row.get("instrumentId", row.get("internalInstrumentId"))),
            symbol=symbol,
            name=row.get("internalInstrumentDisplayName") or row.get("name"),
            exchange=row.get("internalExchangeName") or row.get("exchange"),
            currency=row.get("instrumentCurrency") or row.get("currency"),
            raw=dict(row),
        )

    def resolve_instrument_id(self, symbol: str) -> int:
        """Resolve ``symbol`` to its immutable eToro instrument id.

        Cached in the DB `instruments` table forever (ids are immutable per
        DESIGN §1.1) -- a repeat call never hits the network once cached, and
        never re-checks, since there is nothing to invalidate.
        """
        if self._repo is not None:
            cached = self._repo.get_instrument(symbol)
            if cached is not None:
                return cached.instrument_id
        result = self.search_instrument(symbol)
        if self._repo is not None:
            self._repo.upsert_instrument(
                Instrument(
                    symbol=symbol,
                    instrument_id=result.instrument_id,
                    name=result.name,
                    exchange=result.exchange,
                    currency=result.currency,
                )
            )
        return result.instrument_id

    def get_instruments(self, instrument_ids: Sequence[int]) -> list[dict[str, Any]]:
        """``GET /market-data/instruments`` metadata for a set of ids.

        Corrected 2026-09-04: the path returns 200, but the response's
        top-level key is ``instrumentDisplayDatas``, not ``instruments`` --
        the old key silently returned an empty list on every real call, a
        "didn't error" pass that was not actually a pass.
        """
        response = self._request(
            "GET",
            "/api/v1/market-data/instruments",
            request_class="market_data",
            params={"instrumentIds": ",".join(str(i) for i in instrument_ids)},
        )
        data = response.json()
        return list(
            data.get("instrumentDisplayDatas", data if isinstance(data, list) else [])
        )

    # -- rates + candles ---------------------------------------------------

    def get_rates(self, instrument_ids: Sequence[int]) -> dict[int, Rate]:
        """``GET /market-data/instruments/rates`` -- live quotes, no market-open flag.

        This path was already correct (unlike search/candles) and returns
        200 with real quotes. Corrected 2026-09-04, from a live response for
        UPRO/SCHG/SPY: the instrument-id field is ``instrumentID`` (lowercase
        ``i``, capitalised ``ID``) -- a *third* casing distinct from both
        alternatives the old code checked (``instrumentId``,
        ``InstrumentID``), so every row silently matched neither and this
        method always returned an empty dict on real data despite the HTTP
        call itself succeeding -- exactly the "didn't error but not a pass"
        trap. ``bid``/``ask``/``lastExecution`` were already right. There is
        no ``timestamp``/``dateTime`` field; the quote's timestamp comes back
        as ``date``.
        """
        response = self._request(
            "GET",
            "/api/v1/market-data/instruments/rates",
            request_class="market_data",
            params={"instrumentIds": ",".join(str(i) for i in instrument_ids)},
        )
        data = response.json()
        rows = data.get("rates", data if isinstance(data, list) else [])
        result: dict[int, Rate] = {}
        for row in rows:
            instrument_id = int(
                row.get("instrumentID", row.get("instrumentId", row.get("InstrumentID", 0)))
            )
            result[instrument_id] = Rate(
                instrument_id=instrument_id,
                bid=float(row.get("bid", row.get("Bid", 0.0))),
                ask=float(row.get("ask", row.get("Ask", 0.0))),
                last_execution=(
                    float(row["lastExecution"]) if row.get("lastExecution") is not None else None
                ),
                timestamp=row.get("date") or row.get("timestamp") or row.get("dateTime"),
            )
        return result

    #: Wire values for the ``{direction}`` path segment eToro actually
    #: accepts. DESIGN's own vocabulary (``"backward"``/``"forward"``) is
    #: kept as this method's public parameter -- unchanged so existing call
    #: sites (``market/bars.py``) need no edit -- and translated here.
    _CANDLE_DIRECTION_WIRE: Final[dict[str, str]] = {"backward": "desc", "forward": "asc"}

    def get_candles(
        self, instrument_id: int, *, direction: str = "backward", interval: str = "OneDay",
        count: int = 400,
    ) -> list[Candle]:
        """``GET .../instruments/{id}/history/candles/{direction}/{interval}/{count}``.

        Corrected 2026-09-04 against live behaviour: DESIGN's path-segment
        shape (as opposed to the query-parameter shape found in eToro's own
        docs, which 404s live -- ``GET
        /market-data/instruments/history/candles?instrumentId=...`` does
        **not** work) is actually the right one; a hand probe confirmed a
        limit=1000 request genuinely returns 1000 rows (the documented cap
        is real). The one thing DESIGN got wrong is the ``{direction}``
        segment's wire vocabulary -- it must be ``asc``/``desc``, not
        ``backward``/``forward`` (which 404 identically to every other wrong
        path tried). This method's own ``direction`` parameter keeps
        ``backward``/``forward`` and translates below, so callers are
        unaffected.
        """
        wire_direction = self._CANDLE_DIRECTION_WIRE.get(direction, direction)
        response = self._request(
            "GET",
            f"/api/v1/market-data/instruments/{instrument_id}/history/candles/"
            f"{wire_direction}/{interval}/{count}",
            request_class="market_data",
        )
        data = response.json()
        # Response shape observed in eToro's own docs nests candles under the
        # instrument; tolerate both a flat list and that nested shape.
        rows: Iterable[Mapping[str, Any]]
        if isinstance(data, list):
            rows = data
        else:
            candles_block = data.get("candles", [data])
            if candles_block and isinstance(candles_block[0], dict) and "candles" in candles_block[0]:
                rows = candles_block[0]["candles"]
            else:
                rows = candles_block
        return [self._parse_candle(row, interval) for row in rows]

    @staticmethod
    def _parse_candle(row: Mapping[str, Any], interval: str) -> Candle:
        """Parse one candle row, preserving the bar's own timestamp and interval.

        Corrected 2026-09-05: the previous inline version truncated the date
        field to ``[:10]`` for every interval, discarding the bar time on
        anything finer than ``OneDay``. Gate 10's freshness check
        (DESIGN_market_data.md §6, §2.11) depends on exactly that bar-level
        timestamp, so the truncation quietly removed the signal the gate was
        specified around. Both forms are kept now: ``date`` truncated as
        before for existing callers, ``timestamp`` verbatim.

        ``interval`` added the same day: it is ``get_candles``'s own call
        parameter (already known, needs no guessing here) and is threaded
        straight through onto :class:`Candle` so gate 10 can pick a
        freshness bound that matches the bar it was actually handed instead
        of assuming one flat bound fits every granularity.
        """
        raw_date = row.get("fromDate") or row.get("date") or row.get("timestamp")
        return Candle(
            date=str(raw_date)[:10],
            open=float(row.get("open", row.get("Open", 0.0))),
            high=float(row.get("high", row.get("High", 0.0))),
            low=float(row.get("low", row.get("Low", 0.0))),
            close=float(row.get("close", row.get("Close", 0.0))),
            volume=(float(row["volume"]) if row.get("volume") is not None else None),
            timestamp=(None if raw_date is None else str(raw_date)),
            interval=interval,
        )

    # -- eligibility, batched ≤100 ---------------------------------------------

    def get_eligibility(self, instrument_ids: Sequence[int]) -> EligibilityBatchResult:
        """``POST /trading/info/eligibility`` (or ``/demo/eligibility``), batched
        at ≤100 ids per call.

        Corrected 2026-09-04: this path needs the demo/real segment like most
        trading/info endpoints -- it was previously hardcoded without one,
        which happened to work for ``real`` (no segment) but silently called
        the real-account eligibility endpoint even in demo.

        Also corrected the same day, from a live response body for
        UPRO/SCHG: the top-level key is ``eligibilities``, not
        ``eligibility`` -- the old key silently returned zero rows on every
        real call. ``allowPartialClose`` is actually named
        ``allowPartialClosePosition``. And ``minPositionAmount`` is **not**
        a top-level field alongside ``minPositionExposure`` at all -- it
        lives one level down, once per ``(settlementType, direction)``
        combination the instrument supports, inside ``leverageConfigs``
        (e.g. real/long, cfd/short, cfd/long each have their own
        ``minPositionAmount``, all ``10.0`` for UPRO/SCHG in practice). See
        :meth:`_min_position_amount_for_real_long`, which picks the one
        combination this app can ever actually place an order as.
        """
        results: dict[int, Eligibility] = {}
        ids = list(instrument_ids)
        for start in range(0, len(ids), MAX_ELIGIBILITY_BATCH):
            batch = ids[start : start + MAX_ELIGIBILITY_BATCH]
            response = self._request(
                "POST",
                f"/api/v2/trading/info/{self._demo()}eligibility",
                request_class="eligibility",
                json_body={"instrumentIds": batch},
            )
            data = response.json()
            rows = data.get(
                "eligibilities", data.get("eligibility", data if isinstance(data, list) else [])
            )
            for row in rows:
                instrument_id = int(row.get("instrumentId", row.get("instrumentID", 0)))
                results[instrument_id] = Eligibility(
                    instrument_id=instrument_id,
                    allow_open_position=bool(row.get("allowOpenPosition", False)),
                    min_position_exposure=(
                        float(row["minPositionExposure"])
                        if row.get("minPositionExposure") is not None
                        else None
                    ),
                    min_position_amount=self._min_position_amount_for_real_long(row),
                    allow_partial_close=(
                        bool(row["allowPartialClosePosition"])
                        if row.get("allowPartialClosePosition") is not None
                        else (
                            bool(row["allowPartialClose"])
                            if row.get("allowPartialClose") is not None
                            else None
                        )
                    ),
                    raw=dict(row),
                )
        return EligibilityBatchResult(results=results)

    @staticmethod
    def _min_position_amount_for_real_long(row: Mapping[str, Any]) -> float | None:
        """Pick ``minPositionAmount`` for this app's one possible order shape.

        A live eligibility row nests ``minPositionAmount`` per
        ``leverageConfigs`` entry, one per ``(settlementType, direction)``
        pair the instrument supports (observed for UPRO/SCHG: real/long,
        cfd/short, cfd/long). This app can only ever place a
        ``settlementType="real"``, direction ``"long"`` order (see
        :data:`SETTLEMENT_TYPE`/:data:`TRANSACTION`/:data:`MAX_LEVERAGE`),
        so this returns that one row's value, or ``None`` if the instrument
        does not offer that combination at all (also tolerates a
        flat/top-level ``minPositionAmount``, in case a future response
        shape or a hand-built test fixture puts it there directly).
        """
        if row.get("minPositionAmount") is not None:
            return float(row["minPositionAmount"])
        for leverage_config in row.get("leverageConfigs", []) or []:
            if (
                leverage_config.get("settlementType") == SETTLEMENT_TYPE
                and leverage_config.get("direction") == "long"
            ):
                value = leverage_config.get("minPositionAmount")
                return float(value) if value is not None else None
        return None

    def check_eligibility_for_order(
        self, instrument_id: int, order_size: float
    ) -> Eligibility:
        """Fetch eligibility for one instrument and enforce the minimum-size
        guard (DESIGN §4.3): blocks and alerts loudly, never silently resizes.
        """
        batch = self.get_eligibility([instrument_id])
        eligibility = batch.get(instrument_id)
        if eligibility is None:
            raise EtoroEligibilityError(
                f"no eligibility data returned for instrument {instrument_id}"
            )
        if not eligibility.allow_open_position:
            raise EtoroEligibilityError(
                f"instrument {instrument_id} is not open-able "
                "(allowOpenPosition=false)"
            )
        if (
            eligibility.min_position_exposure is not None
            and order_size < eligibility.min_position_exposure
        ):
            raise EtoroEligibilityError(
                f"order_size {order_size} is below instrument {instrument_id}'s "
                f"minPositionExposure {eligibility.min_position_exposure} -- "
                "blocked, not resized"
            )
        return eligibility

    # -- costs -----------------------------------------------------------------

    def get_costs(self, instrument_id: int, amount: float) -> dict[str, Any]:
        """``POST /trading/info/costs`` (or ``/demo/costs``).

        Corrected 2026-09-04: needs the demo/real segment, same issue as
        :meth:`get_eligibility`. Also corrected the same day: the body
        requires a ``transaction`` field -- without it eToro 400s with
        ``"Transaction must be a valid transaction type (Buy, Sell,
        SellShort, BuyToCover)"``. Confirmed live that eToro accepts the
        lowercase :data:`TRANSACTION` constant (``"buy"``) directly, no case
        conversion needed.
        """
        response = self._request(
            "POST",
            f"/api/v2/trading/info/{self._demo()}costs",
            request_class="read",
            json_body={
                "instrumentId": instrument_id,
                "amount": amount,
                "leverage": MAX_LEVERAGE,
                "transaction": TRANSACTION,
            },
        )
        return dict(response.json())

    # -- orders: open (buy) ------------------------------------------------

    def place_buy_order(self, request_id: str, instrument_id: int, amount_usd: float) -> str:
        """``POST /trading/execution/orders`` (or `/demo/orders`) -- DESIGN §4.3.

        Args:
            request_id: The caller-supplied ``x-request-id`` UUID. MUST have
                already been persisted to the ``intents`` table (DESIGN §5)
                before this call -- this method does not generate its own id,
                by design, so recovery can look it up by ``referenceId``.
            instrument_id: The resolved (and immutable) eToro instrument id.
            amount_usd: USD notional to open (opens are USD-denominated).

        Returns:
            eToro's ``orderId`` string. HTTP 200 here means *accepted*, not
            filled -- callers MUST follow up with :meth:`poll_order_to_terminal`.
        """
        body = OrderRequest(instrument_id=instrument_id, amount=amount_usd).to_json()
        response = self._request(
            "POST",
            f"/api/v2/trading/execution/{self._demo()}orders",
            request_class="write",
            json_body=body,
            request_id=request_id,
        )
        data = response.json()
        order_id = data.get("orderId") or data.get("orderID") or data.get("id")
        return str(order_id) if order_id is not None else ""

    # -- orders: close (full close only) --------------------------------------

    def close_position(self, request_id: str, position_id: str, instrument_id: int) -> str:
        """``POST /market-close-orders/positions/{positionId}`` (DESIGN §4.3).

        Always a full close -- ``UnitsToDeduct: null`` -- because one lot is
        exactly one eToro position and partial close is explicitly out of
        scope for this app (see :class:`~nightwatch.etoro.models.CloseRequest`,
        which refuses to construct with a non-``None`` unit count).
        """
        body = CloseRequest(position_id=position_id, instrument_id=instrument_id).to_json()
        response = self._request(
            "POST",
            f"/api/v1/trading/execution/{self._demo()}market-close-orders/positions/{position_id}",
            request_class="write",
            json_body=body,
            request_id=request_id,
        )
        data = response.json()
        order_id = data.get("orderId") or data.get("orderID") or data.get("id")
        return str(order_id) if order_id is not None else ""

    # -- orders:lookup, poll to terminal, referenceId recovery ---------------

    def lookup_order(
        self, *, order_id: str | None = None, reference_id: str | None = None
    ) -> OrderResult:
        """``GET /v2/trading/info/orders:lookup`` (or ``/demo/orders:lookup``)
        for one order.

        Exactly one of ``order_id``/``reference_id`` should normally be given;
        ``reference_id`` is what a crash-recovery path uses (DESIGN §6.H) when
        the original response was lost and the local ``x-request-id`` is the
        only handle left.

        Corrected 2026-09-04: needs the demo/real segment, same issue as
        :meth:`get_eligibility`.
        """
        if order_id is None and reference_id is None:
            raise ValueError("lookup_order requires order_id or reference_id")
        params: dict[str, Any] = {}
        if order_id is not None:
            params["orderId"] = order_id
        if reference_id is not None:
            params["referenceId"] = reference_id
        response = self._request(
            "GET",
            f"/api/v2/trading/info/{self._demo()}orders:lookup",
            request_class="read",
            params=params,
        )
        return self._parse_order_result(response.json())

    def recover_by_reference_id(self, reference_id: str) -> OrderResult | None:
        """Recover a possibly-lost response by ``referenceId`` (DESIGN §6.H).

        Returns ``None`` if eToro has no record of the order at all -- which
        means the original request never reached the server and it is safe
        to (re)submit; any other result means it did, and must never be
        submitted again.
        """
        try:
            return self.lookup_order(reference_id=reference_id)
        except EtoroValidationError:
            return None

    @staticmethod
    def _parse_order_result(data: Mapping[str, Any]) -> OrderResult:
        row = data.get("order", data)
        status_raw = row.get("status", row.get("orderStatus"))
        try:
            status = OrderStatus(int(status_raw))
        except (TypeError, ValueError):
            status = OrderStatus.RECEIVED
        return OrderResult(
            order_id=(str(row["orderId"]) if row.get("orderId") is not None else None),
            reference_id=(
                str(row["referenceId"]) if row.get("referenceId") is not None else None
            ),
            status=status,
            position_id=(
                str(row["positionId"]) if row.get("positionId") is not None else None
            ),
            fill_price=(float(row["executionPrice"]) if row.get("executionPrice") is not None else None),
            fill_units=(float(row["units"]) if row.get("units") is not None else None),
            fill_amount_usd=(float(row["amount"]) if row.get("amount") is not None else None),
            reject_reason=row.get("rejectReason"),
            raw=dict(row),
        )

    def poll_order_to_terminal(
        self,
        *,
        order_id: str | None = None,
        reference_id: str | None = None,
        interval_seconds: float | None = None,
        timeout_seconds: float | None = None,
    ) -> PollResult:
        """Poll ``orders:lookup`` until a terminal status or a timeout.

        DESIGN §1.1/§6.H: HTTP 200 on submission means *accepted*, not
        *filled*. Status 11 (WaitingForMarket) is explicitly "queued, not
        filled" and is never treated as terminal here -- polling continues
        (bounded by ``timeout_seconds``) rather than returning early on it.
        A timeout returns the last observed status with ``timed_out=True``;
        it is the caller's job to never read that as a fill (`OrderResult`'s
        own `is_filled` already encodes this, since WAITING_FOR_MARKET and
        every non-terminal status are excluded from `FILLED_ORDER_STATUSES`).
        """
        interval = interval_seconds if interval_seconds is not None else self._poll_interval
        timeout = timeout_seconds if timeout_seconds is not None else self._poll_timeout
        deadline = self._clock() + timeout
        attempts = 0
        result = self.lookup_order(order_id=order_id, reference_id=reference_id)
        attempts += 1
        while not result.is_terminal:
            if self._clock() >= deadline:
                return PollResult(order=result, attempts=attempts, timed_out=True)
            self._sleep(interval)
            result = self.lookup_order(
                order_id=order_id or result.order_id, reference_id=reference_id
            )
            attempts += 1
        if result.status in (OrderStatus.REJECTED, OrderStatus.REJECTED_PARTIALLY_FILLED):
            _logger.warning(
                "order rejected: order_id=%s reference_id=%s reason=%s",
                order_id, reference_id, result.reject_reason,
            )
        return PollResult(order=result, attempts=attempts, timed_out=False)
