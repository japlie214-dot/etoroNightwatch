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
from nightwatch.etoro.models import OrderRequest, OrderStatus
from nightwatch.host import DesktopHost
from nightwatch.logging_setup import configure_logging, log_path_for_today

DEMO_API_KEY = "demo_api_key_for_tests_1234567890"
DEMO_USER_KEY = "demo_user_key_for_tests_0987654321"


def _config(
    environment: EtoroEnvironment = EtoroEnvironment.DEMO,
    secrets: dict[str, str] | None = None,
) -> EtoroConfig:
    secrets = secrets if secrets is not None else {
        "ETORO_DEMO_API_KEY": DEMO_API_KEY,
        "ETORO_DEMO_USER_KEY": DEMO_USER_KEY,
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
        secrets={"ETORO_DEMO_API_KEY": "x", "ETORO_DEMO_USER_KEY": "y"},
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
        "ETORO_API_KEY": "real_write_api_key_1234567890",
        "ETORO_USER_KEY": "real_write_user_key_1234567890",
        "ETORO_READ_API_KEY": "real_read_api_key_1234567890",
        "ETORO_READ_USER_KEY": "real_read_user_key_1234567890",
    }
    config = _config(environment=EtoroEnvironment.REAL, secrets=secrets)
    write_client = EtoroClient(config, role="write")
    read_client = EtoroClient(config, role="read")
    assert write_client._credentials.api_key == secrets["ETORO_API_KEY"]
    assert read_client._credentials.api_key == secrets["ETORO_READ_API_KEY"]


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
# ===========================================================================


def test_get_balance_computes_available_cash_from_sample_response() -> None:
    sample = {
        "credit": 5000.0,
        "totalBalance": 999_999.0,  # must be ignored -- portfolio value, not cash
        "ordersForOpen": [
            {"amount": 300.0, "mirrorID": 0},
            {"amount": 1000.0, "mirrorID": 3},  # excluded: mirrorID != 0
        ],
        "orders": [{"amount": 200.0}],
    }
    scripted = ScriptedTransport([httpx.Response(200, json=sample)])
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    balance = client.get_balance()
    assert balance.credit == 5000.0
    assert balance.total_balance == 999_999.0
    assert balance.available_cash == pytest.approx(5000.0 - 300.0 - 200.0)


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
        secrets={"ETORO_DEMO_API_KEY": api_key, "ETORO_DEMO_USER_KEY": user_key}
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
                "eligibility": [
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
                    "eligibility": [
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
        [httpx.Response(200, json={"eligibility": [{"instrumentId": 7, "allowOpenPosition": False}]})]
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
        return httpx.Response(
            200, json={"instruments": [{"instrumentId": 123, "name": "SPDR S&P 500"}]}
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
# Trade history: <1yr lookback enforced, paging works
# ===========================================================================


def test_trade_history_rejects_window_over_documented_lookback() -> None:
    client = EtoroClient(
        _config(), transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
    )
    with pytest.raises(EtoroValidationError):
        client.get_trade_history(start_date="2024-01-01", end_date="2025-06-01")


def test_trade_history_pages_until_a_short_page() -> None:
    pages = iter(
        [
            [{"instrumentId": 1, "units": 1.0} for _ in range(200)],
            [{"instrumentId": 1, "units": 1.0} for _ in range(5)],
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": next(pages)})

    client = EtoroClient(_config(), transport=httpx.MockTransport(handler))
    entries = client.get_trade_history(
        start_date="2026-01-01", end_date="2026-03-01", page_size=200
    )
    assert len(entries) == 205


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
