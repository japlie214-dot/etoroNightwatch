from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "android-probe" / "app" / "src" / "main" / "python"))

from probe.demo_cleanup import _poll_close_order  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeClient:
    def __init__(self) -> None:
        self.last_close_lookup_responses: list[dict[str, object]] = []
        self.calls = 0

    def lookup_close_order(self, order_id: str):
        self.calls += 1
        if self.calls == 1:
            self.last_close_lookup_responses.append({"status_code": 404, "body": "not indexed"})
            raise SimpleNamespaceError("404 close order not visible", status_code=404)
        body = '{"orderID":"382354058","statusID":3,"positions":[]}'
        self.last_close_lookup_responses.append({"status_code": 200, "body": body})
        return SimpleNamespace(
            status=SimpleNamespace(name="FILLED"),
            status_label="Filled",
            is_terminal=True,
            raw={"orderID": order_id, "statusID": 3, "positions": []},
        )


class SimpleNamespaceError(Exception):
    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def test_close_order_poll_retries_only_404_and_keeps_raw_evidence() -> None:
    clock = FakeClock()
    client = FakeClient()

    result = _poll_close_order(
        client,
        "382354058",
        clock=clock,
        sleep=clock.sleep,
        poll_interval=2,
        poll_timeout=10,
    )

    assert client.calls == 2
    assert [attempt["status_code"] for attempt in result["attempts"]] == [404, 200]
    assert result["attempts"][0]["body"] == "not indexed"
    assert "404 close order not visible" in result["attempts"][0]["error"]
    assert [item["status"] for item in result["transitions"]] == ["FILLED"]
    assert result["terminal"] is True
    assert result["timed_out"] is False


def test_close_order_poll_bounds_repeated_404s() -> None:
    clock = FakeClock()

    class MissingClient:
        last_close_lookup_responses: list[dict[str, object]] = []

        def lookup_close_order(self, order_id: str):
            self.last_close_lookup_responses.append({"status_code": 404, "body": "pending"})
            raise SimpleNamespaceError("404 pending", status_code=404)

    result = _poll_close_order(
        MissingClient(),
        "382354058",
        clock=clock,
        sleep=clock.sleep,
        poll_interval=15,
        poll_timeout=4,
    )

    assert len(result["attempts"]) == 2
    assert result["timed_out"] is True
    assert result["terminal"] is False
    assert clock.now == 4
