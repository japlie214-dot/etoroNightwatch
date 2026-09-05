"""Tests for nightwatch.etoro.client (DESIGN §1.1, §4.3, §5, §6).

Entirely offline: every test drives ``EtoroClient`` through an injected
``httpx.MockTransport`` (or a fake clock/sleep), so no test here ever makes a
real network call. Several tests are written specifically to *break* the
structural safety guarantees (DESIGN's own instruction), not merely to
confirm the happy path.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import logging
import uuid
from typing import Any, Callable

import httpx
import pytest

from nightwatch.config import Credentials, EtoroConfig, EtoroEnvironment, MissingCredentialError
from nightwatch.db.repo import Repo
from nightwatch.etoro.client import MAX_LEVERAGE, SETTLEMENT_TYPE, TRANSACTION, EtoroClient
from nightwatch.etoro.errors import (
    EtoroAuthError,
    EtoroEligibilityError,
    EtoroEnvironmentMismatchError,
    EtoroServerError,
    EtoroValidationError,
)
from nightwatch.etoro.models import OrderRequest, OrderStatus, mark_position
from nightwatch.host import DesktopHost
from nightwatch.logging_setup import configure_logging, log_path_for_today

DEMO_API_KEY = "demo_private_key_for_tests_1234567890"
DEMO_USER_KEY = "demo_public_key_for_tests_0987654321"


def _config(
    environment: EtoroEnvironment = EtoroEnvironment.DEMO,
    secrets: dict[str, str] | None = None,
) -> EtoroConfig:
    secrets = secrets if secrets is not None else {
        "ETORO_DEMO_PRIVATE_KEY": DEMO_API_KEY,
        "ETORO_PUBLIC_KEY": DEMO_USER_KEY,
    }
    return EtoroConfig(environment=environment, secrets=secrets)


class ScriptedTransport:
    """A queue of canned responses (or exception-raising callables), FIFO.

    Records every request made against it so tests can assert on the number
    of HTTP attempts and on request bodies/headers.
    """

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


class Counter:
    """A deterministic fake monotonic clock: starts at `start`, +`step` per call."""

    def __init__(self, start: float = 0.0, step: float = 1.0) -> None:
        self.value = start
        self.step = step

    def __call__(self) -> float:
        v = self.value
        self.value += self.step
        return v


def _no_sleep(_seconds: float) -> None:
    pass


# ===========================================================================
# Environment/credential structural safety -- actively try to break it
# ===========================================================================


def test_cannot_construct_client_when_configured_env_has_no_matching_secrets() -> None:
    """Config says REAL but only demo secrets are present in .secrets.env --
    write_credentials must raise before EtoroClient ever gets a chance to
    make an HTTP call with a nonexistent credential pair."""
    config = _config(
        environment=EtoroEnvironment.REAL,
        secrets={"ETORO_DEMO_PRIVATE_KEY": "x", "ETORO_PUBLIC_KEY": "y"},
    )
    with pytest.raises(MissingCredentialError):
        EtoroClient(config)


def test_client_constructor_exposes_no_raw_credential_or_environment_override() -> None:
    """The only way to influence which credentials/environment a client uses
    is through `config` (+ `role`) -- there must be no api_key=/user_key=/
    environment=/base_url= parameter a caller could use to smuggle a
    mismatched pair past EtoroConfig's own derivation."""
    sig = inspect.signature(EtoroClient.__init__)
    forbidden = {"api_key", "user_key", "credentials", "environment", "base_url"}
    assert forbidden.isdisjoint(sig.parameters)


def test_etoro_config_environment_field_is_frozen() -> None:
    """Cannot mutate a live config's environment after handing it to a client."""
    config = _config()
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.environment = EtoroEnvironment.REAL  # type: ignore[misc]


def test_belt_and_braces_check_rejects_a_config_whose_credentials_lie() -> None:
    """Actively try to break the structural guarantee: hand the client a
    config-like object whose `write_credentials` claims a "real" label while
    `environment` says demo. EtoroClient must refuse construction rather than
    trust that the two agree -- DESIGN §7's "should be unreachable is not the
    same guarantee as is checked."
    """
    base = _config()

    class LyingConfig:
        environment = EtoroEnvironment.DEMO
        base_url = base.base_url
        timeout_seconds = base.timeout_seconds
        path_segment = "demo/"

        @property
        def write_credentials(self) -> Credentials:
            return Credentials(label="real:write", api_key="a" * 20, user_key="b" * 20)

        @property
        def read_credentials(self) -> Credentials:
            return self.write_credentials

    with pytest.raises(EtoroEnvironmentMismatchError):
        EtoroClient(LyingConfig())  # type: ignore[arg-type]


def test_unknown_role_is_rejected() -> None:
    with pytest.raises(ValueError):
        EtoroClient(_config(), role="both")


