"""T062: both live-probe copies stay callable and report stalled reads offline."""

import ast
import importlib.util
import inspect
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

from nightwatch.market.window import assemble_spy_signal


ROOT = Path(__file__).resolve().parents[1]
PROBES = (
    ROOT / "android-probe/app/src/main/python/probe_live_2026_09_11.py",
    ROOT / "tools/probe_live_2026-09-11.py",
)
CALLS = (
    "resolve_instrument_id(SCHG)", "resolve_instrument_id(UPRO)",
    "_parse_candle", "get_rates", "get_eligibility", "get_instruments", "get_costs",
)


@pytest.fixture(params=PROBES, ids=("android", "tools"))
def probe(request, monkeypatch):
    # Importing the runner does not load config or make any live calls.
    spec = importlib.util.spec_from_file_location("t062_probe", request.param)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_save_json", lambda output_dir, name, obj: output_dir / name)
    return module


def test_section_f_call_binds_real_signature(probe):
    tree = ast.parse(inspect.getsource(probe.section_f_window_assembly))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "assemble_spy_signal"]
    assert len(calls) == 1
    call = calls[0]
    signature = inspect.signature(assemble_spy_signal)
    values = dict(tiingo=object(), trading_date="2026-09-11", iex_quote=None,
                  etoro_daily_candle=None, assemble_spy_signal=signature.bind)
    bound = eval(compile(ast.Expression(call), "<probe call>", "eval"), values)
    assert bound.arguments["open_lot_entry_spy_ath"] == ()
    assert bound.arguments["enforce_completeness"] is False
    # The old probe's exact omission must be rejected by the real signature.
    old_kwargs = dict(bound.arguments)
    old_kwargs.pop("tiingo")
    old_kwargs.pop("open_lot_entry_spy_ath")
    with pytest.raises(TypeError, match="open_lot_entry_spy_ath"):
        signature.bind(values["tiingo"], **old_kwargs)


@pytest.mark.parametrize("stalled", CALLS)
def test_section_c_names_each_stall_and_continues(probe, monkeypatch, capsys, tmp_path, stalled):
    release = threading.Event()
    finished = threading.Event()
    seen = []
    original_call = probe._c_call

    # Capture calls at the actual resolver/request boundaries; no network.
    def resolve(symbol):
        return probe.KNOWN_INSTRUMENT_IDS[symbol]

    def request(*args, **kwargs):
        return SimpleNamespace(json=lambda: {})

    def bounded_call(label, fn, *args, **kwargs):
        seen.append(label)
        kwargs.pop("timeout_s", None)

        def read():
            if label == stalled:
                try:
                    release.wait()
                finally:
                    finished.set()
            return fn(*args, **kwargs)

        return original_call(label, read, timeout_s=0.05)

    monkeypatch.setattr(probe, "_c_call", bounded_call)
    client = SimpleNamespace(resolve_instrument_id=resolve, _request=request, _demo=lambda: "demo/")
    try:
        assert probe.run_section("C", probe.section_c_parser_audit, client, tmp_path, timeout_s=2)
        output = capsys.readouterr().out
        assert seen == list(CALLS)
        assert f"C: {stalled} -- BEGIN" in output
        assert f"C: {stalled} -- TIMEOUT after 0.05s" in output
        assert "SECTION C -- END (ok)" in output
        assert "_parse_order_result -- CANNOT" in output
        for label in CALLS:
            if label != stalled:
                assert f"C: {label} -- END (ok)" in output
    finally:
        release.set()
        assert finished.wait(2)


def test_section_c_reports_failure_and_preserves_exception(probe, capsys):
    error = ValueError("offline failure")

    def fail():
        raise error

    with pytest.raises(ValueError) as caught:
        probe._c_call("get_costs", fail)
    assert caught.value is error
    assert "C: get_costs -- FAILED (ValueError status=unknown)" in capsys.readouterr().out
    marker = object()
    assert probe._c_call("get_rates", lambda: marker) is marker


