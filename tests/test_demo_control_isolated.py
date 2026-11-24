"""T083 synthetic-only checks. Run directly with -I -B; never loads conftest/config files."""

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app/src/main/python"))

import httpx
from nightwatch.engine import demo_control as control
from nightwatch.etoro.client import EtoroClient
from nightwatch.etoro.errors import EtoroValidationError
from nightwatch.host import DesktopHost


TEST_ROOT = None


class DemoControlTests(unittest.TestCase):
    order_attempts = 0

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.host = DesktopHost(Path(self.temp.name) / "nightwatch_demo")
        self.now = 1_000_001
        self.reads = []
        self.response_headers = {}
        self.payload = {"clientPortfolio": {"credit": 100, "positions": [],
                                            "orders": [], "ordersForOpen": []}}
        self.network = patch.object(httpx.HTTPTransport, "handle_request",
                                    side_effect=AssertionError("real transport forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def request(self, action="pause_demo_orders"):
        return dict(requestId=str(uuid4()), action=action, deviceId="synthetic-device",
                    environment="demo", issuedAt=1_000_000, deadlineAt=1_120_000)

    def apply(self, request=None, **kwargs):
        return control.handle_demo_control(request or self.request(), host=self.host,
                                           device_id="synthetic-device", operator_write_authorized=True,
                                           clock=lambda: self.now, **kwargs)

    def client(self, role="write", environment="demo", handler=None):
        credentials = SimpleNamespace(label=environment + ":synthetic",
                                      as_headers=lambda: {"x-api-key": "synthetic", "x-user-key": "synthetic"})
        config = SimpleNamespace(environment=SimpleNamespace(value=environment),
                                 read_credentials=credentials, write_credentials=credentials,
                                 path_segment="demo/" if environment == "demo" else "",
                                 base_url="https://synthetic.invalid", timeout_seconds=1)

        def transport(request):
            if request.method != "GET":
                type(self).order_attempts += 1
                raise AssertionError("order transport reached")
            self.reads.append(request.url.path)
            return handler(request) if handler else httpx.Response(200, json=self.payload, headers=self.response_headers)

        client = EtoroClient(config, role=role, host=self.host,
                             transport=httpx.MockTransport(transport), max_attempts=1)
        self.addCleanup(client.close)
        return client

    def assert_blocked(self, client):
        with self.assertRaises(EtoroValidationError):
            client.place_buy_order(str(uuid4()), 1001, 10)
        with self.assertRaises(EtoroValidationError):
            client.close_position(str(uuid4()), "1002", 1001)
        self.assertEqual(self.order_attempts, 0)

    def test_payload_authority_cannot_authorize(self):
        request = self.request()
        request["operatorWriteAuthorized"] = True
        result = control.handle_demo_control(request, host=self.host, device_id="synthetic-device")
        self.assertEqual(result["reasonCode"], "write_authority_required")
        self.assertFalse(self.host.root.exists())

    def test_identity_action_and_timestamp_rejections(self):
        for field, value in [("environment", "real"), ("environment", None),
                             ("action", "resume"), ("action", "cancel"), ("action", []),
                             ("deviceId", "other"), ("requestId", "not-a-uuid"),
                             ("requestId", "00000000-0000-0000-0000-000000000000"),
                             ("issuedAt", True), ("issuedAt", float("nan")),
                             ("deadlineAt", 1_120_001)]:
            with self.subTest(field=field, value=value):
                request = self.request()
                request[field] = value
                self.assertEqual(self.apply(request)["status"], "refused")
        self.assertFalse(self.host.root.exists())

    def test_future_and_expiry_do_not_execute(self):
        for now, status in [(999_999, "refused"), (1_120_000, "expired_unconfirmed")]:
            self.now = now
            self.assertEqual(self.apply()["status"], status)
        self.assertFalse(self.host.root.exists())

    def test_pause_restart_replay_and_nontransmitting_admission(self):
        control.check_demo_submission(self.host)  # Before: the shared guard admits without sending.
        request = self.request()
        receipt = self.apply(request)
        self.assertEqual(receipt["status"], "applied")
        self.assertTrue(receipt["pausePersisted"] and receipt["finalSubmissionGuard"])
        self.assertEqual({key: receipt[key] for key in ("requestId", "action", "deviceId", "environment")},
                         {key: request[key] for key in ("requestId", "action", "deviceId", "environment")})
        self.assertEqual(receipt["executedAt"], self.now)
        reloaded = DesktopHost(self.host.root)
        with self.assertRaises(control.DemoSubmissionPaused):
            control.check_demo_submission(reloaded)
        self.assertEqual(self.apply(json.loads(json.dumps(request)))["reasonCode"], "duplicate")
        self.assert_blocked(self.client())
        self.assertFalse(self.host.halt_flag_path().exists())

    def test_pause_survives_new_interpreter(self):
        self.apply()
        source = str(Path(__file__).resolve().parents[1] / "app/src/main/python")
        script = ("import sys;sys.path.insert(0,sys.argv[1]);"
                  "from pathlib import Path;from nightwatch.host import DesktopHost;"
                  "from nightwatch.engine.demo_control import check_demo_submission,DemoSubmissionPaused\n"
                  "try: check_demo_submission(DesktopHost(Path(sys.argv[2])))\n"
                  "except DemoSubmissionPaused: print('persisted pause enforced')\n"
                  "else: raise AssertionError('pause lost')")
        result = subprocess.run([sys.executable, "-I", "-B", "-c", script, source, str(self.host.root)],
                                capture_output=True, text=True, timeout=10, check=True)
        self.assertEqual(result.stdout.strip(), "persisted pause enforced")

    def test_existing_halt_unchanged_and_still_blocks(self):
        self.host.root.mkdir(parents=True)
        halt = self.host.halt_flag_path()
        halt.write_text("preexisting cause", encoding="utf-8")
        self.assert_blocked(self.client())
        self.assertEqual(self.apply()["status"], "applied")
        self.assertEqual(halt.read_text(encoding="utf-8"), "preexisting cause")

    def test_pause_after_capacity_wait_blocks_both_submission_callers(self):
        client = self.client()
        with patch.object(client, "_wait_for_capacity", side_effect=lambda _: self.apply()):
            self.assert_blocked(client)

    def test_pause_waits_for_preexisting_admission(self):
        entered = threading.Event()
        done = threading.Event()
        results = []
        transaction = control._transaction

        @contextmanager
        def observed(host):
            entered.set()
            with transaction(host) as connection:
                yield connection

        def pause():
            with patch.object(control, "_transaction", observed):
                results.append(self.apply())
            done.set()

        with control.demo_submission_guard(self.host):
            thread = threading.Thread(target=pause)
            thread.start()
            self.assertTrue(entered.wait(2))
            self.assertFalse(done.wait(0.05))
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0]["status"], "applied")
        with self.assertRaises(control.DemoSubmissionPaused):
            control.check_demo_submission(self.host)

    def test_expired_while_waiting_does_not_pause(self):
        transaction = control._transaction

        @contextmanager
        def expired(host):
            with transaction(host) as connection:
                self.now = 1_120_000
                yield connection

        with patch.object(control, "_transaction", expired):
            self.assertEqual(self.apply()["status"], "expired_unconfirmed")
        control.check_demo_submission(self.host)

    def test_read_role_forbids_buy_close_and_patch(self):
        client = self.client(role="read")
        self.assert_blocked(client)
        with self.assertRaises(EtoroValidationError):
            client.clear_take_profit(str(uuid4()), "1002")
        self.assertEqual(self.reads, [])

    def test_unavailable_or_corrupt_storage_fails_closed(self):
        self.host.root.mkdir(parents=True)
        path = self.host.root / "DEMO_CONTROL.sqlite3"
        path.write_bytes(b"synthetic corrupt sqlite")
        self.assertEqual(self.apply()["status"], "unconfirmed")
        self.assert_blocked(self.client())
        self.assertEqual(path.read_bytes(), b"synthetic corrupt sqlite")

    def test_refresh_requires_read_only_demo_adapter(self):
        for broker in [None, self.client(), self.client(role="read", environment="real")]:
            self.assertEqual(self.apply(self.request("refresh_demo_broker"), broker=broker)["status"], "refused")
        self.assertEqual(self.reads, [])

    def test_refresh_is_correlated_partial_read_only_and_replay_safe(self):
        self.payload["clientPortfolio"]["positions"] = [
            {"positionID": 1002, "instrumentID": 1001, "units": 2, "openRate": 10, "amount": 20}
        ]
        client = self.client(role="read")
        request = self.request("refresh_demo_broker")
        result = self.apply(request, broker=client)
        self.assertEqual(result["requestId"], request["requestId"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["brokerObservation"],
                         dict(account=True, positions=True, openOrders=False, observedAt=None))
        self.assertEqual(self.reads, ["/api/v1/trading/info/demo/portfolio"])
        self.assertNotIn("credit", json.dumps(result))
        self.assertEqual(self.apply(request, broker=client)["reasonCode"], "duplicate")
        self.assertEqual(len(self.reads), 1)
        self.assertEqual(result["accountRead"]["sourceTimeStatus"], "missing")
        self.assertEqual(result["positionsRead"]["readCompletedAt"], self.now)
        self.assertEqual(self.order_attempts, 0)

    def test_partial_malformed_and_offline_broker_responses(self):
        for payload in [{}, {"positions": [], "credit": 100, "orders": [], "ordersForOpen": []},
                        {"clientPortfolio": {}}, {"clientPortfolio": {"positions": None}},
                        {"clientPortfolio": {"positions": [None]}},
                        {"clientPortfolio": {"positions": [{}]}},
                        {"clientPortfolio": None},
                        {"clientPortfolio": {"credit": 10, "positions": [], "orders": None, "ordersForOpen": []}}]:
            with self.subTest(payload=payload):
                self.payload = payload
                result = self.apply(self.request("refresh_demo_broker"), broker=self.client(role="read"))
                self.assertEqual(result["status"], "partial")
                self.assertEqual(result["brokerObservation"]["account"],
                                 isinstance(payload.get("clientPortfolio"), dict) and
                                 payload["clientPortfolio"].get("credit") == 10)
                self.assertFalse(result["brokerObservation"]["openOrders"])
        client = self.client(role="read", handler=lambda _: httpx.Response(503, text="synthetic-private-body"))
        result = self.apply(self.request("refresh_demo_broker"), broker=client)
        self.assertFalse(result["brokerObservation"]["account"] or result["brokerObservation"]["positions"])
        self.assertNotIn("synthetic-private-body", json.dumps(result))

    def test_refresh_crossing_deadline_never_adds_read(self):
        def late_response(_):
            self.now = 1_120_000
            return httpx.Response(200, json=self.payload)

        result = self.apply(self.request("refresh_demo_broker"),
                            broker=self.client(role="read", handler=late_response))
        self.assertEqual(result["status"], "expired_unconfirmed")
        self.assertEqual(len(self.reads), 1)
        self.assertTrue(result["brokerObservation"]["positions"])
        self.assertEqual(result["positionsRead"]["readCompletedAt"], self.now)
        self.assertIsNone(result["brokerObservation"]["observedAt"])

    def test_documented_array_states_overlap_and_allowlist(self):
        row = {"orderID": "synthetic-sensitive-id", "amount": 12345.67}
        self.payload["clientPortfolio"].update(
            orders=[row, row], ordersForOpen=[row], ordersForClose=[],
            ordersForCloseMultiple=None, stockOrders={}, entryOrders=[None])
        result = self.apply(self.request("refresh_demo_broker"), broker=self.client(role="read"))
        arrays = result["portfolioArrays"]
        self.assertEqual(arrays["orders"], dict(state="present", count=2))
        self.assertEqual(arrays["ordersForOpen"], dict(state="present", count=1))
        self.assertEqual(arrays["ordersForClose"], dict(state="empty", count=0))
        self.assertEqual(arrays["ordersForCloseMultiple"], dict(state="null", count=None))
        self.assertEqual(arrays["stockOrders"], dict(state="invalid", count=None))
        self.assertEqual(arrays["entryOrders"], dict(state="invalid", count=None))
        self.assertEqual(arrays["exitOrders"], dict(state="absent", count=None))
        self.assertEqual(result["portfolioRead"]["scope"], "clientPortfolio_top_level")
        self.assertNotIn("total", json.dumps(result).lower())
        self.assertNotIn("synthetic-sensitive-id", json.dumps(result))
        self.assertNotIn("12345.67", json.dumps(result))
        self.assertEqual(len(self.reads), 1)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["brokerObservation"]["openOrders"])

    def test_response_source_time_is_separate_and_never_complete_observation(self):
        self.now = 1_001_001
        for date, expected, source_at in [
            ("Thu, 01 Jan 1970 00:16:39 GMT", "stale", 999_000),
            ("Thu, 01 Jan 1970 00:16:41 GMT", "response_time", 1_001_000),
            ("Thu, 01 Jan 1970 00:16:42 GMT", "future", 1_002_000),
            ("synthetic-invalid-date", "invalid", None),
        ]:
            with self.subTest(date=date):
                self.response_headers = {"Date": date}
                result = self.apply(self.request("refresh_demo_broker"), broker=self.client(role="read"))
                for key in ("accountRead", "positionsRead"):
                    self.assertEqual(result[key]["sourceResponseAt"], source_at)
                    self.assertEqual(result[key]["sourceTimeStatus"], expected)
                    self.assertEqual(result[key]["readCompletedAt"], self.now)
                self.assertIsNone(result["brokerObservation"]["observedAt"])
                self.assertEqual(result["status"], "partial")
        self.assertEqual(len(self.reads), 4)

    def test_missing_date_does_not_reuse_prior_source_time(self):
        client = self.client(role="read")
        self.response_headers = {"Date": "Thu, 01 Jan 1970 00:16:39 GMT"}
        self.apply(self.request("refresh_demo_broker"), broker=client)
        self.response_headers = {}
        result = self.apply(self.request("refresh_demo_broker"), broker=client)
        self.assertEqual(result["accountRead"]["sourceTimeStatus"], "missing")
        self.assertIsNone(result["accountRead"]["sourceResponseAt"])
        self.assertEqual(len(self.reads), 2)

    def test_uuid_case_replay_cannot_execute(self):
        request = self.request()
        self.assertEqual(self.apply(request)["status"], "applied")
        request["requestId"] = request["requestId"].upper()
        self.assertEqual(self.apply(request)["reasonCode"], "duplicate")

    def test_refresh_remains_available_while_paused(self):
        self.apply()
        result = self.apply(self.request("refresh_demo_broker"), broker=self.client(role="read"))
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["brokerObservation"]["account"])
        self.assert_blocked(self.client())

    def test_bridge_defaults_refuse_without_config_or_client(self):
        from nightwatch import android_entry
        with patch.object(android_entry, "load_config", side_effect=AssertionError("config loaded")), \
             patch.object(android_entry, "EtoroClient", side_effect=AssertionError("client constructed")):
            result = json.loads(android_entry.apply_demo_control(str(self.host.root), json.dumps(self.request())))
            self.assertEqual(result["reasonCode"], "write_authority_required")
            result = json.loads(android_entry.apply_demo_control(
                str(self.host.root), json.dumps(self.request("resume")),
                device_id="synthetic-device", operator_write_authorized=True))
            self.assertEqual(result["reasonCode"], "invalid_demo_request")

    def test_bridge_rejects_real_config_and_wrong_home(self):
        from nightwatch import android_entry
        from nightwatch.config import EtoroEnvironment
        config = SimpleNamespace(environment=EtoroEnvironment.REAL,
                                 etoro=SimpleNamespace(is_real_money=True))
        with patch.object(android_entry, "load_config", return_value=config), \
             patch.object(android_entry, "EtoroClient", side_effect=AssertionError("client constructed")):
            result = json.loads(android_entry.apply_demo_control(
                str(self.host.root), json.dumps(self.request("refresh_demo_broker")),
                device_id="synthetic-device", operator_write_authorized=True))
            self.assertEqual(result["reasonCode"], "real_not_allowed")
            result = json.loads(android_entry.apply_demo_control(
                str(self.host.root.parent / "wrong_home"), json.dumps(self.request()),
                device_id="synthetic-device", operator_write_authorized=True))
            self.assertEqual(result["reasonCode"], "wrong_home")
        self.assertFalse(self.host.root.exists())

    def test_bridge_pause_and_refresh_use_injected_device_context(self):
        from nightwatch import android_entry
        from nightwatch.config import EtoroEnvironment
        config = SimpleNamespace(environment=EtoroEnvironment.DEMO,
                                 etoro=SimpleNamespace(is_real_money=False))
        client = self.client(role="read")
        with patch.object(android_entry, "load_config", return_value=config), \
             patch.object(android_entry, "EtoroClient", return_value=client) as factory, \
             patch.object(android_entry, "run_session", side_effect=AssertionError("strategy invoked")), \
             patch.object(control.time, "time_ns", return_value=self.now * 1_000_000):
            pause = json.loads(android_entry.apply_demo_control(
                str(self.host.root), json.dumps(self.request()),
                device_id="synthetic-device", operator_write_authorized=True))
            self.assertEqual(pause["status"], "applied")
            factory.assert_not_called()
            result = json.loads(android_entry.apply_demo_control(
                str(self.host.root), json.dumps(self.request("refresh_demo_broker")),
                device_id="synthetic-device", operator_write_authorized=True))
            self.assertEqual(result["status"], "partial")
            self.assertEqual(factory.call_args.kwargs["role"], "read")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scratch", required=True)
    args = parser.parse_args()
    TEST_ROOT = args.scratch
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(DemoControlTests)
    outcome = unittest.TextTestRunner(verbosity=2).run(suite)
    print(f"attempted order transport calls: {DemoControlTests.order_attempts}")
    sys.exit(0 if outcome.wasSuccessful() and DemoControlTests.order_attempts == 0 else 1)