def test_real_environment_uses_write_vs_read_credentials_by_role() -> None:
    """Real environment splits write/read privilege (DESIGN §6.F) -- exercise
    both roles resolve to their own, distinct key pair without error."""
    secrets = {
        "ETORO_PRIVATE_KEY": "real_write_private_key_1234567890",
        "ETORO_READ_PRIVATE_KEY": "real_read_private_key_1234567890",
        "ETORO_PUBLIC_KEY": "real_shared_public_key_1234567890",
    }
    config = _config(environment=EtoroEnvironment.REAL, secrets=secrets)
    write_client = EtoroClient(config, role="write")
    read_client = EtoroClient(config, role="read")
    # Corrected 2026-09-04: x-api-key (Credentials.api_key) takes the short
    # public/application key, and x-user-key (Credentials.user_key) takes
    # the long per-user private key -- proven by a live GET /api/v1/balances
    # (see config.py's _CREDENTIAL_KEYS docstring). The public key is shared
    # across roles; the private key is split by privilege.
    assert write_client._credentials.api_key == secrets["ETORO_PUBLIC_KEY"]
    assert read_client._credentials.api_key == secrets["ETORO_PUBLIC_KEY"]
    assert write_client._credentials.user_key == secrets["ETORO_PRIVATE_KEY"]
    assert read_client._credentials.user_key == secrets["ETORO_READ_PRIVATE_KEY"]


# ===========================================================================
# Leverage / transaction / settlementType cannot be overridden -- break it
# ===========================================================================


def test_place_buy_order_signature_has_no_safety_override_parameters() -> None:
    sig = inspect.signature(EtoroClient.place_buy_order)
    forbidden = {"leverage", "transaction", "settlement_type", "settlementType"}
    assert forbidden.isdisjoint(sig.parameters)


def test_place_buy_order_always_sends_the_hardcoded_safety_fields() -> None:
    scripted = ScriptedTransport(
        [httpx.Response(200, json={"orderId": "o-1", "status": 1})]
    )
    client = EtoroClient(
        _config(), transport=httpx.MockTransport(scripted.handler), sleep=_no_sleep
    )
    client.place_buy_order(
        request_id=str(uuid.uuid4()), instrument_id=42, amount_usd=100.0
    )
    assert len(scripted.requests) == 1
    body = json.loads(scripted.requests[0].content)
    assert body["leverage"] == MAX_LEVERAGE == 1
    assert body["transaction"] == TRANSACTION == "buy"
    assert body["settlementType"] == SETTLEMENT_TYPE == "real"
    assert body["action"] == "open"


def test_order_request_cannot_be_built_with_leverage_above_one() -> None:
    """The second, independent line of defense (DESIGN §6.A): even
    constructing the request body object directly and trying to override
    leverage raises before any HTTP call is made."""
    with pytest.raises(ValueError):
        OrderRequest(instrument_id=1, amount=10.0, leverage=3)


def test_order_request_cannot_be_built_as_a_short() -> None:
    with pytest.raises(ValueError):
        OrderRequest(instrument_id=1, amount=10.0, transaction="sellShort")


# ===========================================================================
# Available-cash formula correctness (DESIGN §1.1, §4.5)
#
# Corrected 2026-09-05 (SEVERE, real-account live probe): the formula's
# inputs (`credit`/`orders`/`ordersForOpen`) do not live on `/balances` --
# confirmed live to carry only `totalBalance` and a `balances` list -- they
# live on the portfolio endpoint's `clientPortfolio` envelope instead.
# `get_balance` now reads that envelope via `_fetch_portfolio_envelope`
# (shared with `get_portfolio`), never `/balances`, for exactly this reason.
# ===========================================================================


def test_get_balance_computes_available_cash_from_real_portfolio_envelope_shape() -> None:
    """Fixture shape matches the real, live `clientPortfolio` envelope
    (DESIGN_market_data.md §10.8) with genuinely populated `credit`/`orders`/
    `ordersForOpen` -- the confirmed live values from the 2026-09-05 probe
    (`credit=1514.59`, one `orders` row `amount=250.00`, `ordersForOpen=[]`,
    `available = 1514.59 - 250.00 = 1264.59`), plus an extra `ordersForOpen`
    row to also exercise the `mirrorID` filter against a non-empty case."""
    sample = {
        "clientPortfolio": {
            "positions": [],
            "mirrors": [],
            "credit": 1514.59,
            "orders": [{"amount": 250.00}],
            "ordersForOpen": [
                {"amount": 300.0, "mirrorID": 0},
                {"amount": 1000.0, "mirrorID": 3},  # excluded: mirrorID != 0
            ],
        }
    }
    scripted = ScriptedTransport([httpx.Response(200, json=sample)])
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    balance = client.get_balance()
    assert balance.credit == pytest.approx(1514.59)
    # total_balance is no longer sourced (the portfolio envelope carries no
    # equivalent field, and /balances is no longer called) -- must not be
    # fabricated from anything in this payload.
    assert balance.total_balance is None
    assert balance.available_cash == pytest.approx(1514.59 - 250.00 - 300.0)


def test_get_balance_raises_rather_than_silently_zero_on_the_real_balances_shape() -> None:
    """The exact regression this bug was: if something upstream ever fed
    `get_balance` a `/balances`-shaped body (only `totalBalance` + a
    `balances` list, no `credit`/`orders`/`ordersForOpen` -- this is the
    *actual*, confirmed live `/balances` response shape for this real
    account) it must raise, never silently compute `available_cash = 0.0`.
    """
    real_balances_shape = {
        "totalBalance": 12345.67,
        "balances": [{"accountId": 1, "amount": 12345.67}],
    }
    scripted = ScriptedTransport([httpx.Response(200, json=real_balances_shape)])
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    with pytest.raises(EtoroValidationError):
        client.get_balance()


