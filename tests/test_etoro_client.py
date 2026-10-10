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
from pathlib import Path
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
    EtoroRateLimitError,
    EtoroServerError,
    EtoroValidationError,
)
from nightwatch.etoro.models import EditPositionAck, OrderRequest, OrderStatus, mark_position
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


@pytest.mark.parametrize("request_id", ["", " \t\n"])
@pytest.mark.parametrize("method", ["place_buy_order", "close_position"])
def test_write_invalid_request_id_never_sends(request_id: str, method: str) -> None:
    scripted = ScriptedTransport([])
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    with pytest.raises(ValueError) as caught:
        if method == "place_buy_order":
            client.place_buy_order(request_id, instrument_id=42, amount_usd=100.0)
        else:
            client.close_position(request_id, position_id="p-1", instrument_id=42)
    assert str(caught.value) == (
        f"{method}: request_id must be non-empty (caller persists it first)"
    )
    assert len(scripted.requests) == 0


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
# close_position: full close stays byte-identical; partial close is now
# expressible (board nightwatch/handoff-2026-09-06.md §7)
# ===========================================================================


def test_close_position_default_is_byte_identical_full_close() -> None:
    """No ``units_to_deduct`` argument at all -- the pre-existing call shape
    -- must still send exactly ``{"InstrumentId": ..., "UnitsToDeduct":
    null}``, unchanged from before partial close existed."""
    scripted = ScriptedTransport([httpx.Response(200, json={"orderId": "o-1"})])
    client = EtoroClient(
        _config(), transport=httpx.MockTransport(scripted.handler), sleep=_no_sleep
    )
    client.close_position(
        request_id=str(uuid.uuid4()), position_id="p-1", instrument_id=42
    )
    body = json.loads(scripted.requests[0].content)
    assert body == {"InstrumentId": 42, "UnitsToDeduct": None}


def test_close_position_accepts_units_to_deduct_for_a_partial_close() -> None:
    scripted = ScriptedTransport([httpx.Response(200, json={"orderId": "o-2"})])
    client = EtoroClient(
        _config(), transport=httpx.MockTransport(scripted.handler), sleep=_no_sleep
    )
    client.close_position(
        request_id=str(uuid.uuid4()),
        position_id="p-1",
        instrument_id=42,
        units_to_deduct=3.5,
    )
    body = json.loads(scripted.requests[0].content)
    assert body == {"InstrumentId": 42, "UnitsToDeduct": 3.5}


# ===========================================================================
# clear_take_profit: demo-only, asynchronous, never retried (T012)
# ===========================================================================


def test_clear_take_profit_sends_demo_patch_and_parses_ack() -> None:
    request_id = str(uuid.uuid4())
    scripted = ScriptedTransport([httpx.Response(202, json={
        "operationId": 123, "positionId": 3550406905, "referenceId": request_id,
    })])
    client = EtoroClient(
        _config(), transport=httpx.MockTransport(scripted.handler), sleep=_no_sleep
    )
    ack = client.clear_take_profit(request_id, "3550406905")
    assert ack == EditPositionAck("123", "3550406905", request_id, 202)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ack.status_code = 200
    assert len(scripted.requests) == 1
    request = scripted.requests[0]
    assert request.method == "PATCH"
    assert request.url.path == "/api/v2/trading/demo/positions/3550406905"
    assert json.loads(request.content) == {"clearTakeProfit": True}
    assert request.headers["x-request-id"] == request_id


@pytest.mark.parametrize("response", [
    httpx.Response(200), httpx.Response(200, json=[]), httpx.Response(202, json={}),
])
def test_clear_take_profit_empty_or_non_object_body(response: httpx.Response) -> None:
    scripted = ScriptedTransport([response])
    client = EtoroClient(
        _config(), transport=httpx.MockTransport(scripted.handler), sleep=_no_sleep
    )
    assert client.clear_take_profit("request-1", "3550406905") == EditPositionAck(
        None, None, None, response.status_code
    )
    assert len(scripted.requests) == 1


def test_clear_take_profit_refuses_wrong_position() -> None:
    scripted = ScriptedTransport([httpx.Response(202, json={"positionId": 123})])
    client = EtoroClient(
        _config(), transport=httpx.MockTransport(scripted.handler), sleep=_no_sleep
    )
    with pytest.raises(EtoroValidationError, match="expected 3550406905"):
        client.clear_take_profit("request-1", "3550406905")
    assert len(scripted.requests) == 1


@pytest.mark.parametrize("request_id, position_id, message", [
    ("", "3550406905", "request_id"),
    ("request-1", "", "position_id"),
    ("request-1", "12/../x", "position_id"),
])
def test_clear_take_profit_invalid_input_never_sends(
    request_id: str, position_id: str, message: str,
) -> None:
    scripted = ScriptedTransport([])
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    with pytest.raises(ValueError, match=message):
        client.clear_take_profit(request_id, position_id)
    assert len(scripted.requests) == 0


def test_clear_take_profit_real_mode_never_sends() -> None:
    config = _config(environment=EtoroEnvironment.REAL, secrets={
        "ETORO_PRIVATE_KEY": "fake_private_key_for_tests",
        "ETORO_PUBLIC_KEY": "fake_public_key_for_tests",
    })
    scripted = ScriptedTransport([])
    client = EtoroClient(config, transport=httpx.MockTransport(scripted.handler))
    with pytest.raises(EtoroValidationError, match="unconfirmed"):
        client.clear_take_profit("request-1", "3550406905")
    assert len(scripted.requests) == 0


@pytest.mark.parametrize("status_code, error", [
    (429, EtoroRateLimitError), (400, EtoroValidationError), (500, EtoroServerError),
    (204, EtoroValidationError),
])
def test_clear_take_profit_errors_never_retry(status_code: int, error: type[Exception]) -> None:
    scripted = ScriptedTransport([httpx.Response(status_code)])
    sleeps: list[float] = []
    client = EtoroClient(
        _config(), transport=httpx.MockTransport(scripted.handler), sleep=sleeps.append
    )
    with pytest.raises(error):
        client.clear_take_profit("request-1", "3550406905")
    assert len(scripted.requests) == 1
    assert sleeps == []


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


