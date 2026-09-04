"""EtoroClient tests (task brief step 5) -- all offline, mocked httpx transport.

Covers: environment/credential structural safety, leverage/transaction/
settlementType cannot be overridden, retry policy (5xx retried, 429 retried
honoring Retry-After, other 4xx not retried), order status 11 (WaitingFor
Market) never treated as a fill even through the poll loop, idempotency
recovery via referenceId, and that no secret ever appears in a raised
exception's string representation.
"""

from __future__ import annotations

import inspect
import json
from typing import Any, Callable

import httpx
import pytest

from nightwatch.config import Credentials, EtoroConfig, EtoroEnvironment, MissingCredentialError
from nightwatch.db.repo import Repo
from nightwatch.etoro.client import EtoroClient, MAX_LEVERAGE, SETTLEMENT_TYPE, TRANSACTION
from nightwatch.etoro.errors import (
    EtoroAuthError,
    EtoroEnvironmentMismatchError,
    EtoroRateLimitError,
    EtoroServerError,
    EtoroValidationError,
)
from nightwatch.etoro.models import OrderStatus
from nightwatch.etoro.ratelimit import RateLimiter
from nightwatch.host import DesktopHost


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class ManualClock:
    """A controllable clock so poll/retry timing is deterministic in tests."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def make_sleep(clock: ManualClock) -> Callable[[float], None]:
    """A `sleep` that advances the manual clock instead of blocking."""

    def _sleep(seconds: float) -> None:
        clock.advance(seconds)

    return _sleep


def demo_config(**secrets: str) -> EtoroConfig:
    base_secrets = {"ETORO_DEMO_API_KEY": "demoApiKeyFakeValue", "ETORO_DEMO_USER_KEY": "demoUserKeyFakeValue"}
    base_secrets.update(secrets)
    return EtoroConfig(environment=EtoroEnvironment.DEMO, secrets=base_secrets)


def real_config(**secrets: str) -> EtoroConfig:
    base_secrets = {
        "ETORO_API_KEY": "realApiKeyFakeValue",
        "ETORO_USER_KEY": "realUserKeyFakeValue",
        "ETORO_READ_API_KEY": "realReadApiKeyFakeValue",
        "ETORO_READ_USER_KEY": "realReadUserKeyFakeValue",
    }
    base_secrets.update(secrets)
    return EtoroConfig(environment=EtoroEnvironment.REAL, secrets=base_secrets)


class ScriptedTransport(httpx.BaseTransport):
    """Replays a fixed sequence of responses, one per call, and records
    every request it saw (method, path, json body) for assertions."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("ScriptedTransport ran out of scripted responses")
        return self._responses.pop(0)

    @property
    def call_count(self) -> int:
        return len(self.requests)


def json_response(status_code: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status_code, json=body, headers=headers or {})


def client_with_transport(
    config: EtoroConfig, transport: httpx.BaseTransport, **kwargs: Any
) -> EtoroClient:
    return EtoroClient(config, transport=transport, sleep=lambda s: None, **kwargs)


@pytest.fixture
def repo(tmp_path) -> Repo:
    host = DesktopHost(root=tmp_path)
    r = Repo.open(host.data_dir() / "nightwatch.db", host=host)
    yield r
    r.close()


# ---------------------------------------------------------------------------
# Environment / credential structural safety
# ---------------------------------------------------------------------------


def test_client_has_no_constructor_parameter_for_raw_credentials() -> None:
    """The only way in is a whole EtoroConfig -- there is structurally no
    api_key/user_key/base_url parameter a caller could use to build a
    mismatched client."""
    params = set(inspect.signature(EtoroClient.__init__).parameters)
    assert "api_key" not in params
    assert "user_key" not in params
    assert "credentials" not in params
    assert "base_url" not in params
    assert "environment" not in params