def test_other_resolution_callers_keep_existing_behavior(probe, capsys):
    seen = []

    def resolve(symbol):
        seen.append(symbol)
        if symbol == "UPRO":
            raise ValueError("offline lookup failure")
        return 1084

    ids = probe._resolve_instrument_ids(SimpleNamespace(resolve_instrument_id=resolve), ["SCHG", "UPRO"])
    assert ids == {"SCHG": 1084, "UPRO": 3244}
    assert seen == ["SCHG", "UPRO"]
    output = capsys.readouterr().out
    assert "falling back to known id 3244" in output
    assert "C:" not in output


@pytest.mark.parametrize("status", [403, None, "Authorization: Bearer QA_SYNTHETIC_7462", True])
def test_error_formatter_only_reports_type_and_integer_status(probe, status):
    error = ValueError("Authorization: Bearer QA_SYNTHETIC_7462")
    error.status_code = status
    expected = status if type(status) is int else "unknown"
    assert probe._fmt_exc(error) == f"ValueError status={expected}"


def test_section_c_failure_logs_do_not_echo_credentials(probe, capsys, tmp_path):
    marker = "QA_SYNTHETIC_CREDENTIAL_NOT_REAL_7462"

    def fail(*args, **kwargs):
        raise ValueError(f"Authorization: Bearer {marker}")

    client = SimpleNamespace(resolve_instrument_id=fail, _request=fail, _demo=lambda: "demo/")
    assert probe.run_section("C", probe.section_c_parser_audit, client, tmp_path)
    output = capsys.readouterr().out
    assert marker not in output
    assert "Authorization" not in output
    for label in CALLS:
        assert f"C: {label} -- FAILED (ValueError status=unknown)" in output
    assert "get_costs audit FAILED: ValueError status=unknown" in output
    assert "SECTION C -- END (ok)" in output


def test_section_failure_does_not_print_exception_chain_or_notes(probe, capsys):
    marker = "QA_SYNTHETIC_CREDENTIAL_NOT_REAL_7462"

    def fail():
        try:
            raise ValueError(f"Authorization: Bearer {marker}")
        except ValueError as cause:
            error = RuntimeError(f"Authorization: Bearer {marker}")
            error.add_note(marker)
            raise error from cause

    assert not probe.run_section("F", fail)
    output = capsys.readouterr().out
    assert marker not in output
    assert "Authorization" not in output
    assert "Traceback" not in output
    assert "SECTION F -- END (EXCEPTION RuntimeError status=unknown)" in output


def test_probe_copies_stay_identical():
    assert PROBES[0].read_bytes() == PROBES[1].read_bytes()


RUNNER_ENTRIES = (
    ("probe_runner", "run_default"),
    ("probe_runner", "run_sections"),
    ("adopt_fetch", "run"),
    ("env_probe", "run"),
    ("order_latency_runner", "run"),
    ("demo_cleanup", "run"),
    ("payload_gaps", "run"),
    ("take_profit_probe", "run"),
)


