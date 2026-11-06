"""T050: retry and capacity deadlines use injected monotonic time, never HTTP."""

import httpx
import pytest

from nightwatch.engine.preflight import NetworkRetry, gate_3_network
from nightwatch.etoro.client import EtoroClient
from nightwatch.etoro.errors import (
    EtoroNetworkError, EtoroRateLimitError, EtoroServerError, EtoroTimeoutError,
)
from nightwatch.etoro.ratelimit import RateLimiter
from nightwatch.market.source import (
    TiingoClient, TiingoNetworkError, TiingoRateLimitError, TiingoServerError,
    TiingoTimeoutError,
)
from .test_etoro_client import _config as etoro_config
from .test_market_source import _config as tiingo_config


class Timer:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []
        self.oversleep = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append((self.now, seconds))
        self.now += seconds + self.oversleep


def _reader(kind):
    timer = Timer()
    attempts = []
    behavior = {"failure": "server", "elapsed": 0.0, "retry_after": "600"}

    def handler(req):
        attempts.append((timer.now, req.method))
        timer.now += behavior["elapsed"]
        if behavior["failure"] == "timeout":
            raise httpx.ReadTimeout("offline timeout")
        if behavior["failure"] == "network":
            raise httpx.ConnectError("offline connection")
        if behavior["failure"] == "rate":
            headers = {} if behavior["retry_after"] is None else {"Retry-After": behavior["retry_after"]}
            return httpx.Response(429, headers=headers)
        return httpx.Response(503)

    http = httpx.Client(base_url="https://offline.test", transport=httpx.MockTransport(handler))
    if kind == "etoro":
        client = EtoroClient(etoro_config(), client=http, clock=timer.clock, sleep=timer.sleep,
                             rate_limiter=RateLimiter(clock=timer.clock))
        call = lambda: client._request("GET", "/synthetic", request_class="read")
        errors = {"timeout": EtoroTimeoutError, "network": EtoroNetworkError,
                  "rate": EtoroRateLimitError, "server": EtoroServerError}
    else:
        client = TiingoClient(tiingo_config(), client=http, clock=timer.clock, sleep=timer.sleep)
        call = lambda: client._request("/synthetic", None)
        errors = {"timeout": TiingoTimeoutError, "network": TiingoNetworkError,
                  "rate": TiingoRateLimitError, "server": TiingoServerError}
    yield client, call, timer, attempts, behavior, errors
    http.close()


@pytest.fixture(params=["etoro", "tiingo"])
def reader(request):
    yield from _reader(request.param)


@pytest.fixture
def etoro_reader():
    yield from _reader("etoro")


@pytest.mark.parametrize("failure", ["server", "network", "timeout", "rate"])
def test_read_deadline_refuses_retry_sleep(reader, failure):
    client, call, timer, attempts, behavior, errors = reader
    behavior["failure"] = failure
    # A response can consume most of the budget before its backoff is chosen.
    behavior["elapsed"] = 4.5
    client.set_deadline_in(5)
    with pytest.raises(errors["timeout"], match="^deadline$"):
        call()
    assert attempts == [(100.0, "GET")]
    assert timer.sleeps == []


def test_read_deadline_refuses_backoff_longer_than_five_seconds(reader, monkeypatch):
    client, call, timer, attempts, behavior, errors = reader
    monkeypatch.setattr(client, "_backoff", lambda attempt: 10.0)
    client.set_deadline_in(5)
    with pytest.raises(errors["timeout"], match="^deadline$"):
        call()
    assert attempts == [(100.0, "GET")] and timer.sleeps == []


def test_retry_after_none_still_checks_deadline(reader):
    client, call, timer, attempts, behavior, errors = reader
    behavior["failure"] = "rate"
    behavior["retry_after"] = None
    client.set_deadline_in(0.5)
    with pytest.raises(errors["timeout"], match="^deadline$"):
        call()
    assert attempts == [(100.0, "GET")] and timer.sleeps == []


@pytest.mark.parametrize("remaining", [-1, 0])
def test_expired_deadline_never_calls_transport(reader, remaining):
    client, call, timer, attempts, behavior, errors = reader
    client.set_deadline_in(remaining)
    with pytest.raises(errors["timeout"], match="^deadline$"):
        call()
    assert attempts == [] and timer.sleeps == []


def test_deadline_blocks_next_attempt_after_transport_crosses_cutoff(reader):
    client, call, timer, attempts, behavior, errors = reader
    behavior["elapsed"] = 6
    client.set_deadline_in(5)
    with pytest.raises(errors["timeout"], match="^deadline$"):
        call()
    assert attempts == [(100.0, "GET")] and timer.sleeps == []


def test_deadline_checks_attempt_after_oversleep(reader):
    client, call, timer, attempts, behavior, errors = reader
    timer.oversleep = 10
    client.set_deadline_in(5)
    with pytest.raises(errors["timeout"], match="^deadline$"):
        call()
    assert attempts == [(100.0, "GET")]
    assert timer.sleeps == [(100.0, 1.0)]