@pytest.mark.parametrize(
    "portfolio",
    [
        {"credit": 10.0, "ordersForOpen": []},
        {"credit": 10.0, "orders": []},
        {"credit": 10.0, "ordersForOpen": [], "orders": [{}]},
        {"credit": 10.0, "ordersForOpen": [{"amount": 5.0}], "orders": []},
        {"credit": 10.0, "ordersForOpen": {}, "orders": []},
        {"credit": 10.0, "ordersForOpen": [], "orders": [{"amount": "bad"}]},
        {"credit": True, "ordersForOpen": [], "orders": []},
    ],
)
def test_get_balance_rejects_incomplete_or_malformed_pending_order_fields(
    portfolio: dict[str, Any],
) -> None:
    scripted = ScriptedTransport(
        [httpx.Response(200, json={"clientPortfolio": portfolio})]
    )
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
        # The REAL live fill shape (nested status object + positionExecutions),
        # not the flat one this test used to guess at -- see
        # data/probe_2026-09-11/order_latency_run2/RAW_orders_lookup_payload.json.
        httpx.Response(
            200,
            json={
                "orderId": "o1",
                "referenceId": "r1",
                "status": {"id": 3, "name": "Filled", "errorCode": 0},
                "requestedUnits": 1.5,
                "positionExecutions": [
                    {
                        "positionId": 3598679249,
                        "openingData": {"units": 1.5, "avgPrice": 66.0},
                    }
                ],
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
    assert result.order.position_id == "3598679249"
    assert result.order.fill_units == 1.5
    assert result.order.fill_price == 66.0


def test_poll_close_to_terminal_uses_close_route_for_every_poll() -> None:
    scripted = ScriptedTransport([
        httpx.Response(200, json={
            "orderID": 387742157, "statusID": 1, "positions": [],
        }),
        httpx.Response(200, json={
            "orderID": 387742157, "statusID": 3,
            "positions": [{"positionID": 3601194005, "units": 1.25, "amount": 50.0}],
        }),
    ])
    sleeps: list[float] = []
    client = EtoroClient(
        _config(), transport=httpx.MockTransport(scripted.handler),
        sleep=sleeps.append, clock=Counter(),
    )
    result = client.poll_order_to_terminal(
        order_id="387742157", kind="close", interval_seconds=2.0, timeout_seconds=30.0
    )
    assert result.timed_out is False
    assert result.order.status is OrderStatus.FILLED
    assert result.order.order_id == "387742157"
    assert result.attempts == 2
    assert sleeps == [2.0]
    assert [request.url.path for request in scripted.requests] == [
        "/api/v1/trading/info/demo/close-orders/387742157",
        "/api/v1/trading/info/demo/close-orders/387742157",
    ]


@pytest.mark.parametrize("kwargs", [
    {"kind": "close"}, {"kind": "close", "reference_id": "r1"},
    {"kind": "bogus", "order_id": "387742157"},
])
def test_poll_invalid_kind_or_missing_close_id_never_sends(kwargs: dict[str, Any]) -> None:
    scripted = ScriptedTransport([])
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    with pytest.raises(ValueError):
        client.poll_order_to_terminal(**kwargs)
    assert len(scripted.requests) == 0


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
# orders:lookup parsing, against the REAL captured live payload
#
# Every one of these is a regression test for the 2026-09-11 bug: the parser
# read a guessed flat shape, and a parse miss was CAUGHT and defaulted to
# OrderStatus.RECEIVED -- so an order that filled in ~163ms was polled for 90s,
# booked as a timeout, and its position left open and unclosable because
# position_id had silently become None.
# ===========================================================================


def test_lookup_close_order_uses_documented_endpoint_and_parses_closed_position() -> None:
    payload = {
        "orderID": 382291007,
        "statusID": 3,
        "referenceID": "close-request-1",
        "positions": [{"positionID": 3601194005, "units": 1.25, "amount": 50.0}],
    }
    scripted = ScriptedTransport([httpx.Response(200, json=payload)])
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))

    result = client.lookup_close_order(382291007)

    assert result.status is OrderStatus.FILLED
    assert result.order_id == "382291007"
    assert result.position_id == "3601194005"
    request = scripted.requests[0]
    assert request.url.path == "/api/v1/trading/info/demo/close-orders/382291007"
    assert client.last_close_lookup_responses[0]["status_code"] == 200
    assert json.loads(client.last_close_lookup_responses[0]["body"]) == payload


@pytest.mark.parametrize(
    "payload,match",
    [
        ({"orderID": 9, "statusID": 999, "positions": []}, "unknown statusID"),
        ({"orderID": 9, "statusID": 1, "positions": {}}, "positions must be a list"),
        ({"orderID": 9, "statusID": 3, "positions": []}, "no affected position ID"),
    ],
)
def test_lookup_close_order_rejects_unknown_or_malformed_status_payloads(
    payload: dict[str, Any], match: str,
) -> None:
    client = EtoroClient(
        _config(),
        transport=httpx.MockTransport(
            ScriptedTransport([httpx.Response(200, json=payload)]).handler
        ),
    )
    with pytest.raises(EtoroValidationError, match=match):
        client.lookup_close_order(9)

REAL_ORDERS_LOOKUP_PAYLOAD = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "probe_2026-09-11"
    / "order_latency_run2"
    / "RAW_orders_lookup_payload.json"
)


def _real_lookup_payload() -> dict[str, Any]:
    """The actual live orders:lookup response captured 2026-09-11.

    Loaded from the captured file rather than retyped, so the fixture can
    never drift from what eToro really sent.
    """
    return json.loads(REAL_ORDERS_LOOKUP_PAYLOAD.read_text(encoding="utf-8"))


def _parse_real(**overrides: Any) -> Any:
    payload = _real_lookup_payload()
    payload.update(overrides)
    return EtoroClient._parse_order_result(payload)


