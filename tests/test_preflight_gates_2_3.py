"""Power verdicts and network polling with mocked HTTP and time."""

from dataclasses import asdict

import httpx
import pytest

from nightwatch.engine import preflight
from nightwatch.engine.preflight import (
    GateResult,
    etoro_reachability_probe,
    gate_2_power,
    gate_3_network,
)
from nightwatch.host import DesktopHost, PowerStatus


def test_desktop_power_passes() -> None:
    status = DesktopHost().power_status()
    assert gate_2_power(status=status, min_battery_percent=40) == GateResult(
        2, "power", True, "power ok", asdict(status),
    )


@pytest.mark.parametrize("status, reason", [
    (PowerStatus(39, False, True, True, True), "on battery at 39% (< 40%)"),
    (PowerStatus(100, False, False, True, True), "battery-optimization exemption not granted"),
    (PowerStatus(100, False, True, False, True), "exact alarms not permitted"),
    (PowerStatus(100, False, True, True, False), "wake lock not held"),
    (PowerStatus(None, False, True, True, True), "on battery at None% (< 40%)"),
    (PowerStatus(5, True, True, True, True), "power ok"),
    (PowerStatus(40, False, True, True, True), "power ok"),
    (PowerStatus(39, False, False, False, False), "on battery at 39% (< 40%)"),
    (PowerStatus(40, False, False, False, False), "battery-optimization exemption not granted"),
    (PowerStatus(40, False, True, False, False), "exact alarms not permitted"),
])
def test_power_conditions(status: PowerStatus, reason: str) -> None:
    assert gate_2_power(status=status, min_battery_percent=40) == GateResult(
        2, "power", reason == "power ok", reason, asdict(status),
    )


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.mark.parametrize("failures", [0, 2, 19])
def test_network_polling(failures: int) -> None:
    timer = FakeClock()
    attempts = 0

    def probe() -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= failures:
            raise httpx.ConnectError(f"offline attempt {attempts}")

    result = gate_3_network(
        probe=probe, timeout_seconds=90, interval_seconds=5,
        sleep=timer.sleep, clock=timer.clock,
    )
    expected_attempts = min(failures + 1, 19)
    elapsed = (expected_attempts - 1) * 5.0
    passed = failures < 19
    reason = (
        f"reachable after {expected_attempts} attempt(s)" if passed
        else "no network after 19 attempt(s) in 90s: ConnectError: offline attempt 19"
    )
    assert result == GateResult(
        3, "network", passed, reason,
        {"attempts": expected_attempts, "elapsed_seconds": elapsed},
    )
    assert attempts == expected_attempts
    assert timer.sleeps == [5.0] * (expected_attempts - 1)


@pytest.mark.parametrize("connect_error", [False, True])
@pytest.mark.parametrize("custom", [False, True])
def test_reachability_probe(monkeypatch: pytest.MonkeyPatch, connect_error: bool, custom: bool) -> None:
    calls = []

    def get(url: str, **kwargs) -> httpx.Response:
        calls.append((url, kwargs))
        if connect_error:
            raise httpx.ConnectError("offline")
        return httpx.Response(302)

    monkeypatch.setattr(preflight.httpx, "get", get)
    probe = etoro_reachability_probe("https://example.test", 2.0) if custom else etoro_reachability_probe()
    if connect_error:
        with pytest.raises(httpx.ConnectError, match="^offline$"):
            probe()
    else:
        assert probe() is None
    assert calls == [(
        "https://example.test/" if custom else "https://public-api.etoro.com/",
        {"timeout": 2.0 if custom else 10.0, "follow_redirects": False},
    )]
