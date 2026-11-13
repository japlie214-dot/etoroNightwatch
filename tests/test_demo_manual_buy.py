"""T079: stub broker only; prove gating, unmanaged buys and redacted output."""

import importlib
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from nightwatch.config import EtoroConfig, EtoroEnvironment
from nightwatch.etoro.errors import EtoroError
from nightwatch.host import get_host

ROOT = Path(__file__).resolve().parents[1]
SECRET = "T079_SYNTHETIC_PRIVATE_VALUE"
BODY = "T079_SYNTHETIC_BROKER_BODY"


@pytest.fixture
def device(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "android-probe/app/src/main/python"))
    module = importlib.import_module("probe.demo_manual_buy")
    home = tmp_path / "nightwatch_demo"
    home.mkdir()
    state = SimpleNamespace(environment=EtoroEnvironment.DEMO, detected="demo",
                            clients=0, closed=0, calls=[], orders=[], fail=None, order_id="12345")

    def load(*, host):
        assert get_host() is host
        assert host.root == home
        return SimpleNamespace(etoro=EtoroConfig(state.environment, secrets={"test": SECRET}),
                               telegram=SimpleNamespace(secrets={}), tiingo=SimpleNamespace(secrets={}))

    class Client:
        def __init__(self, config, *, role, repo):
            assert role == "write"
            assert repo is None
            assert get_host().root == home
            state.clients += 1
            self.environment = config.environment.value

        def probe_environment(self):
            state.calls.append("environment")
            if state.fail == "environment":
                raise EtoroError(BODY + SECRET)
            return state.detected

        def resolve_instrument_id(self, symbol):
            assert symbol == "SCHG"
            state.calls.append("resolve")
            if state.fail == "resolve":
                raise RuntimeError(BODY + SECRET)
            return 1084

        def place_buy_order(self, request_id, instrument_id, amount_usd):
            assert instrument_id == 1084
            UUID(request_id)
            state.orders.append((request_id, amount_usd))
            if state.fail == len(state.orders):
                raise EtoroError(BODY + SECRET)
            return state.order_id

        def close(self):
            state.closed += 1

    monkeypatch.setattr(module, "load_config", load)
    monkeypatch.setattr(module, "EtoroClient", Client)
    state.module = module
    state.home = home
    state.run = lambda count=5, usd=11000: module.run(str(home), count, usd)
    return state


@pytest.mark.parametrize("count,usd", [(1, 50), (10, 15000), (5, 11000), (2, 50.25)])
def test_demo_orders_are_sequential_unique_and_redacted(device, capsys, count, usd):
    previous = get_host()
    report = device.run(count, usd)
    assert device.calls == ["environment", "resolve"]
    assert len(device.orders) == count
    assert len({rid for rid, _ in device.orders}) == count
    assert all(amount == usd for _, amount in device.orders)
    assert [f"Order {i}/{count}: accepted" in report for i in range(1, count + 1)] == [True] * count
    assert "fill not verified" in report
    assert "no local database opened" in report
    assert SECRET not in report + capsys.readouterr().out
    assert device.closed == 1
    assert get_host() is previous
    assert not list(device.home.rglob("*.db"))


@pytest.mark.parametrize("name", ["nightwatch_config", "other", "nightwatch_demo/../nightwatch_config"])
def test_wrong_resolved_home_refuses_before_config_load(device, monkeypatch, name):
    def forbidden(**kwargs):
        pytest.fail("wrong home must be refused before configuration load")

    previous = get_host()
    monkeypatch.setattr(device.module, "load_config", forbidden)
    home = device.home.parent / name
    report = device.module.run(str(home), 2, 50)
    assert "dedicated nightwatch_demo home required" in report
    assert device.clients == 0
    assert device.orders == []
    assert not (home / "data").exists()
    assert get_host() is previous


@pytest.mark.parametrize("order_id", ["SYNTH_RAW_BODY", BODY + SECRET, "123\n456", " 123", "-1",
                                     "1.0", "１２３", "", "1" * 21, None, {"orderId": BODY}])