def test_deadline_allows_sleep_ending_at_cutoff_but_no_next_attempt(reader):
    client, call, timer, attempts, behavior, errors = reader
    client.set_deadline_in(1)
    with pytest.raises(errors["timeout"], match="^deadline$"):
        call()
    assert attempts == [(100.0, "GET")] and timer.sleeps == [(100.0, 1.0)]


@pytest.mark.parametrize("failure", ["server", "network", "timeout", "rate"])
@pytest.mark.parametrize("cleared", [False, True])
def test_no_deadline_or_cleared_deadline_keeps_retry_policy(reader, failure, cleared):
    client, call, timer, attempts, behavior, errors = reader
    behavior["failure"] = failure
    behavior["retry_after"] = None
    if cleared:
        client.set_deadline_in(-1)
        client.set_deadline_in(None)
    with pytest.raises(errors[failure]):
        call()
    waits = [1, 1, 1, 1] if failure == "rate" and isinstance(client, EtoroClient) else [1, 2, 4, 8]
    assert [seconds for _, seconds in timer.sleeps] == waits
    assert len(attempts) == 5


def test_tiingo_retry_after_deadline(reader):
    client, call, timer, attempts, behavior, errors = reader
    behavior["failure"] = "rate"
    client.set_deadline_in(60)
    with pytest.raises(errors["timeout"], match="^deadline$"):
        call()
    assert attempts == [(100.0, "GET")] and timer.sleeps == []


@pytest.mark.parametrize("deadline", [None, 5, -1])
@pytest.mark.parametrize("failure", ["server", "network", "timeout", "rate"])
@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE"])
def test_write_deadline_never_adds_retry(etoro_reader, deadline, failure, method):
    client, _, timer, attempts, behavior, errors = etoro_reader
    behavior["failure"] = failure
    client.set_deadline_in(deadline)
    error = errors["timeout"] if deadline == -1 else errors[failure]
    with pytest.raises(error):
        client._request(method, "/synthetic", request_class="write")
    assert len(attempts) == (0 if deadline == -1 else 1)
    assert timer.sleeps == []


@pytest.mark.parametrize("remaining,oversleep,expected", [(4, 0, []), (7, 0, [(100, 5)]),
                                                        (5, 10, [(100, 5)])])
def test_capacity_deadline_chunks(etoro_reader, remaining, oversleep, expected):
    client, _, timer, attempts, behavior, errors = etoro_reader
    client._limiter = RateLimiter({"read": (1, 60)}, clock=timer.clock)
    assert client._limiter.try_acquire("read")
    timer.oversleep = oversleep
    client.set_deadline_in(remaining)
    with pytest.raises(EtoroTimeoutError, match="^deadline$"):
        client._wait_for_capacity("read")
    assert timer.sleeps == expected and attempts == []
    assert all(start < 100 + remaining and start + seconds <= 100 + remaining
               for start, seconds in timer.sleeps)


def test_capacity_safety_valve_without_deadline(etoro_reader):
    client, _, timer, attempts, behavior, errors = etoro_reader
    client._limiter = RateLimiter({"read": (1, 1000)}, clock=timer.clock)
    assert client._limiter.try_acquire("read")
    client._wait_for_capacity("read")
    assert timer.now == 220
    assert timer.sleeps == [(100 + i * 5, 5) for i in range(24)]
    assert attempts == []


@pytest.mark.parametrize("remaining,elapsed,oversleep,expected_attempts,expected_sleeps", [
    (-1, 0, 0, [], []), (0, 0, 0, [], []), (4, 0, 0, [100], []),
    (6, 0, 0, [100, 105], [(100, 5)]), (5, 0, 0, [100], [(100, 5)]),
    (5, 6, 0, [100], []), (6, 0, 10, [100], [(100, 5)]),
])
def test_preflight_network_deadline(remaining, elapsed, oversleep, expected_attempts, expected_sleeps):
    timer = Timer()
    timer.oversleep = oversleep
    retry = NetworkRetry(clock=timer.clock)
    retry.set_deadline_in(remaining)
    attempts = []

    def probe():
        attempts.append(timer.now)
        timer.now += elapsed
        raise httpx.ConnectError("offline")

    with pytest.raises(httpx.TimeoutException, match="^deadline$"):
        gate_3_network(probe=probe, clock=timer.clock, sleep=timer.sleep, network_retry=retry)
    assert attempts == expected_attempts and timer.sleeps == expected_sleeps


def test_preflight_cleared_deadline_keeps_elapsed_budget():
    timer = Timer()
    retry = NetworkRetry(clock=timer.clock)
    retry.set_deadline_in(-1)
    retry.set_deadline_in(None)
    attempts = []

    def probe():
        attempts.append(timer.now)
        raise httpx.ConnectError("offline")

    result = gate_3_network(probe=probe, clock=timer.clock, sleep=timer.sleep, network_retry=retry)
    assert not result.passed and result.details == {"attempts": 19, "elapsed_seconds": 90}
    assert len(attempts) == 19 and len(timer.sleeps) == 18