def test_get_balance_and_get_portfolio_share_the_same_endpoint() -> None:
    """Both now read the portfolio endpoint (not two different endpoints) --
    confirm `get_balance` never touches `/balances` any more."""
    scripted = ScriptedTransport(
        [
            httpx.Response(
                200,
                json={
                    "clientPortfolio": {
                        "positions": [],
                        "credit": 42.0,
                        "orders": [],
                        "ordersForOpen": [],
                    }
                },
            )
        ]
    )
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    client.get_balance()
    assert len(scripted.requests) == 1
    assert "portfolio" in str(scripted.requests[0].url)
    assert "balances" not in str(scripted.requests[0].url)


# ===========================================================================
# Order status handling -- status 11 (WaitingForMarket) never a fill
# ===========================================================================


def test_poll_to_terminal_stops_at_a_real_fill_not_at_waiting_for_market() -> None:
    responses = [
        httpx.Response(
            200, json={"orderId": "o1", "referenceId": "r1", "status": 11}
        ),
        httpx.Response(
            200, json={"orderId": "o1", "referenceId": "r1", "status": 11}
        ),
        httpx.Response(
            200,
            json={
                "orderId": "o1",
                "referenceId": "r1",
                "status": 3,
                "units": 1.5,
                "executionPrice": 66.0,
            },
        ),
    ]
    scripted = ScriptedTransport(responses)
    client = EtoroClient(
        _config(),
        transport=httpx.MockTransport(scripted.handler),
        sleep=_no_sleep,
        clock=Counter(start=0.0, step=1.0),
    )
    result = client.poll_order_to_terminal(
        reference_id="r1", interval_seconds=1.0, timeout_seconds=30.0
    )
    assert result.timed_out is False
    assert result.order.status is OrderStatus.FILLED
    assert result.order.is_filled is True
    assert result.attempts == 3


def test_poll_timeout_on_a_still_queued_order_never_reports_a_fill() -> None:
    responses = [
        httpx.Response(200, json={"orderId": "o2", "referenceId": "r2", "status": 11}),
        httpx.Response(200, json={"orderId": "o2", "referenceId": "r2", "status": 11}),
    ]
    scripted = ScriptedTransport(responses)
    client = EtoroClient(
        _config(),
        transport=httpx.MockTransport(scripted.handler),
        sleep=_no_sleep,
        clock=Counter(start=0.0, step=5.0),
    )
    result = client.poll_order_to_terminal(
        reference_id="r2", interval_seconds=1.0, timeout_seconds=9.0
    )
    assert result.timed_out is True
    assert result.order.status is OrderStatus.WAITING_FOR_MARKET
    assert result.order.is_filled is False
    assert result.order.is_terminal is False


def test_rejected_status_is_terminal_and_never_a_fill() -> None:
    scripted = ScriptedTransport(
        [
            httpx.Response(
                200,
                json={
                    "orderId": "o3",
                    "referenceId": "r3",
                    "status": 4,
                    "rejectReason": "insufficient funds",
                },
            )
        ]
    )
    client = EtoroClient(
        _config(),
        transport=httpx.MockTransport(scripted.handler),
        sleep=_no_sleep,
        clock=Counter(),
    )
    result = client.poll_order_to_terminal(reference_id="r3")
    assert result.timed_out is False
    assert result.order.status is OrderStatus.REJECTED
    assert result.order.is_filled is False
    assert result.order.reject_reason == "insufficient funds"


# ===========================================================================
# Rate limiting integration
# ===========================================================================


def test_429_response_honours_retry_after_header() -> None:
    scripted = ScriptedTransport(
        [
            httpx.Response(429, headers={"Retry-After": "5"}, json={"error": "rate limited"}),
            httpx.Response(200, json={"credit": 50.0}),
        ]
    )
    sleeps: list[float] = []
    client = EtoroClient(
        _config(), transport=httpx.MockTransport(scripted.handler), sleep=sleeps.append
    )
    balance = client.get_balance()
    assert balance.credit == 50.0
    assert sleeps == [5.0]
    assert len(scripted.requests) == 2


def test_reconcile_from_rate_limit_headers_tightens_local_bucket() -> None:
    scripted = ScriptedTransport(
        [httpx.Response(200, json={"credit": 1.0}, headers={"RateLimit-Remaining": "2"})]
    )
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    client.get_balance()
    assert client._limiter.bucket("read").available() == pytest.approx(2, abs=0.01)


# ===========================================================================
# Retry logic: 5xx retried, 429 retried, other 4xx never retried
# ===========================================================================


def test_5xx_is_retried_with_backoff_then_succeeds() -> None:
    scripted = ScriptedTransport(
        [httpx.Response(502, text="bad gateway"), httpx.Response(200, json={"credit": 100.0})]
    )
    sleeps: list[float] = []
    client = EtoroClient(
        _config(), transport=httpx.MockTransport(scripted.handler), sleep=sleeps.append
    )
    balance = client.get_balance()
    assert balance.credit == 100.0
    assert len(scripted.requests) == 2
    assert len(sleeps) == 1


def test_5xx_exhausting_retries_raises_server_error() -> None:
    scripted = ScriptedTransport([httpx.Response(503, text="down")] * 5)
    client = EtoroClient(
        _config(),
        transport=httpx.MockTransport(scripted.handler),
        sleep=_no_sleep,
        max_attempts=5,
    )
    with pytest.raises(EtoroServerError):
        client.get_balance()
    assert len(scripted.requests) == 5