def test_foreign_order_id_text_is_never_printed(device, capsys, order_id):
    device.order_id = order_id
    report = device.run(count=1)
    assert "accepted, order ID unavailable (non-numeric)" in report
    output = report + capsys.readouterr().out
    assert "SYNTH_RAW_BODY" not in output
    assert BODY not in output
    assert SECRET not in output


@pytest.mark.parametrize("order_id", ["0", "12345", "1" * 20])
def test_only_bounded_ascii_integer_order_ids_are_printed(device, order_id):
    device.order_id = order_id
    assert f"accepted, order ID {order_id};" in device.run(count=1)


def test_real_refused_before_client_or_order(device):
    device.environment = EtoroEnvironment.REAL
    assert "configuration must select demo, never real" in device.run()
    assert device.clients == 0
    assert device.orders == []


@pytest.mark.parametrize("detected", ["real", None])
def test_mismatched_or_unknown_environment_refused_before_resolution(device, detected):
    device.detected = detected
    assert "not positively confirmed as demo" in device.run()
    assert device.calls == ["environment"]
    assert device.orders == []
    assert device.closed == 1


@pytest.mark.parametrize("count", [0, 11, -1, True, 1.5, "2", None])
def test_invalid_count_never_constructs_client(device, count):
    assert "count must be an integer from 1 to 10" in device.run(count=count)
    assert device.clients == 0


@pytest.mark.parametrize("usd", [49.99, 15000.01, -1, 0, float("nan"), float("inf"),
                                  float("-inf"), True, "50", None])
def test_invalid_amount_never_constructs_client(device, usd):
    assert "USD each must be finite and from 50 to 15000" in device.run(usd=usd)
    assert device.clients == 0


@pytest.mark.parametrize("failure", [1, 3, 5])
def test_stops_on_first_error_without_retry_and_preserves_prior_results(device, capsys, failure):
    device.fail = failure
    report = device.run()
    assert len(device.orders) == failure
    assert report.count(": accepted") == failure - 1
    assert f"order {failure}/5 submission (EtoroError)" in report
    assert "an order may have been accepted" in report
    assert BODY not in report + capsys.readouterr().out
    assert SECRET not in report
    assert device.closed == 1


@pytest.mark.parametrize("failure", ["environment", "resolve"])
def test_boundary_failures_do_not_print_foreign_bodies(device, capsys, failure):
    device.fail = failure
    report = device.run()
    assert device.orders == []
    assert BODY not in report + capsys.readouterr().out
    assert SECRET not in report
    assert device.closed == 1


@pytest.mark.parametrize("failure", [None, 2, "resolve", "environment", "real", "unknown", "caps"])
def test_no_database_is_opened_on_success_or_failure(device, monkeypatch, failure):
    import sqlite3
    from nightwatch.db.repo import Repo

    def forbidden(*args, **kwargs):
        pytest.fail("manual demo buy must never open a database")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(Repo, "open", forbidden)
    device.fail = failure
    if failure == "real":
        device.environment = EtoroEnvironment.REAL
    if failure == "unknown":
        device.detected = None
    device.run(count=0 if failure == "caps" else 5)


def test_same_home_session_lock_refuses_before_client(device):
    data = device.home / "data"
    data.mkdir()
    with device.module._session_lock(data / "session.lock") as acquired:
        assert acquired
        assert "already running" in device.run()
    assert device.clients == 0


def test_explicit_host_restored_on_load_error_and_interrupt(device, monkeypatch, capsys):
    previous = get_host()

    def fail(**kwargs):
        raise RuntimeError(BODY + SECRET)

    monkeypatch.setattr(device.module, "load_config", fail)
    assert "configuration load (RuntimeError)" in device.run()
    assert BODY not in capsys.readouterr().out
    assert get_host() is previous

    def interrupt(**kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(device.module, "load_config", interrupt)
    with pytest.raises(KeyboardInterrupt):
        device.run()
    assert get_host() is previous
