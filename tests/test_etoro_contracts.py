from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

import httpx
import pytest

from nightwatch.config import EtoroConfig, EtoroEnvironment, TelegramConfig
from nightwatch.etoro.errors import EtoroValidationError
from nightwatch.etoro.client import EtoroClient
from nightwatch.etoro.contracts import CONTRACTS, PARSER_CONTRACTS, check_payload
from nightwatch.etoro.models import OrderStatus
from nightwatch.notify.telegram import TelegramNotifier


FIXTURES = Path(__file__).parent / "fixtures" / "etoro_contracts"
SAMPLES = {
    "balances_real.json": "balance",
    "balances_demo.json": "balance",
    "portfolio_real.json": "portfolio-envelope",
    "portfolio_demo.json": "portfolio-envelope",
    "pnl_real.json": "pnl",
    "pnl_demo.json": "pnl",
    "candles.json": "candle",
    "rates.json": "rates",
    "eligibility.json": "eligibility",
    "instruments.json": "instruments",
    "costs.json": "costs",
    "trade_history.json": "trade-history",
    "orders_lookup.json": "order-lookup",
    "legacy_order_lookup.json": "legacy-order-lookup",
    "close_response.json": "close-response",
}


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_every_sanitized_observed_payload_matches_its_declared_contract() -> None:
    assert len(SAMPLES) == 15
    for filename, contract in SAMPLES.items():
        report = check_payload(contract, _fixture(filename))
        assert report.violations == (), (filename, report.violations)


def test_golden_mutations_report_missing_wrong_type_and_unexpected_shape_once() -> None:
    good = _fixture("orders_lookup.json")
    assert check_payload("order-lookup", {**good, "addedByBroker": True}).violations == ()

    cases = []
    missing = copy.deepcopy(good)
    del missing["status"]["id"]
    cases.append((missing, "missing", "$.status.id"))

    wrong_type = copy.deepcopy(good)
    wrong_type["status"]["id"] = {"value": 3}
    cases.append((wrong_type, "wrong_type", "$.status.id"))

    new_enum = copy.deepcopy(good)
    new_enum["status"]["name"] = "NewBrokerStatus"
    cases.append((new_enum, "unexpected_shape", "$.status.name"))

    for payload, kind, path in cases:
        alerts = []
        report = check_payload("order-lookup", payload, on_violation=alerts.append)
        assert any(issue.kind == kind and issue.path == path for issue in report.violations)
        assert alerts == [report]  # one alert-ready summary for this endpoint call
        assert all("90000" not in issue.expected + issue.actual for issue in report.violations)

    legacy = _fixture("legacy_order_lookup.json")
    del legacy["statusID"]
    assert any(
        issue.kind == "missing" and issue.path == "$.statusID"
        for issue in check_payload("legacy-order-lookup", legacy).violations
    )


def test_captured_order_shapes_reach_the_parser_fields() -> None:
    order = EtoroClient._parse_order_result(_fixture("orders_lookup.json"))
    assert (order.status, order.position_id) == (OrderStatus.FILLED, "900004")
    assert order.fill_units == 1.25
    assert order.fill_price == 1.25

    legacy = EtoroClient._parse_order_result(_fixture("legacy_order_lookup.json"))
    assert (legacy.status, legacy.position_id) == (OrderStatus.FILLED, "900032")
    assert legacy.fill_units == 1.415628
    assert legacy.fill_price == 35.29

    client = object.__new__(EtoroClient)
    response = httpx.Response(200, json=_fixture("close_response.json"))
    order_id = EtoroClient._order_id_from_write_response(
        client, response, request_id="sanitized-fixture", what="close_position"
    )
    assert order_id == "900005"


def test_parser_inventory_requires_a_contract_declaration_for_new_parsers() -> None:
    root = Path(__file__).resolve().parents[1]
    client = ast.parse((root / "app/src/main/python/nightwatch/etoro/client.py").read_text())
    models = ast.parse((root / "app/src/main/python/nightwatch/etoro/models.py").read_text())
    candidates: set[str] = set()
    prefixes = ("get_", "search_", "lookup_", "_parse_", "_detect_environment")
    extra = {
        "_fetch_portfolio_envelope", "_closed_on_or_before",
        "_min_position_amount_for_real_long", "_order_id_from_write_response",
        "_execution_rows", "_id_string", "_first_float",
    }
    for node in client.body:
        if isinstance(node, ast.ClassDef) and node.name == "EtoroClient":
            candidates |= {
                f"EtoroClient.{item.name}"
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and (item.name.startswith(prefixes) or item.name in extra)
            }
    candidates |= {
        node.name for node in models.body
        if isinstance(node, ast.FunctionDef) and node.name == "compute_available_cash"
    }
    assert candidates == set(PARSER_CONTRACTS)
    assert all(name in CONTRACTS for name in PARSER_CONTRACTS.values())


def test_unobserved_endpoints_cannot_be_mistaken_for_validated_contracts() -> None:
    report = check_payload("market-search-unobserved", [{"items": []}])
    assert report.complete is False
    assert report.violations == ()
    assert CONTRACTS["market-search-unobserved"].gaps


def test_client_fails_closed_for_order_drift_and_advises_on_quote_drift(repo) -> None:
    broken_order = _fixture("orders_lookup.json")
    del broken_order["status"]["id"]
    broken_rate = {
        "rates": [{"instrumentID": 424242, "ask": 2.0, "lastExecution": 1.9, "date": "now"}]
    }
    responses = iter([broken_order, broken_order, broken_rate, broken_rate])
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json=next(responses))
    )
    notifier = TelegramNotifier(TelegramConfig(enabled=True, secrets={}), repo)
    client = EtoroClient(
        EtoroConfig(
            environment=EtoroEnvironment.DEMO,
            secrets={"ETORO_PUBLIC_KEY": "public", "ETORO_DEMO_PRIVATE_KEY": "private"},
        ),
        transport=transport,
        contract_notifier=notifier,
    )
    try:
        for _ in range(2):
            with pytest.raises(EtoroValidationError, match="status object has no 'id'"):
                client.lookup_order(order_id="900005")

        for _ in range(2):
            assert client.get_rates([424242])[424242].bid == 0.0

        rows = repo.conn.execute(
            "SELECT event, body, status FROM notifications ORDER BY id"
        ).fetchall()
        assert len(rows) == 2  # one per distinct drift despite repeated polls
        assert {row["event"] for row in rows} == {"etoro_contract_drift"}
        assert {row["status"] for row in rows} == {"skipped"}  # absent secrets are a no-op
        assert all("900005" not in row["body"] for row in rows)
        assert any("order-lookup" in row["body"] for row in rows)
        assert any("rates" in row["body"] for row in rows)
    finally:
        client.close()
        notifier.close()


def test_lookup_order_rejects_a_different_order_identity() -> None:
    payload = _fixture("orders_lookup.json")
    client = EtoroClient(
        EtoroConfig(
            environment=EtoroEnvironment.DEMO,
            secrets={"ETORO_PUBLIC_KEY": "public", "ETORO_DEMO_PRIVATE_KEY": "private"},
        ),
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)),
    )
    with pytest.raises(EtoroValidationError, match="expected 123"):
        client.lookup_order(order_id="123")