def test_other_4xx_is_never_retried() -> None:
    scripted = ScriptedTransport([httpx.Response(400, json={"error": "bad request"})])
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    with pytest.raises(EtoroValidationError):
        client.get_balance()
    assert len(scripted.requests) == 1


def test_401_raises_auth_error_and_is_never_retried() -> None:
    scripted = ScriptedTransport([httpx.Response(401, json={"error": "unauthorized"})])
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    with pytest.raises(EtoroAuthError):
        client.get_balance()
    assert len(scripted.requests) == 1


def test_network_error_is_retried_then_succeeds() -> None:
    calls = {"n": 0}

    def flaky_handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"credit": 10.0})

    client = EtoroClient(
        _config(), transport=httpx.MockTransport(flaky_handler), sleep=_no_sleep
    )
    balance = client.get_balance()
    assert balance.credit == 10.0
    assert calls["n"] == 2


# ===========================================================================
# Secrets never leak into a raised exception or a captured log line
# ===========================================================================


def test_client_never_leaks_credentials_in_error_message_or_log(host: DesktopHost) -> None:
    configure_logging(host=host)
    api_key = "api_key_should_never_leak_1234567890"
    user_key = "user_key_should_never_leak_0987654321"
    config = _config(
        secrets={"ETORO_DEMO_PRIVATE_KEY": api_key, "ETORO_PUBLIC_KEY": user_key}
    )
    # A misbehaving server that echoes the request's own headers back in its
    # error body -- the worst case this redaction exists for.
    scripted = ScriptedTransport(
        [
            httpx.Response(
                400,
                text=f"bad request; saw x-api-key={api_key} x-user-key={user_key}",
            )
        ]
    )
    client = EtoroClient(config, transport=httpx.MockTransport(scripted.handler))
    logger = logging.getLogger("nightwatch.etoro.client.test")
    try:
        with pytest.raises(EtoroValidationError) as excinfo:
            client.get_balance()
        text = str(excinfo.value)
        assert api_key not in text
        assert user_key not in text
        logger.error("balance call failed: %s", text)
    finally:
        logging.shutdown()
    log_path = log_path_for_today(host)
    contents = log_path.read_text(encoding="utf-8")
    assert api_key not in contents
    assert user_key not in contents
    assert "REDACTED" in contents


# ===========================================================================
# Idempotency: lost-response recovery via referenceId, never a resubmission
# ===========================================================================


def test_lost_response_is_recovered_by_reference_id_never_resubmitted() -> None:
    request_id = str(uuid.uuid4())

    def handler(request: httpx.Request) -> httpx.Response:
        if "orders:lookup" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "orderId": "o-99",
                    "referenceId": request_id,
                    "status": 3,
                    "units": 2.0,
                    "executionPrice": 55.0,
                },
            )
        # Any attempt to submit again instead of recovering is exactly the
        # bug this idempotency design exists to prevent.
        raise AssertionError("order was resubmitted instead of recovered by referenceId")

    client = EtoroClient(_config(), transport=httpx.MockTransport(handler), sleep=_no_sleep)
    # Simulate: the original submit's HTTP response never arrived (crash,
    # dropped connection). Recovery goes straight to orders:lookup using the
    # already-persisted request_id/referenceId -- it never re-POSTs to the
    # orders endpoint.
    recovered = client.recover_by_reference_id(request_id)
    assert recovered is not None
    assert recovered.status is OrderStatus.FILLED
    assert recovered.reference_id == request_id
    assert recovered.fill_units == 2.0


def test_recover_by_reference_id_returns_none_when_truly_never_received() -> None:
    scripted = ScriptedTransport([httpx.Response(404, json={"error": "not found"})])
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    assert client.recover_by_reference_id("unknown-ref") is None


def test_submit_order_requires_caller_supplied_request_id_not_generated_here() -> None:
    """Idempotency (DESIGN §6.H): the client never mints its own request id
    for a write -- the caller must have already persisted one. Confirmed via
    the actual request headers sent, not just the signature."""
    scripted = ScriptedTransport(
        [httpx.Response(200, json={"orderId": "o-1", "status": 1})]
    )
    client = EtoroClient(
        _config(), transport=httpx.MockTransport(scripted.handler), sleep=_no_sleep
    )
    my_request_id = str(uuid.uuid4())
    client.place_buy_order(
        request_id=my_request_id, instrument_id=1, amount_usd=50.0
    )
    sent_headers = scripted.requests[0].headers
    assert sent_headers["x-request-id"] == my_request_id


# ===========================================================================
# Eligibility (gate 8): batched at <=100, blocked never resized
# ===========================================================================