def test_demo_order_lookup_uses_legacy_get_after_v2_404() -> None:
    payload = json.loads((Path(__file__).parent / "fixtures/etoro_contracts/legacy_order_lookup.json").read_text())
    payload["orderID"] = 382239291
    payload["referenceID"] = "sanitized-request-id"
    payload["positions"][0]["positionID"] = 3601294502
    scripted = ScriptedTransport([
        httpx.Response(404, json={"message": "Order category not found"}),
        httpx.Response(200, json=payload),
    ])
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    captured: list[dict[str, Any]] = []

    result = client.lookup_order(order_id="382239291", response_capture=captured)

    assert result.order_id == "382239291"
    assert result.position_id == "3601294502"
    assert result.fill_units == pytest.approx(1.415628)
    assert result.fill_price == pytest.approx(35.29)
    assert "token" not in result.raw and "CID" not in result.raw
    assert [request.url.path for request in scripted.requests] == [
        "/api/v2/trading/info/demo/orders:lookup",
        "/api/v1/trading/info/demo/orders/382239291",
    ]
    assert [response["status_code"] for response in captured] == [404, 200]
    assert [response["path"] for response in captured] == [
        "/api/v2/trading/info/demo/orders:lookup",
        "/api/v1/trading/info/demo/orders/382239291",
    ]


def test_order_lookup_never_uses_deprecated_fallback_for_real_or_reference_id() -> None:
    real_config = EtoroConfig(
        environment=EtoroEnvironment.REAL,
        secrets={"ETORO_PRIVATE_KEY": "real_private_key_for_test", "ETORO_PUBLIC_KEY": "public_key_for_test"},
    )
    for config, kwargs in (
        (real_config, {"order_id": "382239291"}),
        (_config(), {"reference_id": "known-request-id"}),
    ):
        scripted = ScriptedTransport([httpx.Response(404, json={"message": "not found"})])
        client = EtoroClient(config, transport=httpx.MockTransport(scripted.handler))
        with pytest.raises(EtoroValidationError):
            client.lookup_order(**kwargs)
        assert len(scripted.requests) == 1


def test_real_payload_nested_status_object_parses_to_filled_not_received() -> None:
    """`status` is `{"id": 3, "name": "Filled"}`, not an int. The old parser
    did `int(dict)`, caught the TypeError, and defaulted to RECEIVED -- i.e.
    reported every single filled order as still pending."""
    assert _real_lookup_payload()["status"] == {"id": 3, "name": "Filled", "errorCode": 0}
    result = _parse_real()
    assert result.status is OrderStatus.FILLED
    assert result.status is not OrderStatus.RECEIVED
    assert result.is_filled is True
    assert result.is_terminal is True
    assert result.status_label == "Filled"


def test_real_payload_position_id_resolves_from_position_executions() -> None:
    """There is no top-level `positionId`; it lives at
    `positionExecutions[0].positionId`. Losing it is what stranded two live
    demo positions -- a filled position we cannot name cannot be closed."""
    payload = _real_lookup_payload()
    assert "positionId" not in payload
    assert _parse_real().position_id == "3598679249"


@pytest.mark.parametrize(
    "status_id,status_name",
    [(9, "CanceledPartiallyFilled"), (10, "RejectedPartiallyFilled")],
)
def test_partially_filled_terminal_orders_keep_their_position_id(
    status_id: int, status_name: str
) -> None:
    result = _parse_real(status={"id": status_id, "name": status_name})
    assert result.is_terminal is True
    assert result.is_filled is True
    assert result.position_id == "3598679249"


@pytest.mark.parametrize(
    "field,value",
    [
        ("orderId", ""),
        ("orderId", True),
        ("orderId", {}),
        ("referenceId", " "),
        ("positionId", ""),
        ("positionId", True),
        ("positionId", {}),
    ],
)
def test_order_lookup_ids_must_be_nonempty_strings_or_nonbool_integers(
    field: str, value: Any
) -> None:
    with pytest.raises(EtoroValidationError, match="non-empty string or integer"):
        _parse_real(**{field: value})


def test_order_lookup_rejects_conflicting_cased_order_ids() -> None:
    payload = _real_lookup_payload()
    payload["orderID"] = 123
    with pytest.raises(EtoroValidationError, match="conflicting order ids"):
        EtoroClient._parse_order_result(payload)


def test_position_id_is_found_in_any_execution_and_ambiguous_ids_raise() -> None:
    payload = _real_lookup_payload()
    del payload["positionExecutions"][0]["positionId"]
    payload["positionExecutions"].append({"positionId": 3598679249})
    assert EtoroClient._parse_order_result(payload).position_id == "3598679249"

    payload["positionExecutions"].append({"positionId": 3598679250})
    with pytest.raises(EtoroValidationError, match="multiple position ids"):
        EtoroClient._parse_order_result(payload)


def test_real_payload_units_and_price_resolve_from_the_execution() -> None:
    """No `units` key exists at all (it is `requestedUnits` at the top level
    and `positionExecutions[0].openingData.units` on the execution), and the
    fill price is `openingData.avgPrice`, not `executionPrice`."""
    payload = _real_lookup_payload()
    assert "units" not in payload
    assert "executionPrice" not in payload
    result = _parse_real()
    assert result.fill_units == 1.418439
    assert result.fill_price == 35.22
    assert result.order_id == "380689943"


def test_executed_units_win_over_merely_requested_units() -> None:
    """`requestedUnits` is what we asked for, not what filled. When the
    execution states its own units they must win, or a partial fill would be
    reported as a full one."""
    payload = _real_lookup_payload()
    payload["requestedUnits"] = 99.0
    payload["positionExecutions"][0]["openingData"]["units"] = 1.0
    assert EtoroClient._parse_order_result(payload).fill_units == 1.0


# -- the core regression: a parse miss must never become a default ----------


