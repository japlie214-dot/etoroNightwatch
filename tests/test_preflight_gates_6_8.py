"""Auth, positive environment confirmation and eligibility gates, entirely offline."""

import json

import httpx
import pytest

from nightwatch.config import EtoroConfig, EtoroEnvironment, SymbolConfig
from nightwatch.db.repo import Repo
from nightwatch.engine.preflight import (
    GateResult,
    GateSpec,
    gate_6_auth,
    gate_7_environment,
    gate_8_instruments,
    run_preflight,
)
from nightwatch.etoro.client import EtoroClient
from nightwatch.etoro.errors import EtoroServerError

DEMO_API_KEY = "demo_private_key_for_tests_1234567890"
DEMO_USER_KEY = "demo_public_key_for_tests_0987654321"
DEMO_PORTFOLIO = "/api/v1/trading/info/demo/portfolio"
REAL_PORTFOLIO = "/api/v1/trading/info/portfolio"
SEARCH = "/api/v1/market-data/search"
ELIGIBILITY = "/api/v2/trading/info/demo/eligibility"
IDS = {"SPY": 1, "SCHG": 2, "UPRO": 3}


def _config(
    environment: EtoroEnvironment = EtoroEnvironment.DEMO,
    secrets: dict[str, str] | None = None,
) -> EtoroConfig:
    secrets = secrets if secrets is not None else {
        "ETORO_DEMO_PRIVATE_KEY": DEMO_API_KEY,
        "ETORO_PUBLIC_KEY": DEMO_USER_KEY,
    }
    return EtoroConfig(environment=environment, secrets=secrets)


def _no_sleep(_seconds: float) -> None:
    pass


