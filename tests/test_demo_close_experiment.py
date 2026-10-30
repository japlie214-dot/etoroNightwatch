"""Offline T037 evidence through the real client and an httpx fake transport."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from nightwatch.config import EtoroConfig, EtoroEnvironment, StrategyConfig

_SPEC = importlib.util.spec_from_file_location(
    "demo_close_experiment", Path(__file__).resolve().parents[1] / "tools" / "demo_close_experiment.py"
)
experiment = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(experiment)


def config(environment=EtoroEnvironment.DEMO):
    return SimpleNamespace(etoro=EtoroConfig(environment, secrets={
        "ETORO_PUBLIC_KEY": "fake-public", "ETORO_DEMO_PRIVATE_KEY": "fake-demo",
    }), strategy=StrategyConfig(min_order_size=50))


class Broker:
    def __init__(self, report_path, *, close_error=None, reference_missing=False,
                 wrong_reference=False, partial_wrong=False):
        self.report_path = report_path
        self.close_error = close_error
        self.reference_missing = reference_missing
        self.wrong_reference = wrong_reference
        self.partial_wrong = partial_wrong
        self.positions = {"900": self.position("900", 8)}
        self.orders = {}
        self.references = {}
        self.posts = []
        self.calls = []
        self.next_id = 100
        self.cash = 10000.0

    @staticmethod
    def position(pid, units):
        return {"positionID": int(pid), "instrumentID": 77, "units": units,
                "openRate": 25, "amount": units * 25, "isBuy": True, "leverage": 1,
                "accountId": "private-account"}

    def handle(self, request):
        self.calls.append(request)
        path = request.url.path
        if request.method == "POST":
            report = json.loads(self.report_path.read_text(encoding="utf-8"))
            rid = request.headers["x-request-id"]
            assert any(row["request_id"] == rid for row in report["requests"])
            self.posts.append(request)
            self.next_id += 1
            oid = str(self.next_id)
            body = json.loads(request.content)
            if "market-close-orders" in path:
                pid = path.rsplit("/", 1)[-1]
                assert pid != "900", "must never close existing positions"
                if self.close_error == "timeout":
                    raise httpx.ReadTimeout("fake lost response", request=request)
                if self.close_error == "server":
                    return httpx.Response(503, json={"token": "fake-response-token"})
                units = body["UnitsToDeduct"]
                if units is None:
                    self.cash += self.positions.pop(pid)["amount"]
                elif not self.partial_wrong:
                    self.positions[pid]["units"] -= units
                    self.positions[pid]["amount"] -= units * 25
                    self.cash += units * 25
                result = {"orderID": int(oid), "referenceID": rid, "statusID": 3,
                          "positions": [{"positionID": int(pid), "units": units, "rate": 25}]}
                ack = {"orderForClose": {"orderID": int(oid), "positionID": int(pid)},
                       "token": "fake-response-token"}
            else:
                pid = str(self.next_id + 1000)
                self.positions[pid] = self.position(pid, body["amount"] / 25)
                self.cash -= body["amount"]
                result = {"orderId": oid, "referenceId": rid, "status": {"id": 3, "name": "Filled"},
                          "positionExecutions": [{"positionId": pid, "openingData": {
                              "units": body["amount"] / 25, "avgPrice": 25}}]}
                ack = {"orderId": oid}
            self.orders[oid] = result
            self.references[rid] = result
            return httpx.Response(200, json=ack)
        if path.endswith("portfolio"):
            return httpx.Response(200, json={"clientPortfolio": {
                "positions": list(self.positions.values()), "credit": self.cash,
                "orders": [], "ordersForOpen": [], "accountId": "private-account"}})
        if path.endswith("search"):
            return httpx.Response(200, json={"items": [{"internalSymbolFull": "SCHG", "instrumentId": 77}]})
        if "close-orders/" in path:
            return httpx.Response(200, json=self.orders[path.rsplit("/", 1)[-1]])
        if "orders:lookup" in path:
            params = request.url.params
            if "referenceId" in params:
                if self.reference_missing:
                    return httpx.Response(404, json={"message": "not indexed", "accessToken": "fake-token"})
                result = self.references[params["referenceId"]]
                if self.wrong_reference:
                    result = {**result, "positions": [{"positionID": 99999}]}
            else:
                result = self.orders[params["orderId"]]
            return httpx.Response(200, json=result)
        raise AssertionError(f"unexpected fake request: {request.method} {path}")


class Clock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


def make_run(tmp_path, **kwargs):
    run_dir, fixture_dir = tmp_path / "run", tmp_path / "fixtures"
    broker = Broker(run_dir / "report.json", **kwargs)
    clock = Clock()
    run = experiment.Experiment(config(), run_dir, fixture_dir, transport=httpx.MockTransport(broker.handle),
                                clock=clock, sleep=clock.sleep, settle_timeout=6)
    return run, broker


@pytest.mark.parametrize("legacy_visible", [False, True])
def test_open_lookup_404_then_visible_never_resends_buy(tmp_path, legacy_visible):
    run, broker = make_run(tmp_path)
    lookups, legacy = [], []

    def handle(request):
        path = request.url.path
        if "orders:lookup" in path and "orderId" in request.url.params:
            lookups.append(run.clock.now)
            if legacy_visible or len(lookups) <= 2:
                return httpx.Response(404, json={"message": "Order category not found"})
        if "/orders/" in path and request.method == "GET":
            legacy.append(request)
            if not legacy_visible:
                return httpx.Response(404, json={"message": "Order was not found"})
            return httpx.Response(200, json=broker.orders[path.rsplit("/", 1)[-1]])
        return broker.handle(request)

    run.capture.inner = httpx.MockTransport(handle)
    report = run.run([1, 2])
    assert all(s["status"] == "pass" for s in report["steps"].values())
    assert lookups == ([0] if legacy_visible else [0, 2, 4])
    assert len(legacy) == (1 if legacy_visible else 2)
    assert len(broker.posts) == 2
    assert report["unresolved_open_requests"] == report["remaining_position_ids"] == []


@pytest.mark.parametrize("cleanup_only", [False, True])
def test_open_lookup_stays_404_portfolio_recovers_owned_id_for_cleanup(tmp_path, cleanup_only):
    run, broker = make_run(tmp_path)
    lookups, portfolio_reads = [], []

    def handle(request):
        path = request.url.path
        if request.method == "GET" and ("orders:lookup" in path or "/orders/" in path):
            lookups.append(request)
            return httpx.Response(404, json={"message": "Order was not found"})
        if path.endswith("portfolio") and broker.posts and not run.full_attempted:
            portfolio_reads.append(request)
            if len(portfolio_reads) <= 2:
                return httpx.Response(200, json={"clientPortfolio": {"positions": []}})
        if request.method == "POST" and "market-close-orders" in path:
            persisted = json.loads(run.report_path.read_text())
            pid = path.rsplit("/", 1)[-1]
            assert pid in persisted["opened"] and pid in run.owned
            assert persisted["unresolved_open_requests"] == []
        response = broker.handle(request)
        if request.method == "POST" and "market-close-orders" not in path:
            # A new, unrelated instrument must not be adopted or closed.
            broker.positions["901"] = {**broker.position("901", 1), "instrumentID": 88}
        return response

    if cleanup_only:
        def step(number):
            run.open_position(run.minimum)
            raise RuntimeError("failure after recovered open")
        run.step = step
    run.capture.inner = httpx.MockTransport(handle)
    report = run.run([1])
    assert report["steps"]["1"]["status"] == ("fail" if cleanup_only else "pass")
    assert len(lookups) == 8  # Four bounded v2 lookups, each followed by the v1 GET.
    assert len(report["opened"]) == 1
    assert report["remaining_position_ids"] == report["unresolved_open_requests"] == []
    assert set(broker.positions) == {"900", "901"}
    assert len(broker.posts) == 2  # Exactly one buy and one close, including cleanup.


@pytest.mark.parametrize("visibility", ["absent", "ambiguous", "wrong_instrument"])
def test_open_lookup_404_does_not_adopt_unproven_portfolio_position(tmp_path, visibility):
    run, broker = make_run(tmp_path)

    def handle(request):
        path = request.url.path
        if request.method == "GET" and ("orders:lookup" in path or "/orders/" in path):
            return httpx.Response(404, json={"message": "not visible"})
        if path.endswith("portfolio") and broker.posts:
            rows = []
            if visibility == "ambiguous":
                rows = [broker.position("1101", 2), broker.position("1102", 2)]
            elif visibility == "wrong_instrument":
                rows = [{**broker.position("1101", 2), "instrumentID": 88}]
            return httpx.Response(200, json={"clientPortfolio": {"positions": rows}})
        return broker.handle(request)

    run.capture.inner = httpx.MockTransport(handle)
    report = run.run([1, 4, 5])
    assert list(report["steps"]) == ["1"]
    assert report["steps"]["1"]["status"] == "fail"
    assert len(report["unresolved_open_requests"]) == 1
    assert report["opened"] == [] and run.owned == {}
    assert len(broker.posts) == 1


@pytest.mark.parametrize("persistent", [False, True])
def test_reference_lookup_404_retries_bounded_without_writes(tmp_path, persistent):
    run, broker = make_run(tmp_path)
    attempts = []

    def handle(request):
        if "orders:lookup" in request.url.path and "referenceId" in request.url.params:
            attempts.append(run.clock.now)
            if persistent or len(attempts) <= 2:
                return httpx.Response(404, json={"message": "reference not indexed"})
        return broker.handle(request)

    run.capture.inner = httpx.MockTransport(handle)
    report = run.run([1, 3])
    assert report["steps"]["3"]["status"] == ("fail" if persistent else "pass")
    assert attempts == ([0, 2, 4, 6] if persistent else [0, 2, 4])
    assert len(broker.posts) == 2
    assert report["remaining_position_ids"] == []


def test_open_lookup_non_404_is_not_retried_or_resubmitted(tmp_path):
    run, broker = make_run(tmp_path)
    attempts = []

    def handle(request):
        if "orders:lookup" in request.url.path:
            attempts.append(request)
            return httpx.Response(400, json={"message": "invalid lookup"})
        return broker.handle(request)

    run.capture.inner = httpx.MockTransport(handle)
    report = run.run([1, 4])
    assert list(report["steps"]) == ["1"]
    assert len(attempts) == len(broker.posts) == 1
    assert len(report["unresolved_open_requests"]) == 1


def test_lagging_open_close_partial_and_lookup_settle_without_resends(tmp_path):
    run, broker = make_run(tmp_path)
    stale, reads_left = None, 0
    missing = {}

    def handle(request):
        nonlocal stale, reads_left
        path = request.url.path
        if path.endswith("portfolio") and reads_left:
            reads_left -= 1
            return httpx.Response(200, json=stale)
        if request.method == "GET" and "close-orders/" in path:
            oid = path.rsplit("/", 1)[-1]
            missing[oid] = missing.get(oid, 0) + 1
            if missing[oid] <= 2:
                return httpx.Response(404, json={"message": f"Order was not found for OrderID={oid}"})
        if request.method == "POST":
            stale = {"clientPortfolio": {"positions": [dict(p) for p in broker.positions.values()],
                                         "credit": broker.cash, "orders": [], "ordersForOpen": []}}
            reads_left = 2
        return broker.handle(request)

    run.capture.inner = httpx.MockTransport(handle)
    report = run.run([1, 2, 3, 4, 5])
    assert [row["status"] for row in report["steps"].values()] == ["pass"] * 5
    assert report["remaining_position_ids"] == []
    assert set(broker.positions) == {"900"}
    assert len(broker.posts) == 7
    assert all(count == 3 for count in missing.values())
    for step in (2, 4):
        result = json.loads((run.fixture_dir / f"step_{step}.json").read_text())["result"]
        assert len(result["lookup_errors"]) == 2
        assert result["lookup_errors"][0]["status_code"] == 404
        assert "ORDER_" in result["lookup_errors"][0]["error_message"]
    assert run.clock.now >= 6


@pytest.mark.parametrize("partial", [False, True])
def test_persistent_close_lookup_404_falls_back_to_settled_portfolio(tmp_path, partial):
    run, broker = make_run(tmp_path)
    reads = []

    def handle(request):
        if request.method == "GET" and "close-orders/" in request.url.path:
            reads.append(request)
            return httpx.Response(404, json={"message": "not yet visible"})
        return broker.handle(request)

    run.capture.inner = httpx.MockTransport(handle)
    report = run.run([4] if partial else [1, 2])
    assert all(row["status"] == "pass" for row in report["steps"].values())
    assert report["remaining_position_ids"] == []
    assert len(reads) == (8 if partial else 4)
    assert len([r for r in broker.posts if "market-close-orders" in r.url.path]) == (2 if partial else 1)


@pytest.mark.parametrize("appears_in_cleanup", [False, True])
def test_cleanup_tracks_order_owned_position_even_when_portfolio_empty(tmp_path, appears_in_cleanup):
    run, broker = make_run(tmp_path)
    hidden_reads = 0

    def handle(request):
        nonlocal hidden_reads
        if request.url.path.endswith("portfolio") and len(broker.positions) > 1:
            hidden_reads += 1
            if not appears_in_cleanup or hidden_reads <= 5:
                return httpx.Response(200, json={"clientPortfolio": {
                    "positions": [], "credit": broker.cash, "orders": [], "ordersForOpen": []}})
        return broker.handle(request)

    run.capture.inner = httpx.MockTransport(handle)
    report = run.run([1])
    assert report["steps"]["1"]["status"] == "fail"
    assert report["opened"]
    assert hidden_reads >= 4
    if appears_in_cleanup:
        assert report["remaining_position_ids"] == []
        assert set(broker.positions) == {"900"}
        assert len(broker.posts) == 2
    else:
        assert report["remaining_position_ids"] == report["opened"]
        assert set(report["opened"]) <= broker.positions.keys()
        assert len(broker.posts) == 1
    result = json.loads((run.fixture_dir / "step_1.json").read_text())["result"]
    assert result["error_message"] == "open not confirmed filled and present"


def test_cleanup_retains_all_three_owned_ids_when_they_never_appear(tmp_path):
    run, broker = make_run(tmp_path)

    def handle(request):
        if request.url.path.endswith("portfolio"):
            return httpx.Response(200, json={"clientPortfolio": {"positions": []}})
        return broker.handle(request)

    run.capture.inner = httpx.MockTransport(handle)
    report = run.run([1, 4, 5])
    assert len(report["opened"]) == 3
    assert report["remaining_position_ids"] == sorted(report["opened"])
    assert set(report["remaining_position_ids"]) <= broker.positions.keys()
    assert all("market-close-orders" not in request.url.path for request in broker.posts)
    assert run.clock.now == 36  # 6s per open, then 6s per owned cleanup candidate


@pytest.mark.parametrize("timeout,interval", [(0, 2), (90, 0), (float("inf"), 2)])
def test_invalid_polling_bounds_refused_before_files(tmp_path, timeout, interval):
    with pytest.raises(ValueError, match="invalid polling bounds"):
        experiment.Experiment(config(), tmp_path / "run", tmp_path / "fixtures",
                              settle_timeout=timeout, poll_interval=interval)
    assert list(tmp_path.iterdir()) == []


def test_non_404_close_lookup_error_is_not_retried(tmp_path):
    run, broker = make_run(tmp_path)
    lookups = []

    def handle(request):
        if request.method == "GET" and "close-orders/" in request.url.path:
            lookups.append(request)
            return httpx.Response(400, json={"message": "invalid lookup"})
        return broker.handle(request)

    run.capture.inner = httpx.MockTransport(handle)
    report = run.run([1, 2])
    assert report["steps"]["2"]["status"] == "fail"
    assert len(lookups) == 1
    assert len(broker.posts) == 2
    assert report["remaining_position_ids"] == []
    result = json.loads((run.fixture_dir / "step_2.json").read_text())["result"]
    assert result["status_code"] == 400
    assert "invalid lookup" in result["error_message"]


def test_cleanup_does_not_call_a_temporary_empty_snapshot_closed(tmp_path):
    run, broker = make_run(tmp_path, close_error="timeout")
    empty_reads = 0

    def handle(request):
        nonlocal empty_reads
        if request.url.path.endswith("portfolio") and run.full_attempted:
            empty_reads += 1
            if empty_reads <= 2:
                return httpx.Response(200, json={"clientPortfolio": {"positions": []}})
        return broker.handle(request)

    run.capture.inner = httpx.MockTransport(handle)
    report = run.run([1])
    assert report["remaining_position_ids"] == report["opened"]
    assert empty_reads == 4
    assert len(broker.posts) == 2


def test_error_message_sanitizes_embedded_response_fields(tmp_path):
    run, broker = make_run(tmp_path, close_error="server", reference_missing=True)
    report = run.run([1])
    assert report["steps"]["1"]["status"] == "fail"
    result = json.loads((run.fixture_dir / "step_1.json").read_text())["result"]
    assert "503 server error" in result["error_message"]
    assert "fake-response-token" not in result["error_message"]
    assert experiment.PLACEHOLDER in result["error_message"]
    assert "fake-response-token" not in (run.fixture_dir / "summary.json").read_text()
    clean = experiment.error_details(RuntimeError('Authorization: Bearer fake-value; apiKey="fake-key"'))
    assert "fake-value" not in clean["error_message"] and "fake-key" not in clean["error_message"]
    escaped = experiment.error_details(RuntimeError(json.dumps({"accessToken": 'fake-"quoted"-value'})))
    assert "quoted" not in escaped["error_message"] and "value" not in escaped["error_message"]


def test_fixture_destination_uses_unique_run_timestamp(tmp_path, monkeypatch):
    destinations = []

    class FakeExperiment:
        def __init__(self, config, run_dir, fixture_dir):
            destinations.append((run_dir, fixture_dir))
            self.report_path = run_dir / "report.json"

        def run(self, steps):
            return {"steps": {str(n): {"status": "pass"} for n in steps},
                    "remaining_position_ids": [], "unresolved_open_requests": []}

    monkeypatch.setattr(experiment, "ROOT", tmp_path)
    monkeypatch.setattr(experiment, "load_config", lambda **kwargs: config())
    monkeypatch.setattr(experiment, "Experiment", FakeExperiment)
    assert experiment.main(["--steps", "1"]) == 0
    assert experiment.main(["--steps", "1"]) == 0
    assert destinations[0][1] != destinations[1][1]
    for run_dir, fixture_dir in destinations:
        assert run_dir.name == fixture_dir.name
        assert fixture_dir.name.startswith("close_experiment_" + experiment.datetime.now(experiment.timezone.utc).strftime("%Y-%m-%d"))


def test_real_environment_refused_before_any_effect(tmp_path):
    def forbidden(request):
        pytest.fail("real environment attempted transport")
    with pytest.raises(ValueError, match="must be demo"):
        experiment.Experiment(config(EtoroEnvironment.REAL), tmp_path / "run", tmp_path / "fixtures",
                              transport=httpx.MockTransport(forbidden))
    assert list(tmp_path.iterdir()) == []


def test_all_steps_persist_before_post_and_cleanup_only_owned(tmp_path, monkeypatch):
    syncs = []
    original = experiment.os.fsync
    monkeypatch.setattr(experiment.os, "fsync", lambda fd: (syncs.append(fd), original(fd))[-1])
    run, broker = make_run(tmp_path)
    report = run.run([1, 2, 3, 4, 5])
    assert [s["status"] for s in report["steps"].values()] == ["pass"] * 5
    assert report["remaining_position_ids"] == []
    assert report["unresolved_open_requests"] == []
    assert set(broker.positions) == {"900"}
    assert len(broker.posts) == 7  # three buys, three experiment closes, partial remainder cleanup
    assert len(report["requests"]) == len(broker.posts)
    assert len(syncs) >= len(broker.posts)
    partial_body = json.loads(broker.posts[3].content)
    assert partial_body["UnitsToDeduct"] == 2.5
    fixture = json.loads((run.fixture_dir / "step_2.json").read_text())
    assert fixture["result"]["cash_change"] == 50
    assert fixture["result"]["position_gone"] is True
    full = json.loads((run.fixture_dir / "step_1.json").read_text())
    assert full["result"]["order_id"] == fixture["result"]["lookup"]["order_id"]
    for path in run.fixture_dir.glob("*.json"):
        content = path.read_text()
        assert "fake-demo" not in content and "fake-public" not in content
        assert "fake-response-token" not in content and "private-account" not in content
        assert "accessToken" not in content and '"token"' not in content
    assert len(list(run.fixture_dir.glob("*.json"))) == 6


def test_lost_response_completed_once_and_recovery_gets_only(tmp_path):
    run, broker = make_run(tmp_path, reference_missing=True)
    report = run.run([5])
    assert report["steps"]["5"]["status"] == "pass"
    closes = [r for r in broker.posts if "market-close-orders" in r.url.path]
    assert len(closes) == 1
    index = broker.calls.index(closes[0])
    assert all(r.method == "GET" for r in broker.calls[index + 1:])
    fixture = json.loads((run.fixture_dir / "step_5.json").read_text())
    assert fixture["result"]["close_post_count"] == 1
    assert fixture["result"]["response_discarded"] is True
    assert fixture["result"]["probe"]["status_code"] == 404
    assert any(c.get("response_discarded") for c in fixture["calls"])


@pytest.mark.parametrize("failure", ["timeout", "server"])
def test_failed_full_close_never_resent_even_by_cleanup(tmp_path, failure):
    run, broker = make_run(tmp_path, close_error=failure)
    report = run.run([1, 2, 3])
    assert report["steps"]["1"]["status"] == "fail"
    assert len([r for r in broker.posts if "market-close-orders" in r.url.path]) == 1
    assert report["remaining_position_ids"] == report["opened"]
    assert "900" not in report["remaining_position_ids"]


def test_uncertain_partial_close_not_closed_again(tmp_path):
    run, broker = make_run(tmp_path, partial_wrong=True)
    report = run.run([4])
    assert report["steps"]["4"]["status"] == "fail"
    assert len(broker.posts) == 2
    assert report["remaining_position_ids"] == report["opened"]


@pytest.mark.parametrize("steps", [[5], [1, 2]])
@pytest.mark.parametrize("payload", [
    {"clientPortfolio": {"credit": 10000, "orders": [], "ordersForOpen": []}},
    {"clientPortfolio": {"positions": None}},
    {"clientPortfolio": []},
])
def test_invalid_portfolio_after_close_is_unresolved_and_keeps_leftovers(tmp_path, steps, payload):
    run_dir, fixture_dir = tmp_path / "run", tmp_path / "fixtures"
    broker = Broker(run_dir / "report.json")
    close_acknowledged = False

    def handle(request):
        nonlocal close_acknowledged
        if request.url.path.endswith("portfolio") and close_acknowledged:
            return httpx.Response(200, json=payload)
        positions_before = dict(broker.positions)
        response = broker.handle(request)
        if request.method == "POST" and "market-close-orders" in request.url.path:
            # An acknowledgement is not proof of a fill: keep the owned position open.
            broker.positions = positions_before
            close_acknowledged = True
        return response

    run = experiment.Experiment(config(), run_dir, fixture_dir, transport=httpx.MockTransport(handle))
    report = run.run(steps)
    assert close_acknowledged
    assert report["opened"] and set(report["opened"]) <= broker.positions.keys()
    assert report["steps"][str(steps[-1])]["status"] == "fail"
    assert report["remaining_position_ids"] == report["opened"]
    assert report["cleanup"]["error_type"] == "EtoroValidationError"
    persisted = json.loads(run.report_path.read_text())
    assert persisted["remaining_position_ids"] == report["opened"]
    fixture = json.loads((fixture_dir / f"step_{steps[-1]}.json").read_text())
    assert fixture["status"] == "fail"
    assert len([r for r in broker.posts if "market-close-orders" in r.url.path]) == 1


def test_reference_probe_rejects_wrong_position(tmp_path):
    run, _ = make_run(tmp_path, wrong_reference=True)
    report = run.run([1, 3])
    assert report["steps"]["3"]["status"] == "fail"


def test_sanitizer_nested_headers_ids_and_numbers():
    sanitizer = experiment.Sanitizer()
    source = {"headers": {"Authorization": "Bearer fake", "x-api-key": "fake", "X-User-Key": "fake"},
              "orderId": 123, "positionID": 456, "referenceId": "fake-rid", "accountId": 789,
              "rows": [{"token": "fake", "accessToken": "fake", "amount": 12.3456789,
                        "nested": {"ToKeN": "fake"}, "positionId": "456", "gcid": "789"}],
              "path": "/positions/456", "params": {"referenceId": "fake-rid"}}
    clean = sanitizer.sanitize(source)
    assert clean["headers"] == {}
    assert clean["rows"][0]["nested"] == {}
    assert clean["rows"][0]["amount"] == 12.3456789
    assert clean["positionID"] == clean["rows"][0]["positionId"] == "POSITION_1"
    assert clean["path"] == "/positions/POSITION_1"
    assert clean["referenceId"] == clean["params"]["referenceId"] == "REFERENCE_1"
    assert clean["accountId"] == clean["rows"][0]["gcid"] == "ACCOUNT"
    assert source["rows"][0]["token"] == "fake"  # input not mutated


@pytest.mark.parametrize("key", ["refreshToken", "secret", "apiKey", "authInfo"])
def test_sanitizer_refuses_unhandled_sensitive_keys(key):
    with pytest.raises(ValueError, match="fixture refused"):
        experiment.Sanitizer().sanitize({"nested": [{key: "unsafe"}]})
    assert experiment.Sanitizer().sanitize({key: experiment.PLACEHOLDER}) == {key: experiment.PLACEHOLDER}


def test_transport_refuses_unpersisted_post_and_foreign_position(tmp_path):
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({"requests": []}))
    transport = experiment.CaptureTransport(httpx.MockTransport(lambda r: pytest.fail("must not send")), report_path)
    request = httpx.Request("POST", "https://fake.invalid/api/v1/trading/execution/demo/market-close-orders/positions/900",
                            headers={"x-request-id": "fake-rid"}, json={})
    with pytest.raises(RuntimeError, match="not persisted"):
        transport.handle_request(request)
    report_path.write_text(json.dumps({"requests": [{"request_id": "fake-rid"}]}))
    with pytest.raises(RuntimeError, match="foreign position or resend"):
        transport.handle_request(request)


def test_existing_run_not_overwritten(tmp_path):
    run, _ = make_run(tmp_path)
    original = run.report_path.read_bytes()
    with pytest.raises(FileExistsError):
        experiment.Experiment(config(), run.run_dir, run.fixture_dir)
    assert run.report_path.read_bytes() == original
    run.client.close()


def test_dry_plan_no_config_credentials_client_or_network(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("dry-plan must not construct client, read config, or use transport")
    monkeypatch.setattr(experiment, "EtoroClient", forbidden)
    monkeypatch.setattr(experiment, "load_config", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    assert experiment.main(["--steps", "1,2,3,4,5", "--dry-plan"]) == 0
    text = capsys.readouterr().out
    assert "no network calls" in text and "discard response" in text and "GET close-orders" in text


def test_dry_plan_explicit_real_config_refused(monkeypatch):
    monkeypatch.setattr(experiment, "load_config", lambda **kwargs: config(EtoroEnvironment.REAL))
    with pytest.raises(SystemExit) as exc:
        experiment.main(["--config", "unused.toml", "--dry-plan"])
    assert exc.value.code == 2


@pytest.mark.parametrize("steps", ["1,1", "5,1", "2", "3", "6", "x", ""])
def test_invalid_steps_refused_without_config_or_network(steps, monkeypatch):
    monkeypatch.setattr(experiment, "load_config", lambda **kwargs: pytest.fail("unexpected config read"))
    with pytest.raises(SystemExit) as exc:
        experiment.main(["--steps", steps, "--dry-plan"])
    assert exc.value.code == 2