def test_unparseable_status_raises_and_never_silently_becomes_received() -> None:
    """THE regression test for this bug class. A status the parser cannot
    understand must be loud. The old code's `except (TypeError, ValueError):
    status = OrderStatus.RECEIVED` substituted a *non-terminal* status for
    "I don't know", which is what converted fills into timeouts."""
    for broken in (
        {"name": "Filled"},
        {"id": "not-a-number"},
        {"id": 9999},
        {"id": 3.9},
        {"id": True},
        "Filled",
        None,
    ):
        with pytest.raises(EtoroValidationError):
            _parse_real(status=broken)


def test_missing_status_key_entirely_raises() -> None:
    payload = _real_lookup_payload()
    del payload["status"]
    with pytest.raises(EtoroValidationError):
        EtoroClient._parse_order_result(payload)


def test_missing_legacy_status_does_not_echo_write_token() -> None:
    payload = json.loads((Path(__file__).parent / "fixtures/etoro_contracts/legacy_order_lookup.json").read_text())
    del payload["statusID"]
    payload["token"] = "sensitive-write-capability"
    with pytest.raises(EtoroValidationError) as caught:
        EtoroClient._parse_order_result(payload)
    assert "sensitive-write-capability" not in str(caught.value)


def test_filled_order_with_no_resolvable_position_id_raises() -> None:
    """A fill we cannot attach a position id to is an unclosable position.
    That is position-identity-critical, so it raises rather than yielding
    `position_id=None` and a cheerful FILLED."""
    payload = _real_lookup_payload()
    payload["positionExecutions"] = []
    with pytest.raises(EtoroValidationError):
        EtoroClient._parse_order_result(payload)

    payload = _real_lookup_payload()
    del payload["positionExecutions"][0]["positionId"]
    with pytest.raises(EtoroValidationError):
        EtoroClient._parse_order_result(payload)


def test_malformed_position_executions_container_raises() -> None:
    for broken in ({"positionId": 1}, "nope", [42]):
        with pytest.raises(EtoroValidationError):
            _parse_real(positionExecutions=broken)

    with pytest.raises(EtoroValidationError, match="opening/closingData"):
        _parse_real(positionExecutions=[{"positionId": 123, "openingData": []}])


def test_non_numeric_stated_numeric_field_raises_rather_than_becoming_none() -> None:
    """"Absent" and "present but garbage" are different facts. Absent is
    allowed to be None; garbage must raise, not fall through to the next
    candidate key and not silently become None."""
    payload = _real_lookup_payload()
    payload["positionExecutions"][0]["openingData"]["avgPrice"] = "thirty-five"
    with pytest.raises(EtoroValidationError):
        EtoroClient._parse_order_result(payload)

    payload = _real_lookup_payload()
    payload["positionExecutions"][0]["openingData"]["units"] = {"value": 1.0}
    with pytest.raises(EtoroValidationError):
        EtoroClient._parse_order_result(payload)


def test_optional_fields_may_be_genuinely_absent_without_raising() -> None:
    """The flip side: a non-identity field the broker simply did not state
    parses to None. Only silence about a *miss* is forbidden."""
    payload = _real_lookup_payload()
    del payload["positionExecutions"][0]["openingData"]
    del payload["positionExecutions"][0]["initialExposureAccountCurrency"]
    del payload["requestedUnits"]
    del payload["requestedAmount"]
    result = EtoroClient._parse_order_result(payload)
    assert result.status is OrderStatus.FILLED
    assert result.position_id == "3598679249"
    assert result.fill_units is None
    assert result.fill_price is None
    assert result.fill_amount_usd is None


def test_non_filled_order_without_a_position_id_is_fine() -> None:
    """Before a fill there genuinely is no position yet; None there is a
    fact, not a parse miss."""
    payload = _real_lookup_payload()
    payload["status"] = {"id": 11, "name": "WaitingForMarket"}
    payload["positionExecutions"] = []
    result = EtoroClient._parse_order_result(payload)
    assert result.status is OrderStatus.WAITING_FOR_MARKET
    assert result.position_id is None
    assert result.is_filled is False


def test_broker_error_code_becomes_a_visible_reject_reason() -> None:
    payload = _real_lookup_payload()
    payload["status"] = {"id": 4, "name": "Rejected", "errorCode": 1207}
    result = EtoroClient._parse_order_result(payload)
    assert result.status is OrderStatus.REJECTED
    assert result.reject_reason is not None
    assert "1207" in result.reject_reason


def test_parse_failure_is_never_read_as_never_received_by_recovery() -> None:
    """`recover_by_reference_id` returning None means "safe to resubmit".
    A payload we could not parse must never be allowed to authorise a
    duplicate live order -- only the API's own 404 may."""
    scripted = ScriptedTransport(
        [httpx.Response(200, json={"orderId": "o-x", "status": {"name": "Filled"}})]
    )
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    with pytest.raises(EtoroValidationError):
        client.recover_by_reference_id("some-ref")


# ===========================================================================
# Rate limiting integration
# ===========================================================================