def test_demo_config_with_only_real_secrets_cannot_construct_a_client() -> None:
    config = EtoroConfig(
        environment=EtoroEnvironment.DEMO,
        secrets={"ETORO_API_KEY": "x", "ETORO_USER_KEY": "y"},  # wrong keys for demo
    )
    with pytest.raises(MissingCredentialError):
        EtoroClient(config, transport=httpx.MockTransport(lambda r: json_response(200, {})))


def test_real_config_with_only_demo_secrets_cannot_construct_a_client() -> None:
    config = EtoroConfig(
        environment=EtoroEnvironment.REAL,
        secrets={"ETORO_DEMO_API_KEY": "x", "ETORO_DEMO_USER_KEY": "y"},
    )
    with pytest.raises(MissingCredentialError):
        EtoroClient(config, transport=httpx.MockTransport(lambda r: json_response(200, {})))


def test_defense_in_depth_rejects_a_credentials_label_environment_mismatch() -> None:
    """Even if something upstream of EtoroConfig's own invariants were broken
    (simulated here with a duck-typed fake config whose write_credentials
    disagrees with its own environment), EtoroClient's constructor still
    refuses rather than silently proceeding."""

    class _BrokenConfig:
        environment = EtoroEnvironment.DEMO
        base_url = "https://public-api.etoro.com"
        timeout_seconds = 30.0
        path_segment = "demo/"
        write_credentials = Credentials(label="real:write", api_key="a", user_key="b")
        read_credentials = Credentials(label="real:read", api_key="a", user_key="b")

    with pytest.raises(EtoroEnvironmentMismatchError):
        EtoroClient(_BrokenConfig(), transport=httpx.MockTransport(lambda r: json_response(200, {})))


def test_demo_client_uses_the_demo_url_path_segment() -> None:
    transport = ScriptedTransport([json_response(200, {"credit": 100.0})])
    client = client_with_transport(demo_config(), transport)
    client.get_balance()
    assert "/demo/" in str(transport.requests[0].url)


def test_real_client_uses_no_demo_path_segment() -> None:
    transport = ScriptedTransport([json_response(200, {"credit": 100.0})])
    client = client_with_transport(real_config(), transport)
    client.get_balance()
    assert "/demo/" not in str(transport.requests[0].url)


def test_read_role_uses_read_credentials_not_write() -> None:
    transport = ScriptedTransport([json_response(200, {"credit": 100.0})])
    config = real_config()
    client = client_with_transport(config, transport, role="read")
    client.get_balance()
    sent = transport.requests[0].headers
    assert sent["x-api-key"] == config.read_credentials.api_key
    assert sent["x-api-key"] != config.write_credentials.api_key


# ---------------------------------------------------------------------------
# Leverage / transaction / settlementType cannot be overridden
# ---------------------------------------------------------------------------


def test_module_constants_are_the_only_safe_values() -> None:
    assert MAX_LEVERAGE == 1
    assert TRANSACTION == "buy"
    assert SETTLEMENT_TYPE == "real"


def test_place_buy_order_has_no_leverage_or_transaction_parameter() -> None:
    """There is no way to call place_buy_order and ask for a different
    leverage, transaction, or settlementType -- the signature simply has no
    such parameter."""
    params = set(inspect.signature(EtoroClient.place_buy_order).parameters)
    assert "leverage" not in params
    assert "transaction" not in params
    assert "settlement_type" not in params
    assert "settlementType" not in params


def test_place_buy_order_sends_exactly_the_safe_body() -> None:
    transport = ScriptedTransport([json_response(200, {"orderId": "o-1"})])
    client = client_with_transport(demo_config(), transport)
    client.place_buy_order("11111111-1111-1111-1111-111111111111", 1001, 250.0)
    sent_body = json.loads(transport.requests[0].content)
    assert sent_body["leverage"] == 1
    assert sent_body["transaction"] == "buy"
    assert sent_body["settlementType"] == "real"
    assert sent_body["action"] == "open"
    assert sent_body["amount"] == 250.0


