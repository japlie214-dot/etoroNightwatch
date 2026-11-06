"""Synthetic-only regression checks for broker error diagnostics (T065)."""

import logging
import traceback

import httpx
import pytest

from nightwatch.config import EtoroEnvironment
from nightwatch.etoro.client import EtoroClient
from nightwatch.etoro.errors import (
    EtoroAuthError, EtoroNetworkError, EtoroRateLimitError, EtoroServerError,
    EtoroTimeoutError, EtoroValidationError,
)
from tests.test_etoro_client import ScriptedTransport, _config


PRIVATE = "broker note 한글"


def assert_redacted(error):
    assert PRIVATE not in str(error)
    assert PRIVATE not in "".join(traceback.format_exception(error))
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.parametrize("status,error_type", [
    (301, EtoroValidationError), (400, EtoroValidationError),
    (401, EtoroAuthError), (403, EtoroAuthError), (404, EtoroValidationError),
    (422, EtoroValidationError), (429, EtoroRateLimitError),
    (500, EtoroServerError), (503, EtoroServerError),
])
@pytest.mark.parametrize("write", [False, True])
@pytest.mark.parametrize("body_kind", ["text", "json", "invalid_utf8"])
def test_http_errors_expose_only_metadata(status, error_type, write, body_kind, caplog, monkeypatch):
    logger = logging.getLogger("nightwatch.etoro.client")
    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    monkeypatch.setattr(logger, "propagate", False)
    monkeypatch.setattr(logger, "level", logging.ERROR)
    if body_kind == "json":
        response = httpx.Response(status, json={"message": PRIVATE, "token": PRIVATE})
    else:
        body = PRIVATE.encode() + (b"\xff" if body_kind == "invalid_utf8" else b"")
        response = httpx.Response(status, content=body)
    scripted = ScriptedTransport([response] * 2)
    sleeps = []
    with EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler),
                     max_attempts=2, sleep=sleeps.append) as client:
        with pytest.raises(error_type) as caught:
            client._request("POST" if write else "GET", "/synthetic-endpoint",
                            request_class="write" if write else "read")
        assert_redacted(caught.value)
        message = str(caught.value)
        assert str(status) in message
        assert "/synthetic-endpoint" in message
        assert f"bytes={len(response.content)}" in message
        if isinstance(caught.value, (EtoroValidationError, EtoroServerError)):
            assert caught.value.status_code == status
        if status == 429:
            assert caught.value.retry_after == 1.0
        expected_attempts = 2 if not write and (status == 429 or status >= 500) else 1
        assert len(scripted.requests) == expected_attempts
        assert len(sleeps) == expected_attempts - 1
        with caplog.at_level(logging.ERROR):
            logging.getLogger("nightwatch.etoro.client").error("failed: %s", caught.value)
        assert PRIVATE not in caplog.text
        assert any(record.getMessage() == f"failed: {message}" for record in caplog.records)


@pytest.mark.parametrize("status,error_type", [
    (400, EtoroValidationError), (401, EtoroAuthError), (403, EtoroAuthError),
    (404, EtoroValidationError), (429, EtoroRateLimitError), (502, EtoroServerError),
])
@pytest.mark.parametrize("demo", [False, True])
def test_real_portfolio_path_keeps_endpoint_label_after_redaction(status, error_type, demo, caplog, monkeypatch):
    from nightwatch.logging_setup import RedactionFilter

    logger = logging.getLogger("nightwatch.etoro.client")
    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    monkeypatch.setattr(logger, "propagate", False)
    monkeypatch.setattr(logger, "level", logging.ERROR)
    response = httpx.Response(status, text=PRIVATE)
    scripted = ScriptedTransport([response])
    config = _config() if demo else _config(environment=EtoroEnvironment.REAL, secrets={
        "ETORO_PRIVATE_KEY": "fake-private", "ETORO_PUBLIC_KEY": "fake-public",
    })
    with EtoroClient(config, transport=httpx.MockTransport(scripted.handler),
                     max_attempts=1) as client:
        with pytest.raises(error_type) as caught:
            client.get_balance()
        assert scripted.requests[0].url.path == (
            "/api/v1/trading/info/demo/portfolio" if demo else "/api/v1/trading/info/portfolio"
        )
        assert_redacted(caught.value)
        message = str(caught.value)
        assert message.startswith(f"{status} from ")
        assert f"bytes={len(response.content)}" in message
        assert "endpoint=portfolio" in message
        record = logging.LogRecord(logger.name, logging.ERROR, __file__, 0,
                                   "failed: %s", (caught.value,), None)
        assert RedactionFilter().filter(record)
        logger.handle(record)
        assert "endpoint=portfolio" in caplog.text
        assert PRIVATE not in caplog.text


@pytest.mark.parametrize("fallback_status", [400, 503])
def test_lookup_fallback_errors_have_no_exception_context(fallback_status):
    scripted = ScriptedTransport([
        httpx.Response(404, text=PRIVATE), httpx.Response(fallback_status, text=PRIVATE),
    ])
    with EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler),
                     max_attempts=1) as client:
        with pytest.raises((EtoroValidationError, EtoroServerError)) as caught:
            client.lookup_order(order_id="123")
        assert_redacted(caught.value)