def test_429_response_honours_retry_after_header() -> None:
    scripted = ScriptedTransport(
        [
            httpx.Response(429, headers={"Retry-After": "5"}, json={"error": "rate limited"}),
            httpx.Response(200, json={"credit": 50.0, "orders": [], "ordersForOpen": []}),
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
        [
            httpx.Response(
                200,
                json={"credit": 1.0, "orders": [], "ordersForOpen": []},
                headers={"RateLimit-Remaining": "2"},
            )
        ]
    )
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    client.get_balance()
    assert client._limiter.bucket("read").available() == pytest.approx(2, abs=0.01)


# ===========================================================================
# Retry logic: 5xx retried, 429 retried, other 4xx never retried
# ===========================================================================


def test_5xx_is_retried_with_backoff_then_succeeds() -> None:
    scripted = ScriptedTransport(
        [
            httpx.Response(502, text="bad gateway"),
            httpx.Response(
                200, json={"credit": 100.0, "orders": [], "ordersForOpen": []}
            ),
        ]
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
        return httpx.Response(
            200, json={"credit": 10.0, "orders": [], "ordersForOpen": []}
        )

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
                    # Deliberately the FLAT spelling, to keep the
                    # lower-precedence fallback path covered alongside the
                    # real nested shape exercised elsewhere in this file.
                    "orderId": "o-99",
                    "referenceId": request_id,
                    "status": 3,
                    "positionId": "p-99",
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


def test_search_instrument_accepts_top_level_list_payload() -> None:
    scripted = ScriptedTransport(
        [httpx.Response(200, json=[{"instrumentId": 123, "internalSymbolFull": "SPY"}])]
    )
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    assert client.search_instrument("SPY").instrument_id == 123


def test_search_instrument_rejects_unrecognized_object_envelope() -> None:
    scripted = ScriptedTransport(
        [
            httpx.Response(
                200,
                json={"instrumentId": 123, "internalSymbolFull": "SPY"},
            )
        ]
    )
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    with pytest.raises(EtoroValidationError, match="array of objects"):
        client.search_instrument("SPY")


def test_fuzzy_search_result_is_rejected_and_never_cached() -> None:
    cached: list[Any] = []

    class CacheSpy:
        def get_instrument(self, symbol: str) -> None:
            return None

        def upsert_instrument(self, instrument: Any) -> None:
            cached.append(instrument)

    scripted = ScriptedTransport(
        [
            httpx.Response(
                200,
                json={"items": [{"instrumentId": 999, "internalSymbolFull": "SPYD"}]},
            )
        ]
    )
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler), repo=CacheSpy())
    with pytest.raises(EtoroValidationError, match="exact instrument"):
        client.resolve_instrument_id("SPY")
    assert cached == []


@pytest.mark.parametrize(
    "items",
    [
        [{"instrumentId": 123.9, "internalSymbolFull": "SPY"}],
        [
            {"instrumentId": 123, "internalSymbolFull": "SPY"},
            {"instrumentId": 124, "internalSymbolFull": "SPY"},
        ],
    ],
)
def test_search_instrument_rejects_lossy_or_ambiguous_exact_ids(
    items: list[dict[str, Any]],
) -> None:
    scripted = ScriptedTransport([httpx.Response(200, json={"items": items})])
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    with pytest.raises(EtoroValidationError):
        client.search_instrument("SPY")


def test_duplicate_exact_search_rows_are_ok_only_when_the_id_agrees() -> None:
    scripted = ScriptedTransport(
        [
            httpx.Response(
                200,
                json={
                    "items": [
                        {"instrumentId": 123, "internalSymbolFull": "SPY"},
                        {"instrumentId": 123, "internalSymbolFull": "SPY"},
                    ]
                },
            )
        ]
    )
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    assert client.search_instrument("SPY").instrument_id == 123


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
    """No marker in the body AND an inconclusive segment probe -> no raise.

    "Unable to confirm" stays "no mismatch detected", never an exception --
    callers needing positive confirmation must check probe_environment()
    themselves (order_latency.evaluate_demo_confirmation does).
    """
    client = EtoroClient(
        _config(),
        transport=httpx.MockTransport(
            _env_router(balances={"credit": 100.0}, demo_status=403, real_status=403)
        ),
    )
    client.verify_environment()  # inconclusive must not raise


# ---------------------------------------------------------------------------
# 2026-09-11 regression suite: verify_environment was a SILENT NO-OP.
#
# _detect_environment only scanned the TOP LEVEL of the /balances body. The
# live body (captured read-only today under both configs, saved at
# data/probe_2026-09-11/env_probe_real/ and env_probe_demo/) carries no marker
# at any depth, so the function returned None unconditionally -- on the REAL
# account as much as on demo -- and gate 7 guarded nothing.
#
# LIVE_BALANCES_* below are the captured bodies verbatim, balance floats
# included, deliberately NOT trimmed to the "interesting" keys: the whole
# point is that a test built from a hand-written idea of the payload is what
# let this ship in the first place.
# ---------------------------------------------------------------------------

#: Captured verbatim 2026-09-11 from GET /api/v1/balances, config environment
#: = "real" (data/probe_2026-09-11/env_probe_real/20260911T150945Z_balances_raw.json).
LIVE_BALANCES_REAL = {
    "gcid": 18049964,
    "totalBalance": 56792.27,
    "displayCurrency": "USD",
    "balances": [
        {
            "accountId": "17761031",
            "accountType": "Trading",
            "subType": None,
            "balance": 56792.27,
            "currency": "USD",
            "displayBalance": 56792.27,
            "displayCurrency": "USD",
            "exchangeRate": 1,
        }
    ],
}

#: Captured verbatim 2026-09-11 from GET /api/v1/balances, config environment
#: = "demo" (data/probe_2026-09-11/env_probe_demo/20260911T151136Z_balances_raw.json).
#: Structurally IDENTICAL to the real one -- same gcid, same accountId, same
#: accountType, same subType. Only the balance float differs, and by 0.03%.
LIVE_BALANCES_DEMO = {
    "gcid": 18049964,
    "totalBalance": 56775.82,
    "displayCurrency": "USD",
    "balances": [
        {
            "accountId": "17761031",
            "accountType": "Trading",
            "subType": None,
            "balance": 56775.82,
            "currency": "USD",
            "displayBalance": 56775.82,
            "displayCurrency": "USD",
            "exchangeRate": 1,
        }
    ],
}

#: The live 403 body eToro returns for the path segment a credential is NOT
#: bound to (captured today in both directions).
LIVE_403_BODY = {
    "errorCode": "InsufficientPermissions",
    "errorMessage": "UserToken does not have permission to access",
}