def test_place_buy_order_uses_caller_supplied_request_id_not_a_fresh_one() -> None:
    """DESIGN §5/§6.H: the request id must be generated and persisted BEFORE
    the HTTP call by the caller (the intents table); the client must use it
    verbatim, never mint its own."""
    transport = ScriptedTransport([json_response(200, {"orderId": "o-1"})])
    client = client_with_transport(demo_config(), transport)
    request_id = "22222222-2222-2222-2222-222222222222"
    client.place_buy_order(request_id, 1001, 100.0)
    assert transport.requests[0].headers["x-request-id"] == request_id


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


def test_5xx_is_retried_then_succeeds() -> None:
    transport = ScriptedTransport(
        [
            json_response(500, {"error": "boom"}),
            json_response(502, {"error": "boom"}),
            json_response(200, {"credit": 500.0}),
        ]
    )
    client = client_with_transport(demo_config(), transport)
    balance = client.get_balance()
    assert balance.credit == 500.0
    assert transport.call_count == 3


def test_5xx_exhausting_retries_raises_server_error() -> None:
    transport = ScriptedTransport([json_response(500, {"error": "boom"})] * 5)
    client = client_with_transport(demo_config(), transport)
    with pytest.raises(EtoroServerError):
        client.get_balance()
    assert transport.call_count == 5


def test_429_is_retried_honoring_retry_after() -> None:
    clock = ManualClock()
    sleeps: list[float] = []

    def tracking_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)

    transport = ScriptedTransport(
        [
            json_response(429, {"error": "slow down"}, headers={"Retry-After": "7"}),
            json_response(200, {"credit": 42.0}),
        ]
    )
    client = EtoroClient(demo_config(), transport=transport, sleep=tracking_sleep, clock=clock)
    balance = client.get_balance()
    assert balance.credit == 42.0
    assert 7.0 in sleeps


def test_429_exhausting_retries_raises_rate_limit_error_with_retry_after() -> None:
    transport = ScriptedTransport(
        [json_response(429, {"error": "slow down"}, headers={"Retry-After": "3"})] * 5
    )
    client = client_with_transport(demo_config(), transport)
    with pytest.raises(EtoroRateLimitError) as excinfo:
        client.get_balance()
    assert excinfo.value.retry_after == 3.0


@pytest.mark.parametrize("status_code", [400, 404, 422])
def test_other_4xx_is_never_retried(status_code: int) -> None:
    transport = ScriptedTransport([json_response(status_code, {"error": "bad request"})])
    client = client_with_transport(demo_config(), transport)
    with pytest.raises(EtoroValidationError):
        client.get_balance()
    assert transport.call_count == 1  # no retry attempted


def test_401_raises_auth_error_immediately_without_retry() -> None:
    transport = ScriptedTransport([json_response(401, {"error": "unauthorized"})])
    client = client_with_transport(demo_config(), transport)
    with pytest.raises(EtoroAuthError):
        client.get_balance()
    assert transport.call_count == 1


# ---------------------------------------------------------------------------
# Order status 11 (WaitingForMarket) handling through the poll loop
# ---------------------------------------------------------------------------


def _lookup_response(status: OrderStatus, **extra: Any) -> httpx.Response:
    body = {"order": {"orderId": "o-1", "referenceId": "r-1", "status": int(status), **extra}}
    return json_response(200, body)


def test_poll_order_to_terminal_never_reports_waiting_for_market_as_filled() -> None:
    clock = ManualClock()
    transport = ScriptedTransport(
        [
            _lookup_response(OrderStatus.RECEIVED),
            _lookup_response(OrderStatus.WAITING_FOR_MARKET),
            _lookup_response(OrderStatus.WAITING_FOR_MARKET),
            _lookup_response(OrderStatus.FILLED, executionPrice=55.5, units=10.0),
        ]
    )
    client = EtoroClient(
        demo_config(), transport=transport, sleep=make_sleep(clock), clock=clock,
        poll_interval_seconds=1.0, poll_timeout_seconds=60.0,
    )
    result = client.poll_order_to_terminal(order_id="o-1")
    assert result.timed_out is False
    assert result.order.status is OrderStatus.FILLED
    assert result.order.is_filled is True
    assert result.attempts == 4