@pytest.mark.parametrize("call,payload", [
    (lambda c: c.get_balance(), {"credit": PRIVATE, "orders": [], "ordersForOpen": []}),
    (lambda c: c.get_portfolio(), {"positions": [{"private": PRIVATE}]}),
    (lambda c: c.get_portfolio(), {"positions": [{"positionID": PRIVATE}]}),
    (lambda c: c.get_portfolio(), {"positions": [{"positionID": "1", "instrumentID": 1,
                                                "units": PRIVATE, "openRate": 1, "amount": 1}]}),
    (lambda c: c.get_rates([1]), {"rates": [{"instrumentID": 1, "bid": PRIVATE}]}),
    (lambda c: c.lookup_order(order_id="123"), {"orderId": PRIVATE, "status": PRIVATE}),
    (lambda c: c.lookup_order(order_id="123"), {"orderId": "123", "status": 1,
                                               "units": PRIVATE}),
    (lambda c: c.clear_take_profit("ref", "123"), {"positionId": PRIVATE}),
])
def test_response_parse_errors_do_not_echo_values_or_conversion_context(call, payload):
    with EtoroClient(_config(), transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=payload))) as client:
        with pytest.raises(EtoroValidationError) as caught:
            call(client)
        assert_redacted(caught.value)


def test_rejected_order_keeps_numeric_code_not_broker_message(caplog):
    scripted = ScriptedTransport([
        httpx.Response(200, json={"orderId": "123", "status": 1}),
        httpx.Response(200, json={"orderId": "123", "status": {"id": 4, "errorCode": 1207},
                                 "rejectReason": PRIVATE}),
    ])
    with EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler),
                     sleep=lambda _: None) as client:
        with caplog.at_level(logging.WARNING):
            result = client.poll_order_to_terminal(order_id="123")
        assert result.order.reject_reason == "errorCode=1207"
        assert PRIVATE not in caplog.text


@pytest.mark.parametrize("transport_error,error_type", [
    (httpx.ReadTimeout, EtoroTimeoutError), (httpx.ReadError, EtoroNetworkError),
])
@pytest.mark.parametrize("write", [False, True])
def test_transport_errors_drop_untrusted_messages_and_chains(transport_error, error_type, write):
    attempts = []

    def fail(request):
        attempts.append(request)
        raise transport_error(PRIVATE, request=request)

    with EtoroClient(_config(), transport=httpx.MockTransport(fail),
                     max_attempts=2, sleep=lambda _: None) as client:
        with pytest.raises(error_type) as caught:
            client._request("GET", "/synthetic-endpoint", request_class="write" if write else "read")
        assert_redacted(caught.value)
        assert "/synthetic-endpoint" in str(caught.value)
        assert len(attempts) == (1 if write else 2)


@pytest.mark.parametrize("call", [
    lambda c: c.get_balance(), lambda c: c.get_portfolio(),
    lambda c: c.get_pnl(), lambda c: c.get_rates([1]),
    lambda c: c.get_candles(1), lambda c: c.get_eligibility([1]),
    lambda c: c.get_costs(1, 10), lambda c: c.get_instruments([1]),
    lambda c: c.search_instrument("SPY"), lambda c: c.lookup_order(order_id="123"),
    lambda c: c.lookup_close_order("123"), lambda c: c.verify_environment(),
    lambda c: c.get_trade_history(start_date="2026-01-01", end_date="2026-02-01"),
])
def test_json_decoding_errors_never_retain_response_body(call):
    with EtoroClient(_config(), transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=b'"' + PRIVATE.encode() + b'\xff"'))) as client:
        with pytest.raises(EtoroValidationError) as caught:
            call(client)
        assert_redacted(caught.value)


@pytest.mark.parametrize("parse,payload", [
    (EtoroClient._parse_position, {"private": PRIVATE}),
    (EtoroClient._parse_position, {"positionID": PRIVATE, "instrumentID": 1}),
    (EtoroClient._parse_trade_history_entry, {"private": PRIVATE}),
    (EtoroClient._parse_trade_history_entry, {"positionId": PRIVATE}),
    (EtoroClient._parse_candle, {"open": PRIVATE}),
    (EtoroClient._parse_order_result, {"order": PRIVATE}),
    (EtoroClient._parse_order_result, {"orderId": PRIVATE, "status": {"name": PRIVATE}}),
    (EtoroClient._parse_order_result, {"status": {"id": PRIVATE, "name": PRIVATE}}),
    (EtoroClient._parse_order_result, {"status": 1, "positionExecutions": PRIVATE}),
    (EtoroClient._parse_order_result, {"status": 1, "positionExecutions": [PRIVATE]}),
    (EtoroClient._parse_order_result, {"status": 1, "positionExecutions": [{"openingData": PRIVATE}]}),
    (EtoroClient._parse_order_result, {"orderId": PRIVATE, "status": 3}),
])
def test_direct_parser_errors_are_also_redacted(parse, payload):
    with pytest.raises(EtoroValidationError) as caught:
        if parse is EtoroClient._parse_candle:
            parse(payload, "OneDay")
        else:
            parse(payload)
    assert_redacted(caught.value)


@pytest.mark.parametrize("error_code", [1207, PRIVATE, True, {"private": PRIVATE}])
@pytest.mark.parametrize("partial", [False, True])
def test_close_reject_reason_keeps_only_numeric_code(error_code, partial):
    payload = {"orderID": "123", "statusID": 4, "errorMessage": PRIVATE,
               "errorCode": error_code, "positions": []}
    if partial:
        payload["positions"] = [{"positionID": "1"}, {"positionID": "2", "units": 1}]
    result = EtoroClient._parse_close_order_result(
        payload, expected_order_id="123", expected_position_id="1" if partial else None)
    assert result.reject_reason == ("errorCode=1207" if type(error_code) is int
                                    else "broker rejection (body=REDACTED)")