def _env_router(
    *,
    balances: dict[str, Any],
    demo_status: int,
    real_status: int,
) -> Any:
    """A MockTransport handler routing by path, not by call order.

    verify_environment may make one request (marker found in the body) or
    three (marker absent -> two segment probes), so an order-based
    ScriptedTransport would couple these tests to that branch.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/balances":
            return httpx.Response(200, json=balances)
        if path == "/api/v1/trading/info/demo/portfolio":
            return httpx.Response(
                demo_status,
                json={"clientPortfolio": {"positions": []}} if demo_status < 300 else LIVE_403_BODY,
            )
        if path == "/api/v1/trading/info/portfolio":
            return httpx.Response(
                real_status,
                json={"clientPortfolio": {"positions": []}} if real_status < 300 else LIVE_403_BODY,
            )
        raise AssertionError(f"unexpected path {path!r}")

    return handler


def test_detect_environment_returns_none_for_the_real_live_balances_body() -> None:
    """REGRESSION: the exact payload the old top-level scan silently missed.

    This must stay None -- the body genuinely has no marker. The assertion
    that matters is paired with the test below: a detector that returned
    "real" here would be inventing a signal.
    """
    assert EtoroClient._detect_environment(LIVE_BALANCES_REAL) is None


def test_detect_environment_cannot_tell_the_live_demo_and_real_bodies_apart() -> None:
    """REGRESSION, the core finding: /balances cannot discriminate at all.

    Both live bodies yield the same verdict, so ANY gate relying solely on
    this function is a no-op. This test is what would have caught the
    shipped defect: it fails the moment someone claims /balances can confirm
    an environment.
    """
    assert EtoroClient._detect_environment(LIVE_BALANCES_REAL) == (
        EtoroClient._detect_environment(LIVE_BALANCES_DEMO)
    )
    assert EtoroClient._detect_environment(LIVE_BALANCES_DEMO) is None


def test_detect_environment_does_not_mistake_account_type_trading_for_an_environment() -> None:
    """The nested key IS named accountType -- but "Trading" is an account
    kind. A recursive scan that accepted any accountType value would now
    confidently return garbage."""
    assert EtoroClient._detect_environment({"balances": [{"accountType": "Trading"}]}) is None


def test_detect_environment_now_descends_into_nested_structures() -> None:
    """The specific bug: markers below the top level were invisible. If eToro
    ever adds one in the per-account rows, it must be found."""
    assert EtoroClient._detect_environment({"balances": [{"accountType": "Demo"}]}) == "demo"
    assert EtoroClient._detect_environment({"balances": [{"isDemo": True}]}) == "demo"
    assert EtoroClient._detect_environment({"a": {"b": {"c": {"environment": "real"}}}}) == "real"


def test_probe_environment_positively_identifies_demo() -> None:
    """Live-observed pattern: a demo-bound credential gets 200 on the demo
    segment and 403 on the real one."""
    client = EtoroClient(
        _config(),
        transport=httpx.MockTransport(
            _env_router(balances=LIVE_BALANCES_DEMO, demo_status=200, real_status=403)
        ),
    )
    assert client.probe_environment() == "demo"


def test_probe_environment_positively_identifies_real() -> None:
    """The mirror image, also observed live today."""
    client = EtoroClient(
        _config(
            environment=EtoroEnvironment.REAL,
            secrets={
                "ETORO_PRIVATE_KEY": "real_write_private_key_1234567890",
                "ETORO_READ_PRIVATE_KEY": "real_read_private_key_1234567890",
                "ETORO_PUBLIC_KEY": "real_shared_public_key_1234567890",
            },
        ),
        transport=httpx.MockTransport(
            _env_router(balances=LIVE_BALANCES_REAL, demo_status=403, real_status=200)
        ),
    )
    assert client.probe_environment() == "real"


def test_probe_environment_returns_none_when_both_segments_are_allowed() -> None:
    """Not a discriminating answer -> None (refuse), never a coin flip."""
    client = EtoroClient(
        _config(),
        transport=httpx.MockTransport(
            _env_router(balances=LIVE_BALANCES_DEMO, demo_status=200, real_status=200)
        ),
    )
    assert client.probe_environment() is None


def test_probe_environment_returns_none_when_both_segments_are_denied() -> None:
    client = EtoroClient(
        _config(),
        transport=httpx.MockTransport(
            _env_router(balances=LIVE_BALANCES_DEMO, demo_status=403, real_status=403)
        ),
    )
    assert client.probe_environment() is None


def test_probe_environment_returns_none_when_a_probe_fails_inconclusively() -> None:
    """A 500 says nothing about the credential's binding and must not be
    collapsed into "denied" -- otherwise a flaky server manufactures a
    confident environment verdict."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/trading/info/demo/portfolio":
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"clientPortfolio": {"positions": []}})

    client = EtoroClient(
        _config(), transport=httpx.MockTransport(handler), max_attempts=1
    )
    assert client.probe_environment() is None


def test_verify_environment_now_detects_a_mismatch_the_body_alone_could_not() -> None:
    """REGRESSION, the severity case: configured demo, but the credential is
    REAL-bound. The old implementation returned silently here -- on the real
    account -- because the body scan found nothing. It must now raise."""
    client = EtoroClient(
        _config(),  # configured demo
        transport=httpx.MockTransport(
            _env_router(balances=LIVE_BALANCES_REAL, demo_status=403, real_status=200)
        ),
    )
    with pytest.raises(EtoroEnvironmentMismatchError):
        client.verify_environment()


def test_verify_environment_accepts_a_matching_segment_probe() -> None:
    client = EtoroClient(
        _config(),  # configured demo
        transport=httpx.MockTransport(
            _env_router(balances=LIVE_BALANCES_DEMO, demo_status=200, real_status=403)
        ),
    )
    client.verify_environment()  # must not raise


def test_a_body_with_no_marker_and_no_probe_signal_still_yields_none() -> None:
    """Step 4's third case: unable to confirm stays unable to confirm.

    The harness gate consumes exactly this None and refuses on it; nothing
    in today's fix softens that path.
    """
    from nightwatch.probes.order_latency import evaluate_demo_confirmation

    client = EtoroClient(
        _config(),
        transport=httpx.MockTransport(
            _env_router(balances=LIVE_BALANCES_DEMO, demo_status=403, real_status=403)
        ),
    )
    detected = EtoroClient._detect_environment(LIVE_BALANCES_DEMO)
    if detected is None:
        detected = client.probe_environment()
    assert detected is None

    decision = evaluate_demo_confirmation(
        config_environment="demo",
        credential_label="demo:write",
        verify_environment_ok=True,
        verify_environment_error=None,
        detected_signal=detected,
    )
    assert decision.confirmed is False
    assert any("did not POSITIVELY confirm demo" in r for r in decision.reasons)