def test_poll_order_to_terminal_times_out_while_stuck_waiting_for_market() -> None:
    clock = ManualClock()
    responses = [_lookup_response(OrderStatus.WAITING_FOR_MARKET) for _ in range(100)]
    transport = ScriptedTransport(responses)
    client = EtoroClient(
        demo_config(), transport=transport, sleep=make_sleep(clock), clock=clock,
        poll_interval_seconds=5.0, poll_timeout_seconds=12.0,
    )
    result = client.poll_order_to_terminal(order_id="o-1")
    assert result.timed_out is True
    assert result.order.status is OrderStatus.WAITING_FOR_MARKET
    assert result.order.is_filled is False  # never booked as a fill, even on timeout


def test_poll_order_to_terminal_stops_on_rejection() -> None:
    clock = ManualClock()
    transport = ScriptedTransport(
        [_lookup_response(OrderStatus.REJECTED, rejectReason="insufficient funds")]
    )
    client = EtoroClient(demo_config(), transport=transport, sleep=make_sleep(clock), clock=clock)
    result = client.poll_order_to_terminal(order_id="o-1")
    assert result.timed_out is False
    assert result.order.status is OrderStatus.REJECTED
    assert result.order.is_filled is False


# ---------------------------------------------------------------------------
# Idempotency: recovery via referenceId, not re-sending
# ---------------------------------------------------------------------------


def test_recover_by_reference_id_finds_a_previously_submitted_order() -> None:
    transport = ScriptedTransport([_lookup_response(OrderStatus.FILLED, executionPrice=10.0)])
    client = client_with_transport(demo_config(), transport)
    result = client.recover_by_reference_id("r-lost-response")
    assert result is not None
    assert result.status is OrderStatus.FILLED
    # The recovery path is a GET lookup, never a re-POST of the order.
    assert transport.requests[0].method == "GET"


def test_recover_by_reference_id_returns_none_when_etoro_never_saw_it() -> None:
    """A 4xx from orders:lookup for an unknown referenceId means the original
    request never reached the server -- safe to (re)submit. Any other
    response means it did, and must never be resubmitted."""
    transport = ScriptedTransport([json_response(404, {"error": "not found"})])
    client = client_with_transport(demo_config(), transport)
    result = client.recover_by_reference_id("r-never-sent")
    assert result is None


# ---------------------------------------------------------------------------
# No secret ever appears in a raised exception's string representation
# ---------------------------------------------------------------------------


def test_auth_error_message_never_contains_the_api_key() -> None:
    config = demo_config(
        ETORO_DEMO_API_KEY="superSecretDemoApiKeyAbc123456789012345",
        ETORO_DEMO_USER_KEY="superSecretDemoUserKeyXyz987654321098765",
    )
    # Echo the auth headers back in the error body, simulating a server (or a
    # bug) that reflects request headers into an error response -- the worst
    # case for a leak, not the best case.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": "unauthorized", "sawApiKey": request.headers.get("x-api-key")},
        )

    client = EtoroClient(config, transport=httpx.MockTransport(handler), sleep=lambda s: None)
    with pytest.raises(EtoroAuthError) as excinfo:
        client.get_balance()

    message = str(excinfo.value)
    assert "superSecretDemoApiKeyAbc123456789012345" not in message
    assert "REDACTED" in message