def test_eligibility_requests_are_batched_at_100() -> None:
    calls: list[list[int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        ids = body["instrumentIds"]
        calls.append(ids)
        return httpx.Response(
            200,
            json={
                # "eligibilities" -- the real top-level key (corrected
                # 2026-09-04; the old fixture used "eligibility", which
                # matched the client's old, wrong parsing but not eToro's
                # real response).
                "eligibilities": [
                    {"instrumentId": i, "allowOpenPosition": True} for i in ids
                ]
            },
        )

    client = EtoroClient(_config(), transport=httpx.MockTransport(handler), sleep=_no_sleep)
    ids = list(range(1, 151))  # 150 ids -> two batches of 100 and 50
    result = client.get_eligibility(ids)
    assert len(calls) == 2
    assert len(calls[0]) == 100
    assert len(calls[1]) == 50
    assert len(result.results) == 150


def test_order_below_minimum_size_is_blocked_never_resized() -> None:
    scripted = ScriptedTransport(
        [
            httpx.Response(
                200,
                json={
                    "eligibilities": [
                        {
                            "instrumentId": 7,
                            "allowOpenPosition": True,
                            "minPositionExposure": 50.0,
                        }
                    ]
                },
            )
        ]
    )
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    with pytest.raises(EtoroEligibilityError):
        client.check_eligibility_for_order(7, order_size=10.0)


def test_instrument_not_open_for_new_positions_is_blocked() -> None:
    scripted = ScriptedTransport(
        [httpx.Response(200, json={"eligibilities": [{"instrumentId": 7, "allowOpenPosition": False}]})]
    )
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    with pytest.raises(EtoroEligibilityError):
        client.check_eligibility_for_order(7, order_size=1000.0)


# ===========================================================================
# Instrument resolution + DB caching (immutable ids -- cache forever)
# ===========================================================================


def test_resolve_instrument_id_caches_in_the_database_not_just_memory(repo: Repo) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # "items" -- the real top-level key from /api/v1/market-data/search
        # (corrected 2026-09-04); "internalSymbolFull" must echo the query
        # so search_instrument's exact-match filter accepts this row.
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "instrumentId": 123,
                        "internalSymbolFull": "SPY",
                        "internalInstrumentDisplayName": "SPDR S&P 500",
                    }
                ]
            },
        )

    client = EtoroClient(_config(), transport=httpx.MockTransport(handler), repo=repo)
    first = client.resolve_instrument_id("SPY")
    assert first == 123
    assert calls["n"] == 1

    # A brand new client sharing the same repo must hit the DB cache, not the
    # network -- proving this is a persistent cache, not per-instance memory.
    client2 = EtoroClient(_config(), transport=httpx.MockTransport(handler), repo=repo)
    second = client2.resolve_instrument_id("SPY")
    assert second == 123
    assert calls["n"] == 1


# ===========================================================================
# Trade history (DESIGN_market_data.md §10.5, corrected 2026-09-05 against
# a real-account live probe):
#
# - a >1yr window is no longer rejected -- the live API has no such limit
#   (a single MinDate=1970-01-01 call returned 6 years of real history);
# - MinDate/MaxDate are the real query parameter names, not startDate/
#   endDate (the live API 400s on the old names);
# - MaxDate is confirmed ignored server-side, so end_date is now enforced
#   locally, in Python, after the fetch.
# ===========================================================================


def test_trade_history_sends_mindate_maxdate_not_startdate_enddate() -> None:
    """The live API 400s with 'Missing required query parameter [MinDate]'
    on the old startDate/endDate names -- this is the core parameter-name
    regression test."""
    scripted = ScriptedTransport([httpx.Response(200, json={"items": []})])
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    client.get_trade_history(start_date="2020-01-01", end_date="2026-08-31")
    sent_params = dict(scripted.requests[0].url.params)
    assert sent_params["MinDate"] == "2020-01-01"
    assert sent_params["MaxDate"] == "2026-08-31"
    assert "startDate" not in sent_params
    assert "endDate" not in sent_params


def test_trade_history_no_longer_rejects_a_window_over_a_year() -> None:
    """DESIGN_market_data.md §10.5 is closed: a live probe proved the
    endpoint is not windowed at all (six years' worth of history returned in
    one call), so the old <1yr guard -- based on a documented limit that
    turned out to be false -- must not raise here any more."""
    scripted = ScriptedTransport([httpx.Response(200, json={"items": []})])
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    entries = client.get_trade_history(start_date="1970-01-01", end_date="2026-08-31")
    assert entries == []


