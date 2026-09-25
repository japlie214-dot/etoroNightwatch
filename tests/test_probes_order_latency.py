"""Tests for nightwatch.probes.order_latency (the 2026-09-11 order-latency
measurement harness's off-device-testable logic: RUNBOOK_LIVE_WINDOW_
2026-09-11.md §8(b), DESIGN_session.md §3.2/§3.4/§4.8/§4.9).

Two families of test here, mirroring this project's own established idioms:

* Pure-arithmetic tests (statistics, the T-30 projection, the
  demo-confirmation decision logic, summary rendering) -- entirely offline,
  hand-built inputs, no broker at all, like ``tests/test_probes_live_
  window.py``/``tests/test_probes_fee_audit.py``.
* Broker-mocked tests for ``run_open_leg``/``run_close_leg`` -- a real
  ``EtoroClient`` wired to ``httpx.MockTransport`` with a scripted response
  queue, exactly ``tests/test_etoro_client.py``'s own idiom. No network call
  is ever made; ``sleep``/``clock`` are injected fakes so timing assertions
  are deterministic.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from typing import Any

import httpx
import pytest

from nightwatch.config import EtoroConfig, EtoroEnvironment
from nightwatch.etoro.client import EtoroClient
from nightwatch.etoro.errors import EtoroServerError, EtoroTimeoutError, EtoroValidationError
from nightwatch.probes.order_latency import (
    DemoConfirmation,
    LegStatistics,
    ProjectionResult,
    T30_BUDGET_SECONDS,
    evaluate_demo_confirmation,
    project_session_budget,
    render_human_summary,
    run_close_leg,
    run_open_leg,
    summarize_leg,
)

DEMO_API_KEY = "demo_private_key_for_tests_1234567890"
DEMO_USER_KEY = "demo_public_key_for_tests_0987654321"


def _config(environment: EtoroEnvironment = EtoroEnvironment.DEMO) -> EtoroConfig:
    return EtoroConfig(
        environment=environment,
        secrets={"ETORO_DEMO_PRIVATE_KEY": DEMO_API_KEY, "ETORO_PUBLIC_KEY": DEMO_USER_KEY},
    )


class ScriptedTransport:
    """FIFO queue of canned responses -- mirrors test_etoro_client.py's own
    helper of the same name exactly, duplicated here rather than imported
    since tests/ has no shared fixture module for it today."""

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


class FakeClock:
    """Deterministic monotonic clock: starts at ``start``, +``step`` per call."""

    def __init__(self, start: float = 0.0, step: float = 1.0) -> None:
        self.value = start
        self.step = step

    def __call__(self) -> float:
        v = self.value
        self.value += self.step
        return v


def _wall_clock() -> str:
    # Fixed, since these tests never assert on the literal wall-clock string,
    # only that one is present per StatusObservation.
    return "2026-09-11T20:00:00.000000Z"


def _no_sleep(_seconds: float) -> None:
    pass


# ===========================================================================
# summarize_leg
# ===========================================================================


def test_summarize_leg_reports_mean_median_stdev_and_spread():
    stats = summarize_leg("close_full", [2.0, 4.0, 6.0])
    assert stats.leg == "close_full"
    assert stats.n == 3
    assert stats.mean_s == pytest.approx(4.0)
    assert stats.median_s == pytest.approx(4.0)
    assert stats.stdev_s == pytest.approx(2.0)
    assert stats.min_s == 2.0
    assert stats.max_s == 6.0
    assert stats.spread_s == pytest.approx(4.0)


def test_summarize_leg_single_sample_has_no_stdev_but_still_summarizes():
    # The task's own ask: variance must be visible, not just the mean -- a
    # single sample cannot show variance, and this must say so honestly
    # (None), never silently report 0.0 (a false claim of "no variance").
    stats = summarize_leg("open", [5.0])
    assert stats.n == 1
    assert stats.stdev_s is None
    assert stats.spread_s == 0.0


def test_summarize_leg_raises_on_empty_input_rather_than_faking_a_result():
    with pytest.raises(ValueError):
        summarize_leg("close_partial", [])


# ===========================================================================
# project_session_budget -- §4.9's order-count multiplier vs T-30
# ===========================================================================


def test_project_session_budget_fits_comfortably_at_todays_lot_count():
    # A fast, realistic per-order latency (a few seconds) times 2-3 lots is
    # nowhere near a 30-minute budget.
    proj = project_session_budget(5.0, orders_low=2, orders_high=3, label="today")
    assert proj.projected_seconds_low == pytest.approx(10.0)
    assert proj.projected_seconds_high == pytest.approx(15.0)
    assert proj.fits_low is True
    assert proj.fits_high is True
    assert proj.margin_low_seconds == pytest.approx(T30_BUDGET_SECONDS - 10.0)


def test_project_session_budget_can_fail_to_fit_at_a_slow_per_order_latency():
    # A deliberately pathological per-order latency (10 minutes/order) times
    # 10 lots (the mature-book case, §4.9) blows through T-30 many times
    # over -- this is exactly the finding this harness exists to surface if
    # Friday's measurement comes back slow.
    proj = project_session_budget(600.0, orders_low=10, orders_high=10, label="mature book")
    assert proj.projected_seconds_low == pytest.approx(6000.0)
    assert proj.fits_low is False
    assert proj.fits_high is False
    assert proj.margin_low_seconds < 0


def test_project_session_budget_rejects_inverted_bounds():
    with pytest.raises(ValueError):
        project_session_budget(5.0, orders_low=10, orders_high=2, label="bad")


def test_project_session_budget_rejects_negative_latency():
    with pytest.raises(ValueError):
        project_session_budget(-1.0, orders_low=1, orders_high=2, label="bad")


# ===========================================================================
# evaluate_demo_confirmation -- the fail-closed safety gate
# ===========================================================================


def test_demo_confirmation_true_only_when_every_signal_agrees():
    result = evaluate_demo_confirmation(
        config_environment="demo",
        credential_label="demo:write",
        verify_environment_ok=True,
        verify_environment_error=None,
        detected_signal="demo",
    )
    assert result.confirmed is True
    assert result.detected_signal == "demo"
    assert any("positively signals demo" in r for r in result.reasons)


def test_demo_confirmation_refuses_when_config_says_real():
    result = evaluate_demo_confirmation(
        config_environment="real",
        credential_label="real:write",
        verify_environment_ok=True,
        verify_environment_error=None,
        detected_signal="demo",  # even if everything else somehow said demo
    )
    assert result.confirmed is False
    assert any("REFUSING" in r and "not 'demo'" in r for r in result.reasons)


def test_demo_confirmation_refuses_on_detected_mismatch():
    result = evaluate_demo_confirmation(
        config_environment="demo",
        credential_label="demo:write",
        verify_environment_ok=False,
        verify_environment_error="live /balances signal='real' disagrees with configured environment='demo'",
        detected_signal="real",
    )
    assert result.confirmed is False
    assert any("mismatch" in r for r in result.reasons)


def test_demo_confirmation_refuses_on_ambiguous_signal_not_just_on_mismatch():
    # The crux of the hard safety requirement: "unable to confirm" must be
    # refused exactly like "confirmed real" -- not treated as a pass because
    # nothing contradicted it.
    result = evaluate_demo_confirmation(
        config_environment="demo",
        credential_label="demo:write",
        verify_environment_ok=True,
        verify_environment_error=None,
        detected_signal=None,
    )
    assert result.confirmed is False
    assert any("did not POSITIVELY confirm demo" in r for r in result.reasons)


def test_demo_confirmation_refuses_on_credential_label_mismatch():
    result = evaluate_demo_confirmation(
        config_environment="demo",
        credential_label="real:write",  # inconsistent with config_environment
        verify_environment_ok=True,
        verify_environment_error=None,
        detected_signal="demo",
    )
    assert result.confirmed is False
    assert any("does not start with 'demo:'" in r for r in result.reasons)


def test_demo_confirmation_reasons_are_never_empty_either_way():
    confirmed = evaluate_demo_confirmation(
        config_environment="demo", credential_label="demo:write",
        verify_environment_ok=True, verify_environment_error=None, detected_signal="demo",
    )
    refused = evaluate_demo_confirmation(
        config_environment="real", credential_label="real:write",
        verify_environment_ok=True, verify_environment_error=None, detected_signal=None,
    )
    assert len(confirmed.reasons) > 0
    assert len(refused.reasons) > 0


# ===========================================================================
# render_human_summary -- smoke tests, refusal path must never show orders
# ===========================================================================


def test_render_human_summary_refusal_path_never_mentions_placing_orders():
    demo = DemoConfirmation(confirmed=False, reasons=("REFUSING: not demo",), detected_signal=None)
    text = render_human_summary(
        demo=demo, leg_stats={}, projections=[], partial_fill_events=[],
        timed_out_events=[], left_open_positions=[],
    )
    assert "NO ORDERS WERE PLACED" in text
    assert "REFUSING: not demo" in text


def test_render_human_summary_confirmed_path_reports_stats_and_fit():
    demo = DemoConfirmation(confirmed=True, reasons=("ok",), detected_signal="demo")
    stats = {"open": summarize_leg("open", [3.0, 4.0, 5.0])}
    proj = project_session_budget(4.0, orders_low=2, orders_high=3, label="today")
    text = render_human_summary(
        demo=demo, leg_stats=stats, projections=[proj], partial_fill_events=[],
        timed_out_events=[], left_open_positions=[],
    )
    assert "open" in text
    assert "FITS" in text
    assert "No leg timed out." in text
    assert "Cleanup confirmed" in text


def test_render_human_summary_flags_left_open_positions_loudly():
    demo = DemoConfirmation(confirmed=True, reasons=("ok",), detected_signal="demo")
    text = render_human_summary(
        demo=demo, leg_stats={}, projections=[], partial_fill_events=[],
        timed_out_events=[], left_open_positions=["p-123: opened, never confirmed closed"],
    )
    assert "MANUAL CLEANUP REQUIRED" in text
    assert "p-123" in text


# ===========================================================================
# run_open_leg / run_close_leg -- broker mocked via httpx.MockTransport
# (this project's own established offline idiom, tests/test_etoro_client.py)
# ===========================================================================


def test_run_open_leg_records_every_transition_and_reaches_terminal():
    responses = [
        httpx.Response(200, json={"orderId": "o-1"}),  # place_buy_order
        httpx.Response(200, json={"orderId": "o-1", "status": 1}),   # RECEIVED
        httpx.Response(200, json={"orderId": "o-1", "status": 11}),  # WAITING_FOR_MARKET
        httpx.Response(200, json={"orderId": "o-1", "status": 11}),  # same -- no new observation
        httpx.Response(
            200,
            json={"orderId": "o-1", "status": 3, "positionId": "pos-1", "units": 1.5, "executionPrice": 66.0},
        ),  # FILLED
    ]
    scripted = ScriptedTransport(responses)
    client = EtoroClient(
        _config(), transport=httpx.MockTransport(scripted.handler), sleep=_no_sleep,
    )
    trace = run_open_leg(
        client, request_id=str(uuid.uuid4()), instrument_id=1084, amount_usd=50.0,
        repeat_index=0, clock=FakeClock(start=0.0, step=1.0), wall_clock=_wall_clock,
        sleep=_no_sleep, poll_interval_s=1.0, poll_timeout_s=30.0,
    )
    assert trace.leg == "open"
    assert trace.timed_out is False
    assert trace.terminal_status == "FILLED"
    assert trace.position_id == "pos-1"
    # Four distinct statuses observed (RECEIVED, WAITING_FOR_MARKET, FILLED)
    # -- the repeated WAITING_FOR_MARKET poll must NOT produce a duplicate
    # observation (dedup is the whole point of "per transition", not "per poll").
    observed_statuses = [o.status for o in trace.observations]
    assert observed_statuses == ["RECEIVED", "WAITING_FOR_MARKET", "FILLED"]
    # Monotonically increasing elapsed time across observations.
    elapsed = [o.elapsed_since_submission_s for o in trace.observations]
    assert elapsed == sorted(elapsed)
    assert trace.time_to_terminal_s is not None
    assert trace.time_to_terminal_s > 0


def test_run_open_leg_times_out_and_never_reports_a_fake_latency():
    responses = [
        httpx.Response(200, json={"orderId": "o-2"}),
        httpx.Response(200, json={"orderId": "o-2", "status": 11}),
        httpx.Response(200, json={"orderId": "o-2", "status": 11}),
        httpx.Response(200, json={"orderId": "o-2", "status": 11}),
    ]
    scripted = ScriptedTransport(responses)
    client = EtoroClient(
        _config(), transport=httpx.MockTransport(scripted.handler), sleep=_no_sleep,
    )
    trace = run_open_leg(
        client, request_id=str(uuid.uuid4()), instrument_id=1084, amount_usd=50.0,
        repeat_index=0, clock=FakeClock(start=0.0, step=5.0), wall_clock=_wall_clock,
        sleep=_no_sleep, poll_interval_s=1.0, poll_timeout_s=9.0,
    )
    assert trace.timed_out is True
    assert trace.time_to_terminal_s is None  # never a fabricated latency figure
    assert trace.terminal_status == "WAITING_FOR_MARKET"


def test_open_lookup_404_uses_reference_id_and_traces_both_responses():
    request_id = str(uuid.uuid4())
    missing_by_order_id = {"message": "Order category for o-open not found"}
    filled = {
        "orderId": "o-open",
        "referenceId": request_id,
        "status": 3,
        "positionId": "pos-open",
    }
    scripted = ScriptedTransport(
        [
            httpx.Response(200, json={"orderId": "o-open"}),
            httpx.Response(404, json=missing_by_order_id),
            httpx.Response(200, json=filled),
        ]
    )
    client = EtoroClient(
        _config(), transport=httpx.MockTransport(scripted.handler), sleep=lambda _: None
    )
    trace = run_open_leg(
        client, request_id=request_id, instrument_id=1084, amount_usd=50.0,
        repeat_index=0, clock=FakeClock(), wall_clock=_wall_clock, sleep=lambda _: None,
    )
    assert trace.terminal_status == "FILLED"
    assert trace.order_id == "o-open"
    assert trace.position_id == "pos-open"
    assert trace.observations[0].route == "v1_order_id_demo"
    assert [r["status_code"] for r in trace.raw_open_lookup_responses] == [404, 200]
    assert json.loads(trace.raw_open_lookup_responses[0]["body"]) == missing_by_order_id
    assert json.loads(trace.raw_open_lookup_responses[1]["body"]) == filled
    assert len(scripted.requests) == 3  # one POST, v2 orderId GET, and v1 orderId GET
    assert scripted.requests[1].url.path == "/api/v2/trading/info/demo/orders:lookup"
    assert scripted.requests[1].url.params.get("orderId") == "o-open"
    assert scripted.requests[2].url.path == "/api/v1/trading/info/demo/orders/o-open"
    assert all(request.url.params.get("referenceId") is None for request in scripted.requests[1:])


def test_persistent_open_lookup_404_times_out_with_submission_and_raw_trace():
    request_id = str(uuid.uuid4())
    error_body = {"message": "order not visible"}
    scripted = ScriptedTransport(
        [httpx.Response(200, json={"orderId": "o-open"})]
        + [httpx.Response(404, json=error_body) for _ in range(9)]
    )
    client = EtoroClient(
        _config(), transport=httpx.MockTransport(scripted.handler), sleep=lambda _: None
    )

    with pytest.raises(EtoroTimeoutError, match="order o-open was not visible") as caught:
        run_open_leg(
            client, request_id=request_id, instrument_id=1084, amount_usd=50.0,
            repeat_index=1, clock=FakeClock(), wall_clock=_wall_clock, sleep=lambda _: None,
            poll_interval_s=1.0, poll_timeout_s=3.0,
        )

    evidence = caught.value.open_leg_trace
    assert evidence["request_id"] == request_id
    assert evidence["order_id"] == "o-open"
    assert evidence["raw_submission_response"] == {"orderId": "o-open"}
    assert len(evidence["raw_open_lookup_responses"]) == 9
    assert all(r["status_code"] == 404 for r in evidence["raw_open_lookup_responses"])
    assert all(json.loads(r["body"]) == error_body for r in evidence["raw_open_lookup_responses"])
    assert [request.method for request in scripted.requests] == ["POST"] + ["GET"] * 9
    lookup_requests = scripted.requests[1:]
    for index in range(0, len(lookup_requests), 3):
        v2_order_id, v1_order_id, v2_reference_id = lookup_requests[index:index + 3]
        assert v2_order_id.url.path == "/api/v2/trading/info/demo/orders:lookup"
        assert v2_order_id.url.params.get("orderId") == "o-open"
        assert v1_order_id.url.path == "/api/v1/trading/info/demo/orders/o-open"
        assert v2_reference_id.url.path == "/api/v2/trading/info/demo/orders:lookup"
        assert v2_reference_id.url.params.get("referenceId") == request_id


def test_open_lookup_rejects_response_for_a_different_order():
    scripted = ScriptedTransport([
        httpx.Response(200, json={"orderId": "o-open"}),
        httpx.Response(200, json={"orderId": "other", "status": 3, "positionId": "pos-1"}),
    ])
    client = EtoroClient(
        _config(), transport=httpx.MockTransport(scripted.handler), sleep=lambda _: None
    )
    with pytest.raises(EtoroValidationError, match="expected o-open") as caught:
        run_open_leg(
            client, request_id="r-open", instrument_id=1084, amount_usd=50.0,
            repeat_index=0, clock=FakeClock(), wall_clock=_wall_clock, sleep=lambda _: None,
        )
    assert caught.value.open_leg_trace["order_id"] == "o-open"


def test_lookup_route_not_sticky_after_transient_404():
    request_id = str(uuid.uuid4())
    scripted = ScriptedTransport([
        httpx.Response(200, json={"orderId": "o-open"}),
        httpx.Response(404, json={"message": "not visible by orderId"}),
        httpx.Response(404, json={"message": "not visible in v1"}),
        httpx.Response(200, json={
            "orderId": "o-open", "referenceId": request_id, "status": 1,
        }),
        httpx.Response(200, json={
            "orderId": "o-open", "referenceId": request_id, "status": 3,
            "positionId": "pos-open",
        }),
    ])
    client = EtoroClient(
        _config(), transport=httpx.MockTransport(scripted.handler), sleep=lambda _: None
    )

    trace = run_open_leg(
        client, request_id=request_id, instrument_id=1084, amount_usd=50.0,
        repeat_index=0, clock=FakeClock(), wall_clock=_wall_clock, sleep=lambda _: None,
    )

    assert [observation.status for observation in trace.observations] == ["RECEIVED", "FILLED"]
    assert [observation.route for observation in trace.observations] == [
        "v2_reference_id", "v2_order_id",
    ]
    lookups = scripted.requests[1:]
    assert [request.url.path for request in lookups] == [
        "/api/v2/trading/info/demo/orders:lookup",
        "/api/v1/trading/info/demo/orders/o-open",
        "/api/v2/trading/info/demo/orders:lookup",
        "/api/v2/trading/info/demo/orders:lookup",
    ]
    assert lookups[0].url.params.get("orderId") == "o-open"
    assert lookups[2].url.params.get("referenceId") == request_id
    assert lookups[3].url.params.get("orderId") == "o-open"


def test_every_observation_has_route_label():
    scripted = ScriptedTransport([
        httpx.Response(200, json={"orderId": "o-open"}),
        httpx.Response(200, json={"orderId": "o-open", "status": 3, "positionId": "2001"}),
        httpx.Response(200, json={"orderId": "3003"}),
        httpx.Response(200, json={
            "orderID": 3003, "statusID": 3, "positions": [{"positionID": 2001}],
        }),
    ])
    client = EtoroClient(
        _config(), transport=httpx.MockTransport(scripted.handler), sleep=lambda _: None
    )
    open_trace = run_open_leg(
        client, request_id="r-open", instrument_id=1084, amount_usd=50.0,
        repeat_index=0, clock=FakeClock(), wall_clock=_wall_clock, sleep=lambda _: None,
    )
    close_trace = run_close_leg(
        client, leg="close_full", request_id="r-close", position_id="2001",
        instrument_id=1084, units_to_deduct=None, repeat_index=0,
        clock=FakeClock(), wall_clock=_wall_clock, sleep=lambda _: None,
    )

    allowed = {
        "v2_order_id", "v1_order_id_demo", "v2_reference_id", "close_order_lookup",
    }
    observations = open_trace.observations + close_trace.observations
    assert observations
    assert all(observation.route in allowed for observation in observations)
    assert [observation.route for observation in observations] == [
        "v2_order_id", "close_order_lookup",
    ]
    assert dataclasses.asdict(open_trace)["observations"][0]["route"] == "v2_order_id"
    assert dataclasses.asdict(close_trace)["observations"][0]["route"] == "close_order_lookup"


def test_real_env_open_lookup_never_calls_v1():
    request_id = str(uuid.uuid4())
    real_config = EtoroConfig(
        environment=EtoroEnvironment.REAL,
        secrets={
            "ETORO_PRIVATE_KEY": "real_private_key_for_tests",
            "ETORO_PUBLIC_KEY": "public_key_for_tests",
        },
    )
    scripted = ScriptedTransport([
        httpx.Response(200, json={"orderId": "o-real"}),
        httpx.Response(404, json={"message": "not visible by orderId"}),
        httpx.Response(200, json={
            "orderId": "o-real", "referenceId": request_id, "status": 3,
            "positionId": "pos-real",
        }),
    ])
    client = EtoroClient(
        real_config, transport=httpx.MockTransport(scripted.handler), sleep=lambda _: None
    )

    trace = run_open_leg(
        client, request_id=request_id, instrument_id=1084, amount_usd=50.0,
        repeat_index=0, clock=FakeClock(), wall_clock=_wall_clock, sleep=lambda _: None,
    )

    assert trace.observations[0].route == "v2_reference_id"
    lookup_requests = scripted.requests[1:]
    assert [request.url.path for request in lookup_requests] == [
        "/api/v2/trading/info/orders:lookup",
        "/api/v2/trading/info/orders:lookup",
    ]
    assert lookup_requests[0].url.params.get("orderId") == "o-real"
    assert lookup_requests[1].url.params.get("referenceId") == request_id
    assert all("/api/v1/" not in request.url.path for request in lookup_requests)


def test_lookup_paths_issue_no_writes():
    request_id = str(uuid.uuid4())
    scripted = ScriptedTransport([
        httpx.Response(200, json={"orderId": "o-open"}),
        httpx.Response(404, json={"message": "not visible by orderId"}),
        httpx.Response(404, json={"message": "not visible in v1"}),
        httpx.Response(200, json={
            "orderId": "o-open", "referenceId": request_id, "status": 3,
            "positionId": "2002",
        }),
        httpx.Response(200, json={"orderId": "3004"}),
        httpx.Response(200, json={
            "orderID": 3004, "statusID": 3, "positions": [{"positionID": 2002}],
        }),
    ])
    client = EtoroClient(
        _config(), transport=httpx.MockTransport(scripted.handler), sleep=lambda _: None
    )
    run_open_leg(
        client, request_id=request_id, instrument_id=1084, amount_usd=50.0,
        repeat_index=0, clock=FakeClock(), wall_clock=_wall_clock, sleep=lambda _: None,
    )
    run_close_leg(
        client, leg="close_full", request_id="r-close", position_id="2002",
        instrument_id=1084, units_to_deduct=None, repeat_index=0,
        clock=FakeClock(), wall_clock=_wall_clock, sleep=lambda _: None,
    )

    assert [request.method for request in scripted.requests] == [
        "POST", "GET", "GET", "GET", "POST", "GET",
    ]


@pytest.mark.parametrize("failure", ["timeout", "server_error"])
def test_open_submit_exception_captures_exact_request_and_never_retries(failure: str):
    response_body = {"message": "upstream unavailable"}

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("response timed out after submission", request=request)

    scripted = ScriptedTransport(
        [timeout]
        if failure == "timeout"
        else [httpx.Response(503, json=response_body)]
    )
    client = EtoroClient(
        _config(), transport=httpx.MockTransport(scripted.handler), sleep=lambda _: None
    )
    error_type = EtoroTimeoutError if failure == "timeout" else EtoroServerError

    with pytest.raises(error_type) as caught:
        run_open_leg(
            client, request_id="r-ambiguous", instrument_id=1084, amount_usd=50.0,
            repeat_index=0, clock=FakeClock(), wall_clock=_wall_clock, sleep=lambda _: None,
        )

    evidence = caught.value.open_leg_trace
    expected_request = {
        "action": "open", "transaction": "buy", "instrumentId": 1084,
        "orderType": "mkt", "leverage": 1, "amount": 50.0,
        "orderCurrency": "usd", "settlementType": "real",
    }
    assert evidence["request_id"] == "r-ambiguous"
    assert evidence["order_id"] is None
    assert evidence["raw_submission_request"] == expected_request
    assert json.loads(scripted.requests[0].content) == expected_request
    assert len(scripted.requests) == 1  # ambiguous writes are never replayed
    assert evidence["raw_submission_response"] == (
        None if failure == "timeout" else response_body
    )
    assert evidence["raw_open_lookup_responses"] == []


def test_run_close_leg_full_close_sends_null_units_to_deduct_and_times_it():
    responses = [
        httpx.Response(200, json={"orderId": "3003"}),  # close_position
        httpx.Response(200, json={
            "orderID": 3003, "statusID": 3, "positions": [{"positionID": 2002}],
        }),  # FILLED
    ]
    scripted = ScriptedTransport(responses)
    client = EtoroClient(
        _config(), transport=httpx.MockTransport(scripted.handler), sleep=_no_sleep,
    )
    trace = run_close_leg(
        client, leg="close_full", request_id=str(uuid.uuid4()), position_id="2002",
        instrument_id=1084, units_to_deduct=None, repeat_index=0,
        clock=FakeClock(start=0.0, step=2.0), wall_clock=_wall_clock, sleep=_no_sleep,
        poll_interval_s=1.0, poll_timeout_s=30.0,
    )
    assert trace.leg == "close_full"
    assert trace.terminal_status == "FILLED"
    assert trace.timed_out is False
    close_body = json.loads(scripted.requests[0].content)
    assert close_body == {"InstrumentId": 1084, "UnitsToDeduct": None}
    assert trace.raw_submission_request == close_body
    assert trace.raw_close_lookup_responses[0]["status_code"] == 200
    assert json.loads(trace.raw_close_lookup_responses[0]["body"]) == {
        "orderID": 3003, "statusID": 3, "positions": [{"positionID": 2002}],
    }


def test_close_poll_uses_dedicated_endpoint_and_traces_raw_bodies():
    request_id = str(uuid.uuid4())
    close_body = {
        "orderForClose": {
            "positionID": 2004,
            "instrumentID": 1084,
            "orderID": 382291007,
            "orderType": 19,
            "statusID": 1,
        },
        "token": "close-token",
    }
    not_yet_visible = {"message": "close order not found yet"}
    filled = {"orderID": 382291007, "statusID": 3, "positions": [{"positionID": 2004}]}
    scripted = ScriptedTransport(
        [
            httpx.Response(200, json=close_body),
            httpx.Response(404, json=not_yet_visible),
            httpx.Response(200, json=filled),
        ]
    )
    client = EtoroClient(
        _config(), transport=httpx.MockTransport(scripted.handler), sleep=lambda _: None
    )
    trace = run_close_leg(
        client, leg="close_full", request_id=request_id, position_id="2004",
        instrument_id=1084, units_to_deduct=None, repeat_index=0,
        clock=FakeClock(start=0.0, step=1.0), wall_clock=_wall_clock, sleep=lambda _: None,
        poll_interval_s=1.0, poll_timeout_s=30.0,
    )

    assert trace.terminal_status == "FILLED"
    assert trace.raw_submission_response == close_body
    assert trace.raw_submission_request == {"InstrumentId": 1084, "UnitsToDeduct": None}
    assert [o.raw_lookup_body for o in trace.observations] == [filled]
    assert scripted.requests[0].method == "POST"
    assert scripted.requests[1].url.path.endswith("/api/v1/trading/info/demo/close-orders/382291007")
    assert all(r.url.path == scripted.requests[1].url.path for r in scripted.requests[1:])
    assert [r["status_code"] for r in trace.raw_close_lookup_responses] == [404, 200]
    assert [json.loads(r["body"]) for r in trace.raw_close_lookup_responses] == [
        not_yet_visible, filled,
    ]
    assert len(scripted.requests) == 3  # one close POST, then read-only close-order polls


def test_persistent_close_lookup_404_fails_at_deadline_and_never_falls_back_or_resubmits():
    post_body = {
        "orderForClose": {"positionID": 2005, "orderID": 382291008},
        "token": "close-token",
    }
    error_body = {"message": "close order not found"}
    scripted = ScriptedTransport(
        [httpx.Response(200, json=post_body)]
        + [httpx.Response(404, json=error_body) for _ in range(4)]
    )
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler), sleep=lambda _: None)

    with pytest.raises(EtoroTimeoutError, match="was not visible") as caught:
        run_close_leg(
            client, leg="close_full", request_id=str(uuid.uuid4()), position_id="2005",
            instrument_id=1084, units_to_deduct=None, repeat_index=0,
            clock=FakeClock(), wall_clock=_wall_clock, sleep=lambda _: None,
            poll_interval_s=1.0, poll_timeout_s=3.0,
        )

    evidence = caught.value.close_leg_trace
    assert evidence["raw_submission_response"] == post_body
    assert evidence["raw_submission_request"] == {"InstrumentId": 1084, "UnitsToDeduct": None}
    assert len(evidence["raw_close_lookup_responses"]) == 3
    assert all(r["status_code"] == 404 for r in evidence["raw_close_lookup_responses"])
    assert all(json.loads(r["body"]) == error_body for r in evidence["raw_close_lookup_responses"])
    assert all(r["method"] == "GET" for r in evidence["raw_close_lookup_responses"])
    assert all(
        r["path"] == "/api/v1/trading/info/demo/close-orders/382291008"
        for r in evidence["raw_close_lookup_responses"]
    )
    assert len(scripted.requests) == 4  # one POST plus three GET attempts before the bound
    assert [request.method for request in scripted.requests] == ["POST", "GET", "GET", "GET"]
    assert all(
        request.url.path == "/api/v1/trading/info/demo/close-orders/382291008"
        for request in scripted.requests[1:]
    )
    assert all(request.url.params.get("referenceId") is None for request in scripted.requests[1:])


def test_filled_close_with_mismatched_position_id_fails_closed():
    scripted = ScriptedTransport([
        httpx.Response(200, json={"orderId": "3006"}),
        httpx.Response(200, json={
            "orderID": 3006, "statusID": 3, "positions": [{"positionID": 2007}],
        }),
    ])
    client = EtoroClient(_config(), transport=httpx.MockTransport(scripted.handler), sleep=lambda _: None)

    with pytest.raises(EtoroValidationError, match="expected '2006'") as caught:
        run_close_leg(
            client, leg="close_full", request_id=str(uuid.uuid4()), position_id="2006",
            instrument_id=1084, units_to_deduct=None, repeat_index=0,
            clock=FakeClock(), wall_clock=_wall_clock, sleep=lambda _: None,
        )

    assert json.loads(caught.value.close_leg_trace["raw_close_lookup_responses"][0]["body"])[
        "positions"
    ] == [{"positionID": 2007}]
    assert len(scripted.requests) == 2


def test_run_close_leg_partial_close_sends_the_units_to_deduct_value():
    # PARTIALLY_FILLED (status 5) is deliberately NOT in
    # TERMINAL_ORDER_STATUSES (nightwatch.etoro.models) -- an order can sit
    # in it mid-poll and still resolve further, so this scripts one
    # PARTIALLY_FILLED poll followed by a genuine terminal FILLED, and
    # checks that the mid-flight partial fill is still flagged via
    # `is_partial_fill` even though it is not the terminal status.
    responses = [
        httpx.Response(200, json={"orderId": "3004"}),  # close_position
        httpx.Response(200, json={"orderID": 3004, "statusID": 5, "positions": [{"positionID": 2003}]}),  # PARTIALLY_FILLED
        httpx.Response(200, json={"orderID": 3004, "statusID": 3, "positions": [{"positionID": 2003}]}),  # FILLED (terminal)
    ]
    scripted = ScriptedTransport(responses)
    client = EtoroClient(
        _config(), transport=httpx.MockTransport(scripted.handler), sleep=_no_sleep,
    )
    trace = run_close_leg(
        client, leg="close_partial", request_id=str(uuid.uuid4()), position_id="2003",
        instrument_id=1084, units_to_deduct=0.75, repeat_index=0,
        clock=FakeClock(start=0.0, step=2.0), wall_clock=_wall_clock, sleep=_no_sleep,
        poll_interval_s=1.0, poll_timeout_s=30.0,
    )
    assert trace.leg == "close_partial"
    assert trace.terminal_status == "FILLED"
    assert [o.status for o in trace.observations] == ["PARTIALLY_FILLED", "FILLED"]
    assert trace.is_partial_fill is True  # observed mid-flight, even though terminal status is FILLED
    close_body = json.loads(scripted.requests[0].content)
    assert close_body == {"InstrumentId": 1084, "UnitsToDeduct": 0.75}


def test_run_close_leg_rejects_an_invalid_leg_label():
    client = EtoroClient(
        _config(), transport=httpx.MockTransport(ScriptedTransport([]).handler), sleep=_no_sleep,
    )
    with pytest.raises(ValueError):
        run_close_leg(
            client, leg="not_a_real_leg", request_id=str(uuid.uuid4()), position_id="pos-9",
            instrument_id=1084, units_to_deduct=None, repeat_index=0,
            clock=FakeClock(), wall_clock=_wall_clock, sleep=_no_sleep,
        )


def test_run_close_leg_cleanup_leg_is_distinguishable_from_measured_full_close():
    # A cleanup close (flattening a partial-close remainder) must carry its
    # own leg label so the driver never folds it into the measured
    # close_full statistics -- this is what keeps the three named legs'
    # numbers honest.
    responses = [
        httpx.Response(200, json={"orderId": "3005"}),
        httpx.Response(200, json={"orderID": 3005, "statusID": 3, "positions": [{"positionID": 2004}]}),
    ]
    scripted = ScriptedTransport(responses)
    client = EtoroClient(
        _config(), transport=httpx.MockTransport(scripted.handler), sleep=_no_sleep,
    )
    trace = run_close_leg(
        client, leg="close_cleanup", request_id=str(uuid.uuid4()), position_id="2004",
        instrument_id=1084, units_to_deduct=None, repeat_index=0,
        clock=FakeClock(), wall_clock=_wall_clock, sleep=_no_sleep,
    )
    assert trace.leg == "close_cleanup"
    assert trace.leg != "close_full"