def test_server_error_message_never_contains_the_user_key() -> None:
    config = demo_config(
        ETORO_DEMO_API_KEY="anotherSecretApiKeyValue0000000000000000",
        ETORO_DEMO_USER_KEY="anotherSecretUserKeyValue1111111111111111",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"error": "boom", "echoedUserKey": request.headers.get("x-user-key")},
        )

    client = EtoroClient(
        config, transport=httpx.MockTransport(handler), sleep=lambda s: None, max_attempts=1,
    )
    with pytest.raises(EtoroServerError) as excinfo:
        client.get_balance()

    message = str(excinfo.value)
    assert "anotherSecretUserKeyValue1111111111111111" not in message


# ---------------------------------------------------------------------------
# Instrument resolution + DB caching (immutable ids)
# ---------------------------------------------------------------------------


def test_resolve_instrument_id_caches_via_repo_and_never_calls_twice(repo: Repo) -> None:
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return json_response(
            200, {"instruments": [{"instrumentId": 12345, "instrumentDisplayName": "SPY"}]}
        )

    client = EtoroClient(
        demo_config(), transport=httpx.MockTransport(handler), repo=repo, sleep=lambda s: None
    )
    first = client.resolve_instrument_id("SPY")
    second = client.resolve_instrument_id("SPY")

    assert first == 12345
    assert second == 12345
    assert call_count["n"] == 1  # second call served entirely from the DB cache

    cached = repo.get_instrument("SPY")
    assert cached is not None
    assert cached.instrument_id == 12345


# ---------------------------------------------------------------------------
# Eligibility: block-and-alert, never silently resize
# ---------------------------------------------------------------------------


def test_check_eligibility_blocks_when_order_size_below_minimum() -> None:
    from nightwatch.etoro.errors import EtoroEligibilityError

    transport = ScriptedTransport(
        [
            json_response(
                200,
                {
                    "eligibility": [
                        {
                            "instrumentId": 1001,
                            "allowOpenPosition": True,
                            "minPositionExposure": 50.0,
                        }
                    ]
                },
            )
        ]
    )
    client = client_with_transport(demo_config(), transport)
    with pytest.raises(EtoroEligibilityError):
        client.check_eligibility_for_order(1001, order_size=10.0)


def test_check_eligibility_blocks_when_not_open_able() -> None:
    from nightwatch.etoro.errors import EtoroEligibilityError

    transport = ScriptedTransport(
        [json_response(200, {"eligibility": [{"instrumentId": 1001, "allowOpenPosition": False}]})]
    )
    client = client_with_transport(demo_config(), transport)
    with pytest.raises(EtoroEligibilityError):
        client.check_eligibility_for_order(1001, order_size=100.0)


def test_eligibility_batches_at_100_ids_per_call() -> None:
    ids = list(range(1, 151))  # 150 ids -> two batches of 100 and 50

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        batch_ids = body["instrumentIds"]
        assert len(batch_ids) <= 100
        return json_response(
            200,
            {
                "eligibility": [
                    {"instrumentId": i, "allowOpenPosition": True} for i in batch_ids
                ]
            },
        )

    client = EtoroClient(demo_config(), transport=httpx.MockTransport(handler), sleep=lambda s: None)
    result = client.get_eligibility(ids)
    assert len(result.results) == 150


# ---------------------------------------------------------------------------
# Trade history: <1yr lookback enforced
# ---------------------------------------------------------------------------


def test_trade_history_rejects_a_window_over_one_year() -> None:
    client = client_with_transport(demo_config(), ScriptedTransport([]))
    with pytest.raises(EtoroValidationError, match="lookback"):
        client.get_trade_history(start_date="2020-01-01", end_date="2021-06-01")


def test_trade_history_pages_until_a_short_page() -> None:
    transport = ScriptedTransport(
        [
            json_response(200, {"items": [{"instrumentId": 1, "action": "buy", "units": 1.0}] * 2}),
            json_response(200, {"items": [{"instrumentId": 1, "action": "buy", "units": 1.0}]}),
        ]
    )
    client = client_with_transport(demo_config(), transport)
    entries = client.get_trade_history(start_date="2026-01-01", end_date="2026-06-01", page_size=2)
    assert len(entries) == 3
    assert transport.call_count == 2