@pytest.mark.parametrize("module_name,entry_name", RUNNER_ENTRIES)
@pytest.mark.parametrize("config_valid", (True, False), ids=("loaded", "invalid"))
def test_runner_binds_home_after_host_was_resolved(
    monkeypatch, tmp_path, config_path, module_name, entry_name, config_valid,
):
    import nightwatch.config as config_module
    import nightwatch.host as host_module

    # Simulate the diagnostic bridge resolving the host before any runner call.
    previous = host_module.get_host()
    monkeypatch.setenv("NIGHTWATCH_HOME", "unused-before-runner")
    assert host_module.get_host() is previous
    monkeypatch.syspath_prepend(str(ROOT / "android-probe/app/src/main/python"))
    module = importlib.import_module(f"probe.{module_name}")
    loaded = []
    active_roots = []
    real_load_config = config_module.load_config
    if not config_valid:
        config_path.write_text("[invalid", encoding="utf-8")
    # No real dotenv access, even if a broken runner resolves the wrong home.
    monkeypatch.setattr(config_module, "read_dotenv", lambda path: {})

    def load_then_stop(*args, **kwargs):
        active_roots.append(host_module.get_host().config_dir())
        config = real_load_config(*args, **kwargs)
        loaded.append(config.config_path)
        raise ValueError("T067 offline stop after config load")

    monkeypatch.setattr(config_module, "load_config", load_then_stop)
    try:
        result = getattr(module, entry_name)(str(tmp_path), str(tmp_path / "output"))
        assert isinstance(result, str)
        assert active_roots
        assert all(path == config_path.parent for path in active_roots)
        assert loaded == ([config_path] * len(active_roots) if config_valid else [])
        assert host_module.get_host() is previous
        assert os.environ["NIGHTWATCH_HOME"] == str(tmp_path)
    finally:
        host_module.set_host(previous)


@pytest.mark.parametrize("module_name,entry_name", RUNNER_ENTRIES)
def test_runner_restores_host_when_interrupted(
    monkeypatch, tmp_path, module_name, entry_name,
):
    import nightwatch.config as config_module
    import nightwatch.host as host_module

    previous = host_module.get_host()
    monkeypatch.setenv("NIGHTWATCH_HOME", "unused-before-runner")
    monkeypatch.syspath_prepend(str(ROOT / "android-probe/app/src/main/python"))
    module = importlib.import_module(f"probe.{module_name}")

    def interrupt(*args, **kwargs):
        assert host_module.get_host().config_dir() == tmp_path / "config"
        raise KeyboardInterrupt("T067 offline interruption")

    monkeypatch.setattr(config_module, "load_config", interrupt)
    try:
        with pytest.raises(KeyboardInterrupt, match="T067 offline interruption"):
            getattr(module, entry_name)(str(tmp_path), str(tmp_path / "output"))
        assert host_module.get_host() is previous
    finally:
        host_module.set_host(previous)


def test_live_runner_keeps_home_through_sections_and_fee_audit(
    monkeypatch, tmp_path, config_path,
):
    import nightwatch.config as config_module
    import nightwatch.etoro.client as client_module
    import nightwatch.host as host_module

    previous = host_module.get_host()
    monkeypatch.setenv("NIGHTWATCH_HOME", "unused-before-runner")
    monkeypatch.syspath_prepend(str(ROOT / "android-probe/app/src/main/python"))
    from probe import probe_runner

    monkeypatch.setattr(config_module, "read_dotenv", lambda path: {})
    seen = []

    def sections(argv):
        config = config_module.load_config()
        assert config.config_path == config_path
        assert argv[-1] == "A,C,D,E1,F"
        seen.append("sections")
        return 0

    class OfflineClient:
        def __init__(self, config, *, role):
            assert role == "read"
            assert config.environment.value == "demo"
            assert host_module.get_host().config_dir() == config_path.parent
            seen.append("fee audit")

        def resolve_instrument_id(self, symbol):
            return {"SCHG": 1084, "UPRO": 3244}[symbol]

        def get_trade_history(self, **kwargs):
            return []

        def close(self):
            assert host_module.get_host().config_dir() == config_path.parent
            seen.append("closed")

    monkeypatch.setitem(sys.modules, "probe_live_2026_09_11", SimpleNamespace(main=sections))
    monkeypatch.setattr(client_module, "EtoroClient", OfflineClient)
    try:
        result = probe_runner.run_default(str(tmp_path), str(tmp_path / "output"))
        assert "run_default finished" in result
        assert "E1-plus FAILED" not in result
        assert seen == ["sections", "fee audit", "closed"]
        assert host_module.get_host() is previous
    finally:
        host_module.set_host(previous)