def test_a_positive_demo_probe_is_what_finally_lets_the_gate_confirm() -> None:
    """The counterpart: the fix makes confirmation POSSIBLE (that was the
    point), without touching any of the other three signals."""
    from nightwatch.probes.order_latency import evaluate_demo_confirmation

    decision = evaluate_demo_confirmation(
        config_environment="demo",
        credential_label="demo:write",
        verify_environment_ok=True,
        verify_environment_error=None,
        detected_signal="demo",
    )
    assert decision.confirmed is True


# ===========================================================================
# Misc: Date header capture (feeds preflight gate 4 elsewhere)
# ===========================================================================


def test_last_server_date_is_captured_from_response_headers() -> None:
    scripted = ScriptedTransport(
        [
            httpx.Response(
                200,
                json={"credit": 1.0, "orders": [], "ordersForOpen": []},
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
# board nightwatch/handoff-2026-09-04-market-data.md §5) -- the `currentRate`/`invested`/
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


def test_trade_history_accepts_a_bare_list_body_as_the_live_api_returns() -> None:
    """The live endpoint returns a bare JSON *list*, not an ``{"items": ...}``
    envelope -- proven on-device 2026-09-05, where the previous code raised
    ``AttributeError: 'list' object has no attribute 'get'`` and aborted the
    whole fee audit.

    The old line read ``data.get("items", data if isinstance(data, list)
    else [])``, which *looks* like it handles a list and cannot: ``data.get``
    is evaluated before the fallback expression is ever considered. Every
    pre-existing trade-history test wrapped its rows in ``{"items": ...}``,
    so the real shape was never exercised -- the fixture encoded the wrong
    assumption rather than testing it. This test asserts the live shape
    directly.
    """
    rows = [
        {
            "positionId": 3000000001,
            "instrumentId": 100001,
            "isBuy": True,
            "leverage": 1,
            "units": 2.0,
            "openRate": 20.0,
            "closeRate": 25.0,
            "netProfit": 10.0,
            "investment": 40.0,
            "fees": -0.5,
            "openTimestamp": "2024-01-02T14:30:00Z",
            "closeTimestamp": "2024-06-03T14:30:00Z",
        }
    ]
    scripted = ScriptedTransport([httpx.Response(200, json=rows)])
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    entries = client.get_trade_history(start_date="2020-01-01", end_date="2026-12-31")
    assert len(entries) == 1
    assert entries[0].position_id == "3000000001"
    assert entries[0].fees == -0.5


def test_trade_history_still_accepts_the_items_envelope() -> None:
    """The list fix must not break the envelope form -- it is unknown whether
    eToro returns one shape on one endpoint/version and the other elsewhere,
    and this project has already been burned by assuming a convention
    transfers (board nightwatch/handoff-2026-09-05.md §9, the positionID/positionId casing
    trap)."""
    rows = [
        {
            "positionId": 42,
            "instrumentId": 100001,
            "isBuy": True,
            "leverage": 1,
            "units": 1.0,
            "openRate": 10.0,
            "closeRate": 11.0,
            "netProfit": 1.0,
            "investment": 10.0,
            "fees": 0.0,
            "openTimestamp": "2024-01-02T14:30:00Z",
            "closeTimestamp": "2024-06-03T14:30:00Z",
        }
    ]
    scripted = ScriptedTransport([httpx.Response(200, json={"items": rows})])
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    entries = client.get_trade_history(start_date="2020-01-01", end_date="2026-12-31")
    assert len(entries) == 1
    assert entries[0].position_id == "42"


def test_trade_history_raises_on_a_body_that_is_neither_list_nor_object() -> None:
    """A silent empty list here would report "no trade history" for an account
    with 120 closed trades -- the exact silent-default failure mode this
    client has now been bitten by five times. Fail loudly instead."""
    scripted = ScriptedTransport([httpx.Response(200, json="unexpected")])
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    with pytest.raises(EtoroValidationError):
        client.get_trade_history(start_date="2020-01-01", end_date="2026-12-31")


# ===========================================================================
# Broker-side take-profit / stop-loss parsing (DESIGN.md §6.A "Broker-side
# exits", 2026-09-11). These keys were always in the portfolio payload and
# were simply never read, so the app could not see a broker-authored exit it
# never asked for.
#
# The three rows below are REAL, verbatim rows from the live-account capture
# `data/probe_2026-09-11/adopt_fetch/adopt_portfolio_raw.json` (230 open
# positions, instrumentID 1084 == SCHG) -- not hand-built fixtures. Trimmed
# only of fields irrelevant here; every key asserted on is byte-identical to
# the capture.
# ===========================================================================

#: A typical position: takeProfitRate / openRate == 11.00 exactly, i.e. the
#: broker's declared defaultTakeProfitPercentage of +1000% for the SCHG
#: leverage config this app orders into. 229 of the 230 live rows look like
#: this; all 230 carry isNoStopLoss == true with stopLossRate 0.0001.
_REAL_TP_ROW_11X: dict[str, Any] = {
    "positionID": 2867253750, "CID": 17761031,
    "openDateTime": "2024-11-27T14:30:41.173Z", "openRate": 27.65,
    "instrumentID": 1084, "isBuy": True, "takeProfitRate": 304.15,
    "stopLossRate": 0.0001, "amount": 200.0, "leverage": 1,
    "units": 7.233273, "isNoTakeProfit": False, "isNoStopLoss": True,
}

#: A drifted one (10.9x) -- the TP rate is set at open and does not track
#: later changes, so the ratio is not uniformly 11.00 across the book.
_REAL_TP_ROW_10_9X: dict[str, Any] = {
    "positionID": 3486954727, "CID": 17761031,
    "openDateTime": "2026-06-18T13:30:23.643Z", "openRate": 33.82,
    "instrumentID": 1084, "isBuy": True, "takeProfitRate": 368.5,
    "stopLossRate": 0.0001, "amount": 500.0, "leverage": 1,
    "units": 14.784151, "isNoTakeProfit": False, "isNoStopLoss": True,
}

#: The single live position with NO take-profit (positionID 3550406905) --
#: proof that "no TP" is representable in this exact payload shape, and the
#: reason the boolean flag, not the rate, is authoritative: the rate here is
#: 0.0, which is present-and-falsy, not absent.
_REAL_NO_TP_ROW: dict[str, Any] = {
    "positionID": 3550406905, "CID": 17761031,
    "openDateTime": "2026-08-19T13:30:09.647Z", "openRate": 35.37,
    "instrumentID": 1084, "isBuy": True, "takeProfitRate": 0.0,
    "stopLossRate": 0.0001, "amount": 500.0, "leverage": 1,
    "units": 14.136273, "isNoTakeProfit": True, "isNoStopLoss": True,
}


def test_parse_position_reads_the_broker_take_profit_it_used_to_discard() -> None:
    position = EtoroClient._parse_position(_REAL_TP_ROW_11X)
    assert position.take_profit_rate == pytest.approx(304.15)
    assert position.is_no_take_profit is False
    # The live shape: an 11x take-profit nobody in this app requested.
    assert position.take_profit_rate / position.entry_price == pytest.approx(11.0, abs=1e-9)


def test_parse_position_reads_the_stop_loss_pair_too() -> None:
    """Falls out of the same payload keys; every live row carries
    isNoStopLoss == true alongside a nominal 0.0001 stopLossRate."""
    position = EtoroClient._parse_position(_REAL_TP_ROW_11X)
    assert position.stop_loss_rate == pytest.approx(0.0001)
    assert position.is_no_stop_loss is True


def test_parse_position_preserves_the_no_take_profit_case_exactly() -> None:
    """takeProfitRate 0.0 is present-and-falsy: it must parse to 0.0, not
    None, and isNoTakeProfit must survive as True."""
    position = EtoroClient._parse_position(_REAL_NO_TP_ROW)
    assert position.take_profit_rate == 0.0
    assert position.take_profit_rate is not None
    assert position.is_no_take_profit is True


def test_parse_position_tolerates_a_payload_with_no_take_profit_keys_at_all() -> None:
    """The pre-2026-09-11 fixtures (and any older payload shape) have neither
    key. That must parse, not raise: these fields carry no position-identity
    meaning, unlike units/openRate/amount."""
    position = EtoroClient._parse_position(_REAL_SCHG_ROWS[0])
    assert position.take_profit_rate is None
    assert position.is_no_take_profit is False
    assert position.stop_loss_rate is None
    assert position.is_no_stop_loss is False


def test_close_position_reads_the_real_nested_order_for_close_shape() -> None:
    """Confirmed live 2026-09-11 (see
    data/probe_2026-09-11/order_latency_run3/RAW_close_position_response_body.json):
    a close response carries NO top-level orderId/orderID/id -- the id is at
    `orderForClose.orderID`. The old `or`-chain returned "", and polling with
    an empty order id gets a 400, so a successfully submitted close became
    untrackable."""
    body = {
        "orderForClose": {
            "positionID": 3598687197,
            "instrumentID": 1084,
            "orderID": 380818802,
            "orderType": 19,
            "statusID": 1,
        },
        "token": "tok",
    }
    scripted = ScriptedTransport([httpx.Response(200, json=body)])
    client = EtoroClient(
        _config(), transport=httpx.MockTransport(scripted.handler), sleep=_no_sleep
    )
    order_id = client.close_position(
        request_id=str(uuid.uuid4()), position_id="3598687197", instrument_id=1084
    )
    assert order_id == "380818802"
    assert client.last_write_response_body == body


def test_close_write_http_rejection_preserves_body_without_retrying() -> None:
    body = {"message": "Position is already closed", "code": "POSITION_CLOSED"}
    scripted = ScriptedTransport([httpx.Response(422, json=body)])
    client = EtoroClient(
        _config(), transport=httpx.MockTransport(scripted.handler), sleep=_no_sleep
    )
    with pytest.raises(EtoroValidationError) as caught:
        client.close_position(
            request_id=str(uuid.uuid4()), position_id="p1", instrument_id=1084
        )
    assert caught.value.status_code == 422
    assert client.last_write_response_body == body
    assert len(scripted.requests) == 1


def test_write_response_with_no_order_id_anywhere_never_returns_empty_string() -> None:
    """An untrackable close fails closed without guessing a recovery route."""
    scripted = ScriptedTransport(
        [
            httpx.Response(200, json={"somethingElse": {"nope": 1}}),
        ]
    )
    client = EtoroClient(
        _config(), transport=httpx.MockTransport(scripted.handler), sleep=_no_sleep
    )
    with pytest.raises(EtoroValidationError, match="unverified") as caught:
        client.close_position(
            request_id=str(uuid.uuid4()), position_id="p1", instrument_id=1084
        )
    assert str(caught.value) == (
        "close_position: response had no order id; close recovery by reference is "
        "unverified (T024) -- reconcile from portfolio, do not resubmit"
    )
    assert len(scripted.requests) == 1
    assert scripted.requests[0].method == "POST"
    assert client.last_write_response_body == {"somethingElse": {"nope": 1}}


def test_buy_write_response_without_order_id_recovers_by_reference() -> None:
    request_id = str(uuid.uuid4())
    scripted = ScriptedTransport([
        httpx.Response(200, json={}),
        httpx.Response(200, json={
            "orderId": "o-99", "referenceId": request_id, "status": 1,
        }),
    ])
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler))
    assert client.place_buy_order(request_id, instrument_id=42, amount_usd=100.0) == "o-99"
    assert [request.method for request in scripted.requests] == ["POST", "GET"]
    assert scripted.requests[1].url.path == "/api/v2/trading/info/demo/orders:lookup"
    assert scripted.requests[1].url.params["referenceId"] == request_id