def test_trade_history_pages_until_a_short_page() -> None:
    pages = iter(
        [
            [
                {
                    "positionId": i + 1,
                    "instrumentId": 1,
                    "units": 1.0,
                    "openRate": 20.0,
                    "closeTimestamp": "2026-02-01T00:00:00Z",
                }
                for i in range(200)
            ],
            [
                {
                    "positionId": 1000 + i,
                    "instrumentId": 1,
                    "units": 1.0,
                    "openRate": 20.0,
                    "closeTimestamp": "2026-02-01T00:00:00Z",
                }
                for i in range(5)
            ],
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": next(pages)})

    client = EtoroClient(_config(), transport=httpx.MockTransport(handler))
    entries = client.get_trade_history(
        start_date="2026-01-01", end_date="2026-03-01", page_size=200
    )
    assert len(entries) == 205


def test_trade_history_full_120_row_real_account_response_in_a_single_page() -> None:
    """Realistic, populated payload matching the live probe exactly: a
    single call (large pageSize, no server-side pagination needed) returned
    this real account's complete 120-row closed-trade history, spanning
    close dates 2020-12-21..2026-08-31 -- not an empty or trivially small
    fixture.

    Field names here are the real, live-confirmed ones (2026-09-05
    correction): lowercase ``positionId``/``instrumentId`` (trade history's
    own casing -- see :class:`~nightwatch.etoro.models.TradeHistoryEntry`'s
    docstring for why this differs from the portfolio endpoint), ``netProfit``,
    ``openTimestamp``/``closeTimestamp``, plus ``investment``/``fees``/
    ``isBuy``/``leverage``. There is no ``symbol`` or ``action`` key -- those
    do not exist on a real row."""
    rows = [
        {
            "positionId": 3000000000 + i,
            "instrumentId": 100000 + (i % 3),
            "isBuy": True,
            "leverage": 1,
            "units": 1.0 + i * 0.01,
            "openRate": 20.0,
            "closeRate": 25.0,
            "netProfit": 5.0,
            "investment": 20.0,
            "fees": -0.5,
            "openTimestamp": "2020-12-01T00:00:00Z",
            "closeTimestamp": "2020-12-21T00:00:00Z" if i == 0 else "2026-08-31T00:00:00Z",
        }
        for i in range(120)
    ]
    scripted = ScriptedTransport([httpx.Response(200, json={"items": rows})])
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    entries = client.get_trade_history(
        start_date="1970-01-01", end_date="2026-08-31", page_size=500
    )
    assert len(entries) == 120  # not empty -- the "didn't error, not a pass" trap
    assert len(scripted.requests) == 1  # one call, no pagination needed
    assert entries[0].close_date == "2020-12-21T00:00:00Z"
    assert entries[-1].close_date == "2026-08-31T00:00:00Z"
    # No field silently comes back None where the real payload always
    # carries a value.
    for entry in entries:
        assert entry.position_id is not None
        assert entry.instrument_id is not None
        assert entry.profit is not None
        assert entry.investment is not None
        assert entry.fees is not None
        assert entry.open_date is not None
        assert entry.close_date is not None
    assert not hasattr(entries[0], "symbol")
    assert not hasattr(entries[0], "action")


def test_trade_history_entry_matches_real_account_row_shape() -> None:
    """One representative real row (copied verbatim from a live 120-row
    probe, 2026-09-05) parses with every genuinely-present field populated,
    proving the fix against the exact shape the broker sends -- not just a
    synthetic fixture with the right key names."""
    real_row = {
        "netProfit": 12.97,
        "closeRate": 100.79,
        "closeTimestamp": "2026-08-31T13:30:47.147Z",
        "positionId": 3240756580,
        "instrumentId": 4375,
        "isBuy": True,
        "leverage": 1,
        "openRate": 100.3562,
        "openTimestamp": "2026-01-02T14:30:27.333Z",
        "orderId": 1289166240,
        "investment": 2999.89,
        "initialInvestment": 2999.89,
        "fees": -57.9,
        "units": 29.892387,
    }
    scripted = ScriptedTransport([httpx.Response(200, json={"items": [real_row]})])
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    entries = client.get_trade_history(start_date="1970-01-01", end_date="2026-08-31")
    assert len(entries) == 1
    entry = entries[0]
    assert entry.position_id == "3240756580"
    assert entry.instrument_id == 4375
    assert entry.units == pytest.approx(29.892387)
    assert entry.is_buy is True
    assert entry.leverage == 1
    assert entry.open_rate == pytest.approx(100.3562)
    assert entry.close_rate == pytest.approx(100.79)
    assert entry.profit == pytest.approx(12.97)
    assert entry.investment == pytest.approx(2999.89)
    assert entry.fees == pytest.approx(-57.9)
    assert entry.open_date == "2026-01-02T14:30:27.333Z"
    assert entry.close_date == "2026-08-31T13:30:47.147Z"


def test_trade_history_entry_raises_on_missing_position_id() -> None:
    """Matches ``_parse_position``'s corrected precedent (§10.7): a missing
    required field is a broker-contract change, not a silent ``None``/``0.0``
    default."""
    scripted = ScriptedTransport(
        [
            httpx.Response(
                200,
                json={"items": [{"instrumentId": 1, "units": 1.0, "openRate": 20.0}]},
            )
        ]
    )
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    with pytest.raises(EtoroValidationError):
        client.get_trade_history(start_date="1970-01-01", end_date="2026-08-31")


def test_trade_history_entry_preserves_a_position_id_of_zero() -> None:
    """Same falsy-zero trap as `_parse_position` (§10.7's precedent): an
    `or`-chain over `positionId`/`positionID`/`id` would treat a genuine
    `positionId: 0` as missing and fall through -- or pick up a wrong
    fallback value -- instead of preserving it."""
    scripted = ScriptedTransport(
        [
            httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "positionId": 0,
                            "positionID": "wrong-fallback-id",
                            "id": "also-wrong",
                            "instrumentId": 1,
                            "units": 1.0,
                            "openRate": 20.0,
                        }
                    ]
                },
            )
        ]
    )
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    entries = client.get_trade_history(start_date="1970-01-01", end_date="2026-08-31")
    assert len(entries) == 1
    assert entries[0].position_id == "0"


def test_trade_history_entry_raises_on_missing_units_or_open_rate() -> None:
    scripted_missing_units = ScriptedTransport(
        [
            httpx.Response(
                200,
                json={
                    "items": [
                        {"positionId": 1, "instrumentId": 1, "openRate": 20.0}
                    ]
                },
            )
        ]
    )
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted_missing_units.handler))
    with pytest.raises(EtoroValidationError):
        client.get_trade_history(start_date="1970-01-01", end_date="2026-08-31")

    scripted_missing_open_rate = ScriptedTransport(
        [
            httpx.Response(
                200,
                json={"items": [{"positionId": 1, "instrumentId": 1, "units": 1.0}]},
            )
        ]
    )
    client = EtoroClient(
        _config(), transport=httpx.MockTransport(scripted_missing_open_rate.handler)
    )
    with pytest.raises(EtoroValidationError):
        client.get_trade_history(start_date="1970-01-01", end_date="2026-08-31")


