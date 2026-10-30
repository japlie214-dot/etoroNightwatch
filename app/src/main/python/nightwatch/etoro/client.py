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
import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Final, Iterable, Literal, Mapping, Sequence

import httpx

from ..config import Credentials, EtoroConfig
from ..db.repo import Instrument, Repo
from .contracts import ContractReport, check_payload
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
    FILLED_ORDER_STATUSES,
    Balance,
    Candle,
    CloseRequest,
    EditPositionAck,
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

if TYPE_CHECKING:
    from ..notify.telegram import TelegramNotifier

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
        contract_notifier: TelegramNotifier | None = None,
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
        self._contract_notifier = contract_notifier
        self._reported_contract_drift: set[tuple[Any, ...]] = set()
        self._limiter = rate_limiter or RateLimiter()
        self._sleep = sleep
        self._clock = clock
        self._max_attempts = max_attempts
        self._poll_interval = poll_interval_seconds
        self._poll_timeout = poll_timeout_seconds
        #: The most recent response's `Date` header, for preflight gate 4
        #: (clock skew > 60s aborts). `None` until at least one call is made.
        self.last_server_date: str | None = None
        #: Last write response body, including permanent HTTP rejections, for
        #: probe evidence. Reset before each write so failures cannot expose a
        #: previous order's response.
        self.last_write_response_body: Any = None
        #: Exact JSON body passed to the latest write request, available even
        #: when the transport raises before an HTTP response arrives.
        self.last_write_request_body: Any = None
        #: Every HTTP response from the dedicated close-order lookup endpoint,
        #: including retryable and terminal errors, for close-probe evidence.
        self.last_close_lookup_responses: list[dict[str, Any]] = []

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

    def _check_response_contract(
        self,
        contract: str,
        payload: Any,
        *,
        severity: Literal["critical", "advisory"],
    ) -> ContractReport:
        """Report structural drift once per client; the parser enforces critical fields."""
        report = check_payload(contract, payload)
        if not report.violations:
            return report

        signature = tuple(sorted(
            (v.kind, v.path, v.expected, v.actual) for v in report.violations
        ))
        key = (self.environment, contract, signature)
        if key not in self._reported_contract_drift:
            self._reported_contract_drift.add(key)
            summary = "; ".join(
                f"{kind} {path} (expected {expected}, got {actual})"
                for kind, path, expected, actual in signature
            )
            message = (
                f"[{self.environment.upper()}] eToro {contract} payload contract drift: "
                f"{summary}"
            )
            (_logger.error if severity == "critical" else _logger.warning)("%s", message)
            if self._contract_notifier is not None:
                try:
                    self._contract_notifier.send(message, event="etoro_contract_drift")
                except Exception:
                    # Alert delivery must not turn an API read into a trading failure.
                    _logger.warning("eToro contract drift alert delivery failed")

        return report

    def _request(
        self,
        method: str,
        path: str,
        *,
        request_class: RequestClass,
        params: Mapping[str, Any] | None = None,
        json_body: Any | None = None,
        request_id: str | None = None,
        response_capture: list[dict[str, Any]] | None = None,
    ) -> httpx.Response:
        """Send one request with rate limiting, retries, and typed errors.

        Reads retry network errors, 5xx, and 429 with bounded backoff. Writes
        get one attempt: a timeout or server error can follow an accepted
        order, so retrying could duplicate it. Every other 4xx is also raised
        immediately.
        """
        if request_class == "write":
            self.last_write_request_body = json_body
            self.last_write_response_body = None
        attempt_limit = 1 if request_class == "write" else self._max_attempts
        last_error: Exception | str = "unknown error"
        for attempt in range(1, attempt_limit + 1):
            self._wait_for_capacity(request_class)
            headers = auth.build_headers(self._credentials, request_id)
            try:
                response = self._http.request(
                    method, path, params=params, json=json_body, headers=headers
                )
            except httpx.TimeoutException as exc:
                last_error = exc
                if attempt == attempt_limit:
                    raise EtoroTimeoutError(f"timed out calling {path}: {exc}") from exc
                self._sleep(self._backoff(attempt))
                continue
            except httpx.RequestError as exc:
                last_error = exc
                if attempt == attempt_limit:
                    raise EtoroNetworkError(f"network error calling {path}: {exc}") from exc
                self._sleep(self._backoff(attempt))
                continue

            self.last_server_date = response.headers.get("Date", self.last_server_date)
            self._limiter.reconcile(request_class, response.headers)
            if response_capture is not None:
                response_capture.append({
                    "method": method, "path": path, "params": params,
                    "status_code": response.status_code, "body": response.text,
                })

            if request_class == "write" and response.status_code >= 400:
                try:
                    self.last_write_response_body = response.json()
                except Exception:
                    self.last_write_response_body = response.text

            if response.status_code < 300:
                return response

            if response.status_code in (401, 403):
                raise EtoroAuthError(
                    f"{response.status_code} from {path}: {self._safe_body(response)}"
                )

            if response.status_code == 429:
                retry_after = RateLimiter.retry_after_from_headers(response.headers) or 1.0
                last_error = f"429 rate limited (retry_after={retry_after}s)"
                if attempt == attempt_limit:
                    raise EtoroRateLimitError(str(last_error), retry_after=retry_after)
                self._sleep(retry_after)
                continue

            if 500 <= response.status_code < 600:
                last_error = f"{response.status_code} server error: {self._safe_body(response)}"
                if attempt == attempt_limit:
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
        missing = {"credit", "orders", "ordersForOpen"} - data.keys()
        if missing:
            raise EtoroValidationError(
                "portfolio response missing required available-cash fields "
                f"{sorted(missing)!r}; body keys seen: {sorted(data.keys())!r}"
            )
        try:
            available = compute_available_cash(
                data["credit"],
                orders_for_open=data["ordersForOpen"],
                orders=data["orders"],
            )
            credit = float(data["credit"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise EtoroValidationError(
                f"portfolio response has invalid available-cash fields: {exc}"
            ) from exc
        return Balance(
            credit=credit,
            total_balance=None,
            available_cash=available,
            raw=data,
        )

    def verify_environment(self) -> None:
        """Probe that the credential's bound environment matches config.

        DESIGN §5 gate 7 / §6.F: this must be *detected*, not assumed.

        **Corrected 2026-09-11, SEVERE -- this method was a silent no-op.**
        It used to derive its verdict solely from
        :meth:`_detect_environment` applied to the ``/balances`` body, and
        that function only ever looked at the body's TOP LEVEL. A read-only
        capture taken today against this operator's account under BOTH
        configs (``data/probe_2026-09-11/env_probe_real/`` and
        ``env_probe_demo/``, produced by ``probe.env_probe``) shows the live
        ``/balances`` body is::

            {"gcid": 18049964, "totalBalance": ..., "displayCurrency": "USD",
             "balances": [{"accountId": "17761031", "accountType": "Trading",
                           "subType": null, "balance": ..., ...}]}

        -- which carries no environment marker at ANY nesting depth. The
        nested ``accountType`` is ``"Trading"`` (an account *kind*, not an
        environment). Worse, the demo and real responses are **structurally
        identical**: same ``gcid``, same ``accountId``, same ``accountType``,
        same ``subType``. ``/balances`` is account-global, not
        environment-scoped, so it is *structurally incapable* of telling the
        two apart, and this method therefore detected nothing, ever, on the
        REAL account as much as on demo. Gate 7 was not guarding anything.

        The old docstring also claimed a "fairly safe to trust" asymmetric
        signal -- that a demo account carries a fixed seeded ``totalBalance``
        distinguishable from a funded real one. Today's capture refutes it:
        real ``totalBalance=56792.27`` vs demo ``56775.82``, a 0.03%
        difference. That heuristic is not implemented here and must not be.

        **What actually discriminates**, observed live today in both
        directions, is eToro's per-credential authorisation on the
        environment PATH SEGMENT (see :attr:`EtoroConfig.path_segment`):

        ==================  =====================================  ==========
        credential bound    ``GET .../trading/info/demo/portfolio``  ``.../portfolio``
        ==================  =====================================  ==========
        demo                200                                    403
        real                403                                    200
        ==================  =====================================  ==========

        (403 body: ``{"errorCode": "InsufficientPermissions", ...}``.) That
        is a *positive*, bidirectional identification of the environment the
        credential is actually bound to -- exactly what gate 7 needs, and it
        uses an endpoint this client already calls
        (:meth:`_fetch_portfolio_envelope`). :meth:`probe_environment`
        implements it.

        Fail-closed semantics are unchanged and deliberate: this method
        raises :class:`EtoroEnvironmentMismatchError` on a *detected*
        mismatch and returns normally otherwise. Returning normally still
        means "no mismatch detected" -- which is "unable to confirm" when
        :meth:`probe_environment` returns ``None`` (an ambiguous or failed
        probe), NOT "confirmed OK". Callers that need positive confirmation
        before doing something dangerous must check
        :meth:`probe_environment`'s return value themselves and refuse on
        ``None`` -- which is precisely what
        :func:`nightwatch.probes.order_latency.evaluate_demo_confirmation`
        does. The fix here makes positive confirmation *possible*; it does
        not make refusal less likely.
        """
        response = self._request(
            "GET", "/api/v1/balances", request_class="read"
        )
        data = response.json()
        # Body scan first: free (the response is already in hand), and it
        # will start working on its own the day eToro adds a marker. Today
        # it returns None against the live body -- see this method's
        # docstring and _detect_environment's.
        detected = self._detect_environment(data)
        if detected is None:
            detected = self.probe_environment()
        if detected is not None and detected != self._config.environment.value:
            raise EtoroEnvironmentMismatchError(
                configured=self._config.environment.value, detected=detected
            )

    #: The two paths whose 200/403 pattern positively identifies which
    #: environment a credential is bound to (see :meth:`verify_environment`).
    #: Both are GETs on an endpoint this client already uses; neither can
    #: place, modify, or close anything.
    _ENV_PROBE_DEMO_PATH: Final[str] = "/api/v1/trading/info/demo/portfolio"
    _ENV_PROBE_REAL_PATH: Final[str] = "/api/v1/trading/info/portfolio"

    def _probe_segment_access(self, path: str) -> bool | None:
        """``True`` if this credential may GET ``path``, ``False`` if eToro
        refuses it with 401/403, ``None`` if the question could not be
        answered at all.

        The ``None`` case matters as much as the other two: a timeout, a
        5xx, a rate-limit exhaustion or an unexpected 4xx tells us nothing
        about the credential's binding, and must NOT be collapsed into
        "denied" -- that would let a flaky network manufacture a confident
        environment verdict out of nothing.
        """
        try:
            self._request("GET", path, request_class="read")
            return True
        except EtoroAuthError:
            return False
        except EtoroError:
            return None

    def probe_environment(self) -> str | None:
        """Positively identify the environment this CREDENTIAL is bound to.

        Returns ``"demo"``, ``"real"``, or ``None`` for "could not
        determine" -- never a guess. Costs two read-class GETs.

        Method and the live evidence for it are documented on
        :meth:`verify_environment`: eToro authorises a credential for the
        demo path segment or the real one, not both, so the pair of
        (allowed, denied) answers names the binding. This deliberately
        probes the *credential*, not the config -- config is the claim being
        checked, so it is not consulted here at all.

        ``None`` is returned whenever the two answers do not form a clean
        exclusive pair -- both allowed, both denied, or either probe
        inconclusive. A future eToro change that granted a credential both
        segments would land here as ``None`` (refuse), not as a coin flip.
        """
        demo_allowed = self._probe_segment_access(self._ENV_PROBE_DEMO_PATH)
        real_allowed = self._probe_segment_access(self._ENV_PROBE_REAL_PATH)
        if demo_allowed is None or real_allowed is None:
            return None
        if demo_allowed and not real_allowed:
            return "demo"
        if real_allowed and not demo_allowed:
            return "real"
        return None

    @staticmethod
    def _detect_environment(payload: Mapping[str, Any]) -> str | None:
        """Best-effort extraction of an environment marker from a balances body.

        Returns ``"demo"``, ``"real"``, or ``None`` if no marker is present.
        Kept as a static, independently-testable function so it can be
        exercised with a hand-built payload without any HTTP call.

        **This returns ``None`` against the real live payload, and that is
        the correct answer, not a bug.** The live ``/balances`` body carries
        no environment marker at any depth, and is byte-shape-identical
        between demo and real (see :meth:`verify_environment`'s docstring
        for the captured evidence). This function is retained rather than
        deleted for two reasons: it is the cheap check (the body is already
        in hand), and it starts working for free if eToro ever adds a
        marker. It is no longer the *only* thing gate 7 relies on.

        Fixed 2026-09-11: the scan now descends into nested dicts and lists
        instead of looking only at the top level -- the old top-level-only
        scan was the specific defect that made
        :meth:`verify_environment` a silent no-op. Note that descending is
        necessary but was never sufficient here: the live body's only
        nested near-match is ``accountType: "Trading"``, which is an account
        kind, not an environment, and is correctly ignored below because
        ``"trading"`` is in neither value set.
        """
        return EtoroClient._detect_environment_in(payload, _depth=0)

    @staticmethod
    def _detect_environment_in(node: Any, *, _depth: int) -> str | None:
        """Recursive worker for :meth:`_detect_environment`.

        Depth-bounded (8) so a pathological or cyclic-looking payload cannot
        turn a safety probe into an unbounded walk.
        """
        if _depth > 8:
            return None
        if isinstance(node, Mapping):
            for key in ("isDemo", "is_demo", "demo", "isVirtual", "is_virtual"):
                if key in node and isinstance(node[key], bool):
                    return "demo" if node[key] else "real"
            for key in ("accountType", "account_type", "environment", "subType", "sub_type"):
                if key in node and node[key] is not None:
                    value = str(node[key]).strip().lower()
                    if value in ("demo", "virtual", "practice"):
                        return "demo"
                    if value in ("real", "live"):
                        return "real"
                    # Anything else (e.g. the live body's "Trading") is not
                    # an environment marker -- keep scanning, never guess.
            for value in node.values():
                found = EtoroClient._detect_environment_in(value, _depth=_depth + 1)
                if found is not None:
                    return found
            return None
        if isinstance(node, (list, tuple)):
            for item in node:
                found = EtoroClient._detect_environment_in(item, _depth=_depth + 1)
                if found is not None:
                    return found
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

    def get_portfolio_strict(self) -> list[Position]:
        """Read positions without treating an invalid envelope as an empty account."""
        data = self._fetch_portfolio_envelope()
        if not isinstance(data, dict) or not isinstance(data.get("positions"), list):
            raise EtoroValidationError("portfolio response must contain a positions list")
        return [self._parse_position(p) for p in data["positions"]]

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

        Bug found 2026-09-04, corrected 2026-09-05 (board nightwatch/handoff-2026-09-05.md §1,
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

        ``takeProfitRate``/``isNoTakeProfit`` and ``stopLossRate``/
        ``isNoStopLoss`` are read as of 2026-09-11 (they were always in the
        payload, never parsed -- see :class:`Position`'s docstring and
        DESIGN.md §6.A "Broker-side exits"). They are deliberately NOT in
        the required list: unlike ``units``/``openRate``/``amount`` they
        carry no position-identity meaning, so a payload without them parses
        to ``None``/``False`` rather than raising -- the detection helper
        (:func:`nightwatch.engine.preflight.find_broker_take_profits`) treats
        "not stated" as "not detected", never as "suppressed".
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
            take_profit_rate=(
                float(raw["takeProfitRate"]) if raw.get("takeProfitRate") is not None else None
            ),
            is_no_take_profit=bool(raw.get("isNoTakeProfit", False)),
            stop_loss_rate=(
                float(raw["stopLossRate"]) if raw.get("stopLossRate") is not None else None
            ),
            is_no_stop_loss=bool(raw.get("isNoStopLoss", False)),
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
            # fifth bug of this family (board nightwatch/handoff-2026-09-05.md §4) and the
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
        if isinstance(data, list):
            rows = data
        elif isinstance(data, Mapping):
            # "items" is the live key; "instruments" is tolerated for older
            # callers and test fixtures, but fuzzy rows are never accepted.
            rows = data.get("items", data.get("instruments"))
        else:
            rows = None
        if not isinstance(rows, list) or any(
            not isinstance(row, Mapping) for row in rows
        ):
            raise EtoroValidationError(
                "market-data/search response must contain an array of objects"
            )
        exact = [row for row in rows if row.get("internalSymbolFull") == symbol]
        if not exact:
            raise EtoroValidationError(f"no exact instrument found for symbol {symbol!r}")
        instrument_ids: list[int] = []
        try:
            for row in exact:
                value = row.get("instrumentId", row.get("internalInstrumentId"))
                if isinstance(value, bool):
                    raise ValueError("boolean instrument id")
                if isinstance(value, int):
                    instrument_id = value
                elif isinstance(value, str) and value.isdecimal():
                    instrument_id = int(value)
                else:
                    raise ValueError("instrument id is not an integer")
                if instrument_id <= 0:
                    raise ValueError("instrument id must be positive")
                instrument_ids.append(instrument_id)
        except (TypeError, ValueError, OverflowError) as exc:
            raise EtoroValidationError(
                f"exact market-data/search result for {symbol!r} has no valid instrument id"
            ) from exc
        if len(set(instrument_ids)) != 1:
            raise EtoroValidationError(
                f"market-data/search returned ambiguous exact results for {symbol!r}"
            )
        row = exact[0]
        instrument_id = instrument_ids[0]
        return InstrumentSearchResult(
            instrument_id=instrument_id,
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
        self._check_response_contract("rates", data, severity="advisory")
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
        if not request_id or not request_id.strip():
            raise ValueError("place_buy_order: request_id must be non-empty (caller persists it first)")
        self.last_write_request_body = None
        self.last_write_response_body = None
        body = OrderRequest(instrument_id=instrument_id, amount=amount_usd).to_json()
        response = self._request(
            "POST",
            f"/api/v2/trading/execution/{self._demo()}orders",
            request_class="write",
            json_body=body,
            request_id=request_id,
        )
        return self._order_id_from_write_response(
            response, request_id=request_id, what="place_buy_order"
        )

    def _order_id_from_write_response(
        self, response: httpx.Response, *, request_id: str, what: str
    ) -> str:
        """Extract the broker's order id from a write response -- loudly.

        Added 2026-09-11, the same bug class as :meth:`_parse_order_result`
        and found by it: both write methods ended in
        ``return str(order_id) if order_id is not None else ""``. A response
        whose id key we did not guess correctly therefore produced an EMPTY
        STRING, which the caller then polled with -- eToro answers that with
        ``400 ... Either orderId or referenceId must be provided``, i.e. a
        successfully submitted order that we can no longer track. Confirmed
        live against the demo account: the close-order response carries none
        of ``orderId``/``orderID``/``id`` at the top level.

        So: the guessed keys are still tried first, then the whole response
        body is logged at WARNING (it is the only way to learn the real
        shape), then a buy order is recovered through the ``referenceId`` path
        DESIGN §6.H already defines -- the ``x-request-id`` we sent is ours
        and eToro echoes it back as ``referenceId``. Only if that also fails
        does this raise. Close recovery by reference is unverified and fails
        closed (T024). This never hands back ``""`` as if it were an order id.
        """
        try:
            data = response.json()
        except Exception:
            data = None
        #: The last write response body seen, verbatim -- probes save it, since
        #: these bodies are only obtainable while the market is open.
        self.last_write_response_body = data
        if isinstance(data, Mapping):
            # Confirmed live 2026-09-11 (demo): a CLOSE response nests
            # everything one level down under ``orderForClose`` and spells the
            # id ``orderID`` --
            #   {"orderForClose": {"positionID": ..., "orderID": 380818802,
            #    "orderType": 19, "statusID": 1, ...}, "token": "..."}
            # -- so no top-level key matches and the old ``or``-chain returned
            # "". Both levels are searched, top level first.
            sources = [data]
            sources.extend(v for v in data.values() if isinstance(v, Mapping))
            for source in sources:
                for key in ("orderId", "orderID", "orderid", "id"):
                    value = source.get(key)
                    if value not in (None, ""):
                        return str(value)
        _logger.warning(
            "%s: response carries no recognised order id key -- full body: %s",
            what,
            str(data)[:1000],
        )
        if what == "close_position":
            raise EtoroValidationError(
                "close_position: response had no order id; close recovery by reference is "
                "unverified (T024) -- reconcile from portfolio, do not resubmit"
            )
        try:
            recovered = self.lookup_order(reference_id=request_id)
        except EtoroError as exc:
            raise EtoroValidationError(
                f"{what}: submitted successfully but the response carries no "
                f"order id, and recovery by referenceId={request_id} failed "
                f"({exc}). Body: {str(data)[:400]}"
            ) from exc
        if recovered.order_id:
            _logger.warning(
                "%s: order id recovered via referenceId=%s -> %s",
                what, request_id, recovered.order_id,
            )
            return str(recovered.order_id)
        raise EtoroValidationError(
            f"{what}: submitted successfully but no order id could be found "
            f"in the response or recovered by referenceId={request_id}. "
            f"Body: {str(data)[:400]}"
        )

    # -- orders: close (full or partial) --------------------------------------

    def close_position(
        self,
        request_id: str,
        position_id: str,
        instrument_id: int,
        units_to_deduct: float | None = None,
    ) -> str:
        """``POST /market-close-orders/positions/{positionId}`` (DESIGN §4.3;
        partial close decided board nightwatch/handoff-2026-09-06.md §7).

        Args:
            request_id: The caller-supplied ``x-request-id`` UUID, same
                contract as :meth:`place_buy_order`.
            position_id: The eToro position to close.
            instrument_id: The resolved (and immutable) eToro instrument id.
            units_to_deduct: ``None`` (the default) sends
                ``UnitsToDeduct: null`` -- a **full** close, byte-identical on
                the wire to this app's original, only behaviour. A positive
                value instead sends a **partial** close, deducting exactly
                that many units and leaving the position OPEN with the
                remainder -- SCHG has no per-lot tracking
                (``schg_position_value`` is a scalar), so the executor that
                chooses lots must be able to close less than a whole
                position.

        This method does NOT enforce the broker's $10
        ``minPositionExposure``/``minPositionAmount`` stub floor -- it has no
        access to the position's market value. Callers passing a non-``None``
        ``units_to_deduct`` MUST first check
        :func:`~nightwatch.etoro.models.partial_close_leaves_legal_remainder`
        against the position's MARKET VALUE (``units * price``, not
        ``Position.amount``'s cost basis -- see that function's docstring for
        the still-unverified market-value-vs-cost-basis assumption).
        TODO(lot-selection executor, out of scope here): wire that check in
        before calling this with a partial ``units_to_deduct`` -- the
        executor itself is not built yet (board nightwatch/handoff-2026-09-06.md §7).
        """
        if not request_id or not request_id.strip():
            raise ValueError("close_position: request_id must be non-empty (caller persists it first)")
        self.last_write_request_body = None
        self.last_write_response_body = None
        body = CloseRequest(
            position_id=position_id,
            instrument_id=instrument_id,
            units_to_deduct=units_to_deduct,
        ).to_json()
        response = self._request(
            "POST",
            f"/api/v1/trading/execution/{self._demo()}market-close-orders/positions/{position_id}",
            request_class="write",
            json_body=body,
            request_id=request_id,
        )
        return self._order_id_from_write_response(
            response, request_id=request_id, what="close_position"
        )

    def clear_take_profit(self, request_id: str, position_id: str) -> EditPositionAck:
        """Remove a position's broker take-profit with one PATCH (demo: /demo/positions, real: /positions; eToro docs 2026-10-16).

        The caller must persist ``request_id`` before this call. Acceptance
        is asynchronous: the caller must re-read the portfolio and check with
        ``find_broker_take_profits`` to confirm removal.
        """
        if not request_id:
            raise ValueError("clear_take_profit: request_id must be non-empty")
        if not position_id or not position_id.isdigit():
            raise ValueError("clear_take_profit: position_id must be non-empty and all digits")
        response = self._request(
            "PATCH",
            f"/api/v2/trading/{self._demo()}positions/{position_id}",
            request_class="write",
            json_body={"clearTakeProfit": True},
            request_id=request_id,
        )
        if response.status_code not in (200, 202):
            raise EtoroValidationError(
                f"clear_take_profit: tool refused unexpected status {response.status_code}",
                status_code=response.status_code,
            )
        try:
            data = response.json()
        except ValueError:
            data = None
        if not isinstance(data, Mapping):
            data = {}
        ack = EditPositionAck(
            operation_id=str(data["operationId"]) if "operationId" in data else None,
            position_id=str(data["positionId"]) if "positionId" in data else None,
            reference_id=str(data["referenceId"]) if "referenceId" in data else None,
            status_code=response.status_code,
        )
        if ack.position_id is not None and ack.position_id != position_id:
            raise EtoroValidationError(
                f"clear_take_profit: tool refused positionId {ack.position_id}, expected {position_id}"
            )
        return ack

    # -- orders:lookup, poll to terminal, referenceId recovery ---------------

    def lookup_order(
        self,
        *,
        order_id: str | None = None,
        reference_id: str | None = None,
        response_capture: list[dict[str, Any]] | None = None,
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
        try:
            response = self._request(
                "GET",
                f"/api/v2/trading/info/{self._demo()}orders:lookup",
                request_class="read",
                params=params,
                response_capture=response_capture,
            )
        except EtoroValidationError as exc:
            # eToro still serves this deprecated demo GET; the current v2
            # lookup can return 404 for an order visible in the portfolio.
            # Keep it as a read-only, ID-only fallback; never retry a write.
            if not self.is_demo or order_id is None or exc.status_code != 404:
                raise
            response = self._request(
                "GET",
                f"/api/v1/trading/info/{self._demo()}orders/{order_id}",
                request_class="read",
                response_capture=response_capture,
            )
        data = response.json()
        legacy_shape = isinstance(data, Mapping) and "statusID" in data and "status" not in data
        self._check_response_contract(
            "legacy-order-lookup" if legacy_shape else "order-lookup",
            data,
            severity="critical",
        )
        result = self._parse_order_result(data)
        if order_id is not None and result.order_id != str(order_id):
            raise EtoroValidationError(
                f"orders:lookup returned orderId {result.order_id}, expected {order_id}"
            )
        if reference_id is not None and result.reference_id not in (None, reference_id):
            raise EtoroValidationError(
                "orders:lookup returned a different referenceId than requested"
            )
        return result

    def lookup_close_order(self, order_id: str | int) -> OrderResult:
        """Fetch close-order status and affected positions by its order ID.

        eToro documents close orders separately from open orders at
        ``GET /api/v1/trading/info/{demo/}close-orders/{orderId}``. Do not
        fall back to ``orders:lookup``: that endpoint can 404 for an accepted
        close order.
        """
        close_id = self._id_string(order_id, "orderId")
        if close_id is None or not close_id.isdecimal() or int(close_id) <= 0:
            raise EtoroValidationError("close orderId must be a positive integer")
        captured: list[dict[str, Any]] = []
        try:
            response = self._request(
                "GET",
                f"/api/v1/trading/info/{self._demo()}close-orders/{close_id}",
                request_class="read",
                response_capture=captured,
            )
            return self._parse_close_order_result(response.json(), expected_order_id=close_id)
        finally:
            self.last_close_lookup_responses.extend(captured)

    @classmethod
    def _parse_close_order_result(
        cls, data: Any, *, expected_order_id: str
    ) -> OrderResult:
        """Parse the documented close-orders payload, failing on unknown IDs or status."""
        if not isinstance(data, Mapping):
            raise EtoroValidationError("close-orders payload must be an object")
        order_id = cls._id_string(data.get("orderID"), "orderID")
        if order_id != expected_order_id:
            raise EtoroValidationError(
                f"close-orders returned orderID={order_id!r}, expected {expected_order_id!r}"
            )
        status_raw = data.get("statusID")
        if isinstance(status_raw, bool) or not isinstance(status_raw, int):
            raise EtoroValidationError("close-orders statusID must be an integer")
        try:
            status = OrderStatus(status_raw)
        except ValueError as exc:
            raise EtoroValidationError(
                f"close-orders has unknown statusID={status_raw}"
            ) from exc

        raw_positions = data.get("positions")
        if not isinstance(raw_positions, list):
            raise EtoroValidationError("close-orders positions must be a list")
        position_ids: set[str] = set()
        for position in raw_positions:
            if not isinstance(position, Mapping):
                raise EtoroValidationError("close-orders position entry must be an object")
            position_id = cls._id_string(position.get("positionID"), "positionID")
            if position_id is None or not position_id.isdecimal() or int(position_id) <= 0:
                raise EtoroValidationError("close-orders positionID must be a positive integer")
            position_ids.add(position_id)
        if len(position_ids) > 1:
            raise EtoroValidationError("close-orders returned multiple position IDs")
        position_id = next(iter(position_ids), None)
        if status in FILLED_ORDER_STATUSES and position_id is None:
            raise EtoroValidationError(
                f"filled close order {order_id} has no affected position ID"
            )
        reference_id = cls._id_string(data.get("referenceID"), "referenceID")
        return OrderResult(
            order_id=order_id,
            reference_id=reference_id,
            status=status,
            position_id=position_id,
            reject_reason=data.get("errorMessage"),
            raw=dict(data),
        )

    def recover_by_reference_id(self, reference_id: str) -> OrderResult | None:
        """Recover a possibly-lost response by ``referenceId`` (DESIGN §6.H).

        Returns ``None`` if eToro has no record of the order at all -- which
        means the original request never reached the server and it is safe
        to (re)submit; any other result means it did, and must never be
        submitted again.

        Narrowed 2026-09-11, together with the :meth:`_parse_order_result`
        rewrite: only an HTTP-level :class:`EtoroValidationError` (one
        carrying a ``status_code``, i.e. the API itself answering "no such
        order") is read as "never received". Now that a *parse* failure
        raises the same exception type instead of silently defaulting, an
        un-narrowed ``except`` here would turn "I could not understand the
        broker's answer" into "the broker never got it" -- and authorise a
        duplicate live order. Parse failures propagate.
        """
        try:
            return self.lookup_order(reference_id=reference_id)
        except EtoroValidationError as exc:
            if exc.status_code is None:
                raise
            return None

    @staticmethod
    def _execution_rows(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        """``positionExecutions``, validated -- never silently treated as empty.

        An absent key is legitimately "no executions yet" (an order that has
        not filled has none). A key that is PRESENT but not a list of objects
        is a contract change, and is raised rather than skipped: silently
        reading zero executions off a malformed list is exactly how a filled
        order's ``positionId`` went missing in the first place.
        """
        raw = row.get("positionExecutions")
        if raw is None:
            return []
        if not isinstance(raw, (list, tuple)):
            raise EtoroValidationError(
                f"orders:lookup positionExecutions is not a list: {raw!r}"[:500]
            )
        for entry in raw:
            if not isinstance(entry, Mapping):
                raise EtoroValidationError(
                    f"orders:lookup positionExecutions entry is not an object: "
                    f"{entry!r}"[:500]
                )
        return list(raw)

    @staticmethod
    def _id_string(value: Any, field_name: str) -> str | None:
        """Accept broker IDs only as non-empty strings or integer numbers."""
        if value is None:
            return None
        if (
            isinstance(value, bool)
            or not isinstance(value, (str, int))
            or (isinstance(value, str) and not value.strip())
        ):
            raise EtoroValidationError(
                f"orders:lookup {field_name} must be a non-empty string or integer"
            )
        return str(value)

    @staticmethod
    def _first_float(
        sources: Sequence[tuple[str, Any]], *, field_name: str, order_id: Any
    ) -> float | None:
        """First non-``None`` candidate coerced to ``float``, or ``None``.

        ``sources`` is ``(path, value)`` pairs in precedence order. A value
        that is present but not numeric raises
        :class:`~nightwatch.etoro.errors.EtoroValidationError` naming the JSON
        path -- it is never skipped in favour of the next candidate and never
        swallowed into ``None``. "Absent everywhere" is the only way to get
        ``None`` out of this.
        """
        for path, value in sources:
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError) as exc:
                raise EtoroValidationError(
                    f"order {order_id} has a non-numeric {field_name} at "
                    f"{path}: {value!r}"[:500]
                ) from exc
        return None

    @classmethod
    def _parse_order_result(cls, data: Mapping[str, Any]) -> OrderResult:
        """Parse one ``orders:lookup`` payload (DESIGN §1.1, §6.H).

        Rewritten 2026-09-11 against the first REAL captured response
        (``data/probe_2026-09-11/order_latency_run2/RAW_orders_lookup_payload.json``).
        Every field below was previously read off a *guessed* flat shape, and
        all four guesses were wrong:

        =====================  ==========================================
        Old (guessed)          Real live shape
        =====================  ==========================================
        ``status`` as an int   ``status: {"id": 3, "name": "Filled", ...}``
        ``positionId``         ``positionExecutions[0].positionId``
        ``units``              ``requestedUnits`` / ``positionExecutions[0].openingData.units``
        ``executionPrice``     ``positionExecutions[0].openingData.avgPrice``
        =====================  ==========================================

        **The defaulting, not the key names, was the real bug.** The old
        version wrapped the status read in ``except (TypeError, ValueError):
        status = OrderStatus.RECEIVED`` -- so ``int({"id": 3, ...})`` raised,
        was caught, and every order in the app's history was reported as
        non-terminal. An order that filled in ~163ms was polled for 90s and
        booked as a timeout, and its position was left open and unclosable.
        A parse miss must therefore be loud or explicitly absent; it must
        never become a plausible default. This follows the required-vs-
        optional split :meth:`_parse_position` already reasons about (§10.7).

        **Required -- raises** :class:`~nightwatch.etoro.errors.EtoroValidationError`:

        * ``status``. There is no honest default for "I could not tell what
          the broker said about this order". ``RECEIVED`` in particular is a
          *non-terminal* status, so defaulting to it converts an unparseable
          payload into "keep polling, then time out" -- the failure mode
          above. An unknown numeric code raises too (it is a contract change,
          not a synonym for "received").
        * ``position_id`` **when, and only when, the order reports a fill**
          (:data:`~nightwatch.etoro.models.FILLED_ORDER_STATUSES`). This is
          the one genuinely position-identity-critical field: a filled open
          whose position id we did not capture is a position we can never
          close (DESIGN §4.3's close path is keyed on it), which is exactly
          what stranded two live demo positions. Before a fill there is
          legitimately no position yet, so ``None`` there is a fact, not a
          miss.

        **Optional -- ``None`` means "the broker did not state it"**, and a
        value that IS stated but unparseable still raises (see
        :meth:`_first_float`):

        * ``fill_units``, ``fill_price``, ``fill_amount_usd``. These carry no
          position identity and are all recoverable after the fact from the
          portfolio row that ``position_id`` gets us (``units``/``openRate``/
          ``amount`` on :class:`~nightwatch.etoro.models.Position`), so a
          payload that omits them costs us measurement, not control. Note
          ``requestedUnits``/``requestedAmount`` are *requested*, not
          *executed*, so they are used only as a last resort behind the
          execution's own ``openingData``/``closingData`` numbers.
        * ``order_id``, ``reference_id``, ``reject_reason``.

        Both the live nested shape and a flat ``status``/``positionId``/
        ``units``/``executionPrice`` shape are accepted -- eToro is already
        known to disagree with itself across endpoints on casing and nesting
        (see :class:`~nightwatch.etoro.models.TradeHistoryEntry`), so the
        flat spellings are kept as lower-precedence fallbacks. What is NOT
        kept is the silent default when neither matches.
        """
        row = data.get("order", data)
        if not isinstance(row, Mapping):
            raise EtoroValidationError(
                f"orders:lookup payload is not an object: {row!r}"[:500]
            )
        order_ids = [
            cls._id_string(row[key], key)
            for key in ("orderId", "orderID")
            if row.get(key) is not None
        ]
        if len(set(order_ids)) > 1:
            raise EtoroValidationError("orders:lookup has conflicting order ids")
        order_id = order_ids[0] if order_ids else None
        reference_id = cls._id_string(
            row.get("referenceId", row.get("referenceID")), "referenceId"
        )

        # -- status: required, never defaulted ------------------------------
        status_raw = row.get("status", row.get("orderStatus", row.get("statusID")))
        status_label: str | None = None
        if isinstance(status_raw, Mapping):
            # The real live shape: {"id": 3, "name": "Filled", "errorCode": 0}.
            status_label = status_raw.get("name")
            status_code = status_raw.get("id", status_raw.get("statusId"))
            if status_code is None:
                raise EtoroValidationError(f"order {order_id} status object has no 'id'")
        else:
            status_code = status_raw
        if status_code is None:
            raise EtoroValidationError(f"order {order_id} response is missing its status")
        try:
            if isinstance(status_code, bool):
                raise ValueError("boolean status")
            numeric_status = float(status_code)
            if not math.isfinite(numeric_status) or not numeric_status.is_integer():
                raise ValueError("status is not an integer")
            status = OrderStatus(int(numeric_status))
        except (TypeError, ValueError, OverflowError) as exc:
            raise EtoroValidationError(
                f"order {order_id} has an unparseable/unknown status "
                f"(label={status_label!r}); refusing to default it to RECEIVED"
            ) from exc
        if status_label is not None and not isinstance(status_label, str):
            raise EtoroValidationError(
                f"order {order_id} status name is not a string: {status_label!r}"[:500]
            )

        executions = cls._execution_rows(row)
        legacy_positions: list[Mapping[str, Any]] = []
        if "statusID" in row and "status" not in row:
            raw_positions = row.get("positions")
            if not isinstance(raw_positions, list) or any(
                not isinstance(item, Mapping) for item in raw_positions
            ):
                raise EtoroValidationError("legacy order positions must be a list of objects")
            legacy_positions = list(raw_positions)
        execution = executions[0] if executions else {}
        opening = execution.get("openingData")
        closing = execution.get("closingData")
        if opening is None:
            opening = {}
        if closing is None:
            closing = {}
        if not isinstance(opening, Mapping) or not isinstance(closing, Mapping):
            raise EtoroValidationError(
                f"order {order_id} positionExecutions[0] opening/closingData is "
                f"not an object: {dict(execution)!r}"[:500]
            )

        # -- position id: required once the order has actually filled -------
        position_ids = [
            cls._id_string(value, "positionId")
            for value in (
                row.get("positionId"),
                row.get("positionID"),
                *(candidate.get("positionId") for candidate in executions),
                *(candidate.get("positionID") for candidate in executions),
                *(candidate.get("positionID") for candidate in legacy_positions),
            )
            if value is not None
        ]
        distinct_position_ids = set(position_ids)
        if len(distinct_position_ids) > 1:
            raise EtoroValidationError(
                f"order {order_id} has multiple position ids in positionExecutions"
            )
        position_id = position_ids[0] if position_ids else None
        if position_id is None and status in FILLED_ORDER_STATUSES:
            raise EtoroValidationError(
                f"order {order_id} reports {status.name} but carries no "
                "positionId in the response"
            )

        fill_units = cls._first_float(
            [
                ("positionExecutions[0].openingData.units", opening.get("units")),
                ("positionExecutions[0].closingData.units", closing.get("units")),
                ("units", row.get("units")),
                # Requested, not executed -- last resort only.
                ("requestedUnits", row.get("requestedUnits")),
            ],
            field_name="units",
            order_id=order_id,
        )
        fill_price = cls._first_float(
            [
                ("positionExecutions[0].openingData.avgPrice", opening.get("avgPrice")),
                ("positionExecutions[0].closingData.avgPrice", closing.get("avgPrice")),
                ("positions[0].rate", legacy_positions[0].get("rate") if legacy_positions else None),
                ("executionPrice", row.get("executionPrice")),
            ],
            field_name="fill price",
            order_id=order_id,
        )
        fill_amount_usd = cls._first_float(
            [
                ("amount", row.get("amount")),
                (
                    "positionExecutions[0].initialExposureAccountCurrency",
                    execution.get("initialExposureAccountCurrency"),
                ),
                # Requested, not executed -- last resort only.
                ("requestedAmount", row.get("requestedAmount")),
            ],
            field_name="amount",
            order_id=order_id,
        )

        reject_reason = row.get("rejectReason")
        if reject_reason is None and row.get("errorCode") not in (None, 0):
            reject_reason = f"errorCode={row['errorCode']}"
        if reject_reason is None and isinstance(status_raw, Mapping):
            error_code = status_raw.get("errorCode")
            if error_code:  # a real, non-zero broker error code
                reject_reason = f"errorCode={error_code}"

        return OrderResult(
            order_id=order_id,
            reference_id=reference_id,
            status=status,
            position_id=(str(position_id) if position_id is not None else None),
            fill_price=fill_price,
            fill_units=fill_units,
            fill_amount_usd=fill_amount_usd,
            reject_reason=reject_reason,
            status_label=status_label,
            raw={
                key: value
                for key, value in row.items()
                if key not in ("token", "CID", "gcid", "accountId")
            },
        )

    def poll_order_to_terminal(
        self,
        *,
        order_id: str | None = None,
        reference_id: str | None = None,
        interval_seconds: float | None = None,
        timeout_seconds: float | None = None,
        kind: str = "open",
    ) -> PollResult:
        """Poll open or close orders until a terminal status or a timeout.

        DESIGN §1.1/§6.H: HTTP 200 on submission means *accepted*, not
        *filled*. Status 11 (WaitingForMarket) is explicitly "queued, not
        filled" and is never treated as terminal here -- polling continues
        (bounded by ``timeout_seconds``) rather than returning early on it.
        A timeout returns the last observed status with ``timed_out=True``;
        it is the caller's job to never read that as a fill (`OrderResult`'s
        own `is_filled` already encodes this, since WAITING_FOR_MARKET and
        every non-terminal status are excluded from `FILLED_ORDER_STATUSES`).
        """
        if kind not in ("open", "close"):
            raise ValueError("poll_order_to_terminal: kind must be 'open' or 'close'")
        if kind == "close" and not order_id:
            raise ValueError("poll_order_to_terminal: kind='close' requires order_id")
        interval = interval_seconds if interval_seconds is not None else self._poll_interval
        timeout = timeout_seconds if timeout_seconds is not None else self._poll_timeout
        deadline = self._clock() + timeout
        attempts = 0
        result = (
            self.lookup_close_order(order_id) if kind == "close"
            else self.lookup_order(order_id=order_id, reference_id=reference_id)
        )
        attempts += 1
        while not result.is_terminal:
            if self._clock() >= deadline:
                return PollResult(order=result, attempts=attempts, timed_out=True)
            self._sleep(interval)
            result = (
                self.lookup_close_order(order_id) if kind == "close"
                else self.lookup_order(
                    order_id=order_id or result.order_id, reference_id=reference_id
                )
            )
            attempts += 1
        if result.status in (OrderStatus.REJECTED, OrderStatus.REJECTED_PARTIALLY_FILLED):
            _logger.warning(
                "order rejected: order_id=%s reference_id=%s reason=%s",
                order_id, reference_id, result.reject_reason,
            )
        return PollResult(order=result, attempts=attempts, timed_out=False)