class ScriptedTransport:
    """Route canned responses by endpoint and symbol/id, recording requests."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.portfolio = {"clientPortfolio": {
            "credit": 0, "positions": [], "orders": [], "ordersForOpen": [],
        }}
        self.demo_status = 200
        self.real_status = 403
        self.search_rows = {
            s: [{"instrumentId": instrument_id, "internalSymbolFull": s}]
            for s, instrument_id in IDS.items()
        }
        self.eligibility_status = 200
        self.eligibility_rows = {
            instrument_id: {
                "instrumentId": instrument_id,
                "allowOpenPosition": True,
                "minPositionExposure": 10,
                "leverageConfigs": [{
                    "settlementType": "real", "direction": "long", "minPositionAmount": 10,
                }],
            }
            for instrument_id in IDS.values()
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = (request.method, request.url.path)
        if route == ("GET", DEMO_PORTFOLIO):
            return httpx.Response(self.demo_status, json=self.portfolio)
        if route == ("GET", REAL_PORTFOLIO):
            return httpx.Response(self.real_status, json=self.portfolio)
        if route == ("GET", SEARCH):
            symbol = request.url.params["internalSymbolFull"]
            return httpx.Response(200, json={"items": self.search_rows[symbol]})
        if route == ("POST", ELIGIBILITY):
            ids = json.loads(request.content)["instrumentIds"]
            return httpx.Response(self.eligibility_status, json={
                "eligibilities": [self.eligibility_rows[i] for i in ids],
            })
        raise AssertionError(f"unexpected request: {route}")


@pytest.fixture
def transport() -> ScriptedTransport:
    scripted = ScriptedTransport()
    yield scripted
    assert all(
        "order" not in r.url.path and "close" not in r.url.path
        for r in scripted.requests
    )


@pytest.fixture
def client(transport: ScriptedTransport) -> EtoroClient:
    with EtoroClient(
        _config(), role="read", transport=httpx.MockTransport(transport.handler),
        sleep=_no_sleep, max_attempts=1,
    ) as client:
        yield client


def test_gate_6_authenticated_read(client: EtoroClient, transport: ScriptedTransport) -> None:
    assert gate_6_auth(client=client) == GateResult(
        6, "auth", True, "authenticated read ok", {"environment": "demo"},
    )
    assert [r.url.path for r in transport.requests] == [DEMO_PORTFOLIO]


@pytest.mark.parametrize("status, reason", [
    (401, "auth refused:"),
    (503, "auth check inconclusive: EtoroServerError:"),
])
def test_gate_6_http_failure(client, transport, status, reason) -> None:
    transport.demo_status = status
    result = gate_6_auth(client=client)
    assert (result.gate, result.name, result.passed) == (6, "auth", False)
    assert result.reason.startswith(reason)
    assert len(transport.requests) == 1


def test_gate_6_missing_credit(client, transport) -> None:
    del transport.portfolio["clientPortfolio"]["credit"]
    result = gate_6_auth(client=client)
    assert not result.passed
    assert result.reason.startswith("auth check inconclusive: EtoroValidationError:")
    assert "credit" in result.reason
    assert len(transport.requests) == 1


@pytest.mark.parametrize("demo, real, detected, passed, reason", [
    (200, 403, "demo", True, "credential bound to demo"),
    (403, 200, "real", False, "config says demo, credential is bound to real"),
    (200, 200, None, False, "credential environment could not be confirmed"),
    (503, 403, None, False, "credential environment could not be confirmed"),
])
def test_gate_7_probe(client, transport, demo, real, detected, passed, reason) -> None:
    transport.demo_status = demo
    transport.real_status = real
    assert gate_7_environment(client=client) == GateResult(
        7, "environment", passed, reason, {"configured": "demo", "detected": detected},
    )
    assert [r.url.path for r in transport.requests] == [DEMO_PORTFOLIO, REAL_PORTFOLIO]


def test_gate_7_escaping_etoro_error(client, monkeypatch) -> None:
    def fail():
        raise EtoroServerError("probe failed")

    monkeypatch.setattr(client, "probe_environment", fail)
    assert gate_7_environment(client=client) == GateResult(
        7, "environment", False, "environment probe error: EtoroServerError: probe failed",
        {"configured": "demo", "detected": None},
    )


@pytest.mark.parametrize("gate, method", [
    (gate_6_auth, "get_balance"),
    (gate_7_environment, "probe_environment"),
    (gate_8_instruments, "resolve_instrument_id"),
])
def test_non_etoro_errors_propagate(client, monkeypatch, gate, method) -> None:
    def fail(*args):
        raise RuntimeError("local bug")

    monkeypatch.setattr(client, method, fail)
    kwargs = {"client": client}
    if gate is gate_8_instruments:
        kwargs.update(symbols=SymbolConfig(), order_sizes={"SCHG": 50, "UPRO": 50})
    with pytest.raises(RuntimeError, match="local bug"):
        gate(**kwargs)


@pytest.mark.parametrize("schg_size, schg_min", [(50, 10), (500, 100)])
def test_gate_8_instruments_ok(client, transport, schg_size, schg_min) -> None:
    transport.eligibility_rows[IDS["SCHG"]]["minPositionExposure"] = schg_min
    sizes = {"SCHG": schg_size, "UPRO": 50}
    assert gate_8_instruments(client=client, symbols=SymbolConfig(), order_sizes=sizes) == GateResult(
        8, "instruments", True, "instruments ok", {
            "instrument_ids": IDS,
            "order_sizes": sizes,
            "min_position_exposure": {"SCHG": schg_min, "UPRO": 10},
        },
    )
    assert [r.url.params["internalSymbolFull"] for r in transport.requests if r.url.path == SEARCH] == [
        "SPY", "SCHG", "UPRO",
    ]
    assert [json.loads(r.content)["instrumentIds"] for r in transport.requests if r.url.path == ELIGIBILITY] == [
        [IDS["SCHG"]], [IDS["UPRO"]],
    ]


@pytest.mark.parametrize("symbol, field, value, requests", [
    ("UPRO", "allowOpenPosition", False, 2),
    ("SCHG", "minPositionExposure", 100, 1),
])
def test_gate_8_not_orderable(client, transport, symbol, field, value, requests) -> None:
    transport.eligibility_rows[IDS[symbol]][field] = value
    result = gate_8_instruments(
        client=client, symbols=SymbolConfig(), order_sizes={"SCHG": 50, "UPRO": 50},
    )
    assert (result.gate, result.name, result.passed) == (8, "instruments", False)
    assert result.reason.startswith(f"{symbol} not orderable:")
    assert sum(r.url.path == ELIGIBILITY for r in transport.requests) == requests


def test_gate_8_spy_unresolvable(client, transport) -> None:
    transport.search_rows["SPY"] = []
    result = gate_8_instruments(
        client=client, symbols=SymbolConfig(), order_sizes={"SCHG": 50, "UPRO": 50},
    )
    assert not result.passed
    assert result.reason.startswith("SPY unresolvable: EtoroValidationError:")
    assert [r.url.path for r in transport.requests] == [SEARCH]


def test_gate_8_eligibility_http_error(client, transport) -> None:
    transport.eligibility_status = 503
    result = gate_8_instruments(
        client=client, symbols=SymbolConfig(), order_sizes={"SCHG": 50, "UPRO": 50},
    )
    assert not result.passed
    assert result.reason.startswith("SCHG eligibility check error: EtoroServerError:")
    assert sum(r.url.path == ELIGIBILITY for r in transport.requests) == 1


@pytest.mark.parametrize("sizes", [
    {"SCHG": 50},
    {"SCHG": 0, "UPRO": 50},
    {"SCHG": -1, "UPRO": 50},
    {"SPY": 50, "SCHG": 50, "UPRO": 50},
])
def test_gate_8_invalid_order_sizes_before_any_call(client, transport, sizes) -> None:
    with pytest.raises(ValueError, match="order_sizes"):
        gate_8_instruments(client=client, symbols=SymbolConfig(), order_sizes=sizes)
    assert transport.requests == []


def test_preflight_stops_at_auth_failure(repo: Repo, client, transport) -> None:
    session_id = repo.create_session(
        "2026-10-01", mode="dry_run", environment="demo", armed=False,
    ).id
    transport.demo_status = 401
    called = []

    def pass_gate(number):
        called.append(number)
        return GateResult(number, f"gate {number}", True, "ok")

    gates = [
        GateSpec(number, f"gate {number}", lambda number=number: pass_gate(number))
        for number in range(1, 13)
    ]
    gates[5] = GateSpec(6, "auth", lambda: gate_6_auth(client=client))
    outcome = run_preflight(gates, repo=repo, session_id=session_id)
    assert not outcome.passed
    assert outcome.failed.gate == 6
    assert outcome.failed.reason.startswith("auth refused:")
    assert called == [1, 2, 3, 4, 5]
    assert [r.gate for r in repo.get_preflight_checks(session_id)] == [1, 2, 3, 4, 5, 6]
    assert [r.url.path for r in transport.requests] == [DEMO_PORTFOLIO]