def test_trade_history_filters_maxdate_locally_since_server_ignores_it() -> None:
    """Live-confirmed: a MinDate=2024-08-01, MaxDate=2025-01-01 query still
    returned trades closed through 2026-08-31 from the real API -- MaxDate
    is silently ignored server-side. This fixture reproduces exactly that:
    the (fake) server ignores end_date and returns rows both inside and
    well past it; the client must filter the late one out locally."""
    rows = [
        {
            "positionId": 1,
            "instrumentId": 1,
            "units": 1.0,
            "openRate": 20.0,
            "closeTimestamp": "2024-09-15T00:00:00Z",
        },
        {
            "positionId": 2,
            "instrumentId": 1,
            "units": 1.0,
            "openRate": 20.0,
            "closeTimestamp": "2026-08-31T00:00:00Z",
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        # A misbehaving-per-MaxDate server: returns both rows regardless of
        # the MaxDate sent, exactly like the live API does.
        assert dict(request.url.params)["MaxDate"] == "2025-01-01"
        return httpx.Response(200, json={"items": rows})

    client = EtoroClient(_config(), transport=httpx.MockTransport(handler))
    entries = client.get_trade_history(start_date="2024-08-01", end_date="2025-01-01")
    assert len(entries) == 1
    assert entries[0].close_date == "2024-09-15T00:00:00Z"


def test_trade_history_keeps_a_row_with_no_close_date_rather_than_dropping_it() -> None:
    """The local MaxDate filter must not fail closed on a row it cannot
    evaluate -- it is a display filter, not a validation gate."""
    rows = [
        {"positionId": 1, "instrumentId": 1, "units": 1.0, "openRate": 20.0}
    ]  # no closeTimestamp at all
    scripted = ScriptedTransport([httpx.Response(200, json={"items": rows})])
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    entries = client.get_trade_history(start_date="2020-01-01", end_date="2020-06-01")
    assert len(entries) == 1
    assert entries[0].close_date is None


# ===========================================================================
# Environment-mismatch probe (gate 7)
# ===========================================================================


def test_verify_environment_raises_on_a_detected_mismatch() -> None:
    scripted = ScriptedTransport(
        [httpx.Response(200, json={"credit": 100.0, "isDemo": False})]
    )
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))  # configured demo
    with pytest.raises(EtoroEnvironmentMismatchError):
        client.verify_environment()


def test_verify_environment_passes_when_signal_matches() -> None:
    scripted = ScriptedTransport([httpx.Response(200, json={"credit": 100.0, "isDemo": True})])
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    client.verify_environment()  # must not raise


def test_verify_environment_is_inconclusive_not_a_failure_when_no_signal() -> None:
    scripted = ScriptedTransport([httpx.Response(200, json={"credit": 100.0})])
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    client.verify_environment()  # inconclusive must not raise


# ===========================================================================
# Misc: Date header capture (feeds preflight gate 4 elsewhere)
# ===========================================================================


def test_last_server_date_is_captured_from_response_headers() -> None:
    scripted = ScriptedTransport(
        [
            httpx.Response(
                200,
                json={"credit": 1.0},
                headers={"Date": "Fri, 02 Jan 2026 12:00:00 GMT"},
            )
        ]
    )
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    assert client.last_server_date is None
    client.get_balance()
    assert client.last_server_date == "Fri, 02 Jan 2026 12:00:00 GMT"


# ===========================================================================
# _parse_position / get_portfolio (DESIGN_market_data.md §4.2, §4.5, §10.7;
# HANDOFF_2026-09-04_market_data.md §5) -- the `currentRate`/`invested`/
# `unrealizedPL`/`symbol` field-mapping bug.
#
# Deliberately exercised against a REALISTIC, NON-EMPTY payload throughout:
# every prior probe of this parser ran against a demo account holding zero
# positions, which is exactly why the bug went unnoticed for months (§8's
# last bullet -- "a check that passes because nothing happened is not a
# pass"). The three raw rows below are the real field names and real numbers
# quoted in DESIGN_market_data.md §4.6 -- three genuine pre-split SCHG
# positions from a live 227-position portfolio probe (2026-09-04), not
# hand-invented fixtures.
# ===========================================================================

#: Three real SCHG rows (DESIGN_market_data.md §4.6), opened before the
#: 2024-10-11 4:1 split. `amount` is preserved exactly across the split;
#: `units` and `openRate` are both retroactively rescaled -- the reason
#: `amount` (not `units`/`openRate`) is the split-invariant re-link key.
_REAL_SCHG_ROWS: list[dict[str, Any]] = [
    {
        "positionID": "pos-schg-1", "instrumentID": 100000, "units": 0.3964,
        "openRate": 25.2275, "amount": 10.0, "unitsBaseValueDollars": 10.0,
        "isBuy": True, "leverage": 1, "openDateTime": "2024-09-03T00:00:00Z",
        "totalFees": 0.0, "initialUnits": 0.3964, "isSettled": True,
        "settlementTypeID": 1,
    },
    {
        "positionID": "pos-schg-2", "instrumentID": 100000, "units": 0.4000,
        "openRate": 24.9975, "amount": 10.0, "unitsBaseValueDollars": 10.0,
        "isBuy": True, "leverage": 1, "openDateTime": "2024-09-03T00:00:00Z",
    },
    {
        "positionID": "pos-schg-3", "instrumentID": 100000, "units": 0.4082,
        "openRate": 24.4975, "amount": 10.0, "unitsBaseValueDollars": 10.0,
        "isBuy": True, "leverage": 1, "openDateTime": "2024-09-06T00:00:00Z",
    },
]


def test_parse_position_maps_real_schg_rows_with_no_silent_nulls() -> None:
    """The core regression test for the bug: every field that genuinely
    exists on a broker position must come back non-None, and the four
    nonexistent keys (`currentRate`/`invested`/`unrealizedPL`/`symbol`) must
    not appear anywhere on the parsed object -- there is no field to hold
    them any more (see `Position`'s docstring)."""
    for raw in _REAL_SCHG_ROWS:
        position = EtoroClient._parse_position(raw)
        assert position.position_id == raw["positionID"]
        assert position.instrument_id == 100000
        assert position.units == pytest.approx(raw["units"])
        assert position.entry_price == pytest.approx(raw["openRate"])
        assert position.amount == pytest.approx(10.0)
        assert position.leverage == 1
        assert position.is_buy is True
        assert position.open_date == raw["openDateTime"]
        # invariant 3 (DESIGN_market_data.md §4.5): amount ~= units * openRate
        assert position.amount == pytest.approx(position.units * position.entry_price, rel=1e-3)
        assert not hasattr(position, "symbol")
        assert not hasattr(position, "current_price")
        assert not hasattr(position, "invested")
        assert not hasattr(position, "unrealized_pnl")


def test_get_portfolio_parses_non_empty_clientPortfolio_with_real_rows() -> None:
    """Drives the fix through the actual HTTP-facing method, not just the
    static parser, against a genuinely populated envelope -- the shape that
    was never exercised before 2026-09-04 (DESIGN_market_data.md §10.8)."""
    scripted = ScriptedTransport(
        [
            httpx.Response(
                200,
                json={
                    "clientPortfolio": {
                        "positions": _REAL_SCHG_ROWS,
                        "mirrors": [],
                        "credit": 1000.0,
                        "orders": [],
                    }
                },
            )
        ]
    )
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    positions = client.get_portfolio()
    assert len(positions) == 3  # not empty -- the exact trap that hid the bug
    for position, raw in zip(positions, _REAL_SCHG_ROWS):
        assert position.position_id == raw["positionID"]
        assert position.amount == pytest.approx(10.0)
        assert position.units == pytest.approx(raw["units"])
        assert position.entry_price == pytest.approx(raw["openRate"])


@pytest.mark.parametrize("missing_field", ["units", "openRate", "amount"])
def test_parse_position_raises_rather_than_defaults_on_missing_field(missing_field: str) -> None:
    """A missing `units`/`openRate`/`amount` is a broker-contract change, not
    a value to silently default to 0.0 (DESIGN_market_data.md §10.7 fix
    direction; task brief item 2)."""
    raw = dict(_REAL_SCHG_ROWS[0])
    del raw[missing_field]
    with pytest.raises(EtoroValidationError):
        EtoroClient._parse_position(raw)


def test_parse_position_raises_on_missing_position_id() -> None:
    raw = dict(_REAL_SCHG_ROWS[0])
    del raw["positionID"]
    with pytest.raises(EtoroValidationError):
        EtoroClient._parse_position(raw)


def test_parse_position_preserves_a_position_id_of_zero() -> None:
    """`positionID: 0` is a falsy-but-present value: an `or`-chain
    (`raw.get("positionID") or raw.get("positionId") or raw.get("id")`)
    would treat it as absent and fall through to the next key -- or, worse,
    to a *different*, wrong id if one of the fallback keys happens to be
    set. Neither must happen; ``0`` must be preserved exactly."""
    raw = dict(_REAL_SCHG_ROWS[0])
    raw["positionID"] = 0
    position = EtoroClient._parse_position(raw)
    assert position.position_id == "0"


def test_parse_position_position_id_of_zero_is_not_shadowed_by_fallback_keys() -> None:
    """Even with `positionId`/`id` also present, a `positionID` of 0 must
    win -- proving the fix does not merely avoid a spurious error but
    actually preserves the correct (real) id rather than silently reporting
    a different one."""
    raw = dict(_REAL_SCHG_ROWS[0])
    raw["positionID"] = 0
    raw["positionId"] = "wrong-fallback-id"
    raw["id"] = "also-wrong"
    position = EtoroClient._parse_position(raw)
    assert position.position_id == "0"


def test_mark_position_computes_value_and_pnl_against_supplied_price() -> None:
    """Mark-to-market must be computed from a caller-supplied price against
    `amount` as cost basis (DESIGN_market_data.md §4.2) -- never read off the
    broker, since `currentRate`/`invested`/`unrealizedPL` do not exist."""
    position = EtoroClient._parse_position(_REAL_SCHG_ROWS[0])
    mark = mark_position(position, price=30.0)
    assert mark.position_id == position.position_id
    assert mark.current_value == pytest.approx(position.units * 30.0)
    assert mark.unrealized_pnl == pytest.approx(position.units * 30.0 - position.amount)
