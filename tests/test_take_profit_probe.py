from __future__ import annotations

import sys
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "android-probe/app/src/main/python"))

from probe.take_profit_probe import (  # noqa: E402
    CONFIRMATION,
    _redact,
    consume_approval_token,
    evaluate_position_readback,
    require_filled_position_id,
    safety_gate_failures,
    run,
)


@pytest.fixture
def scratch_path(request):
    # The managed Windows test sandbox blocks writes in dynamically named temp
    # directories. Use one stable, ignored workspace folder per test instead.
    directory = Path(__file__).parents[1] / ".tp_probe_test_data" / request.node.name
    directory.mkdir(parents=True, exist_ok=True)
    try:
        yield directory
    finally:
        for child in sorted(directory.rglob("*"), key=lambda path: len(path.parts), reverse=True):
            if child.is_file() or child.is_symlink():
                child.unlink()
            else:
                child.rmdir()
        directory.rmdir()
        directory.parent.rmdir()


def test_real_write_gate_requires_every_exact_signal():
    good = ("real", "real:write", "real", CONFIRMATION)
    assert safety_gate_failures(*good) == ()
    assert safety_gate_failures("demo", *good[1:])
    assert safety_gate_failures(good[0], "real:read", *good[2:])
    assert safety_gate_failures(*good[:2], None, good[3])
    assert safety_gate_failures(*good[:3], CONFIRMATION.lower())
    assert safety_gate_failures(*good, "https://attacker.invalid")
    assert _redact({"token": "secret-value", "nested": {"x-api-key": "api-secret", "id": 9}}) == {
        "token": "[REDACTED]",
        "nested": {"x-api-key": "[REDACTED]", "id": 9},
    }


def test_order_must_be_filled_and_name_a_position():
    from nightwatch.etoro.models import OrderStatus

    assert require_filled_position_id(OrderStatus.FILLED, "123") == "123"
    for status, position_id in ((OrderStatus.PLACED, "123"), (OrderStatus.FILLED, None)):
        with pytest.raises(ValueError):
            require_filled_position_id(status, position_id)


def test_private_approval_token_is_consumed_once(scratch_path: Path):
    token_file = scratch_path / "approval.token"
    token_file.write_text("one-use-random-token\n", encoding="utf-8")
    consume_approval_token(token_file, "one-use-random-token")
    assert not token_file.exists()
    with pytest.raises(ValueError):
        consume_approval_token(token_file, "one-use-random-token")


def test_wrong_private_token_cannot_authorize_or_log_token(scratch_path: Path):
    token_file = scratch_path / "approval.token"
    token_file.write_text("never-log-this-token", encoding="utf-8")
    with pytest.raises(ValueError) as error:
        consume_approval_token(token_file, "wrong")
    assert "never-log-this-token" not in str(error.value)
    assert token_file.read_text(encoding="utf-8") == "never-log-this-token"


@pytest.mark.parametrize(
    ("status_id", "status_label"),
    [(3, "Filled"), (9, "CanceledPartiallyFilled"), (10, "RejectedPartiallyFilled")],
)
def test_run_verifies_full_and_terminal_partial_fills_then_blocks_replay(
    scratch_path: Path, monkeypatch, capsys, status_id: int, status_label: str
):
    from nightwatch.etoro import client as client_module
    from nightwatch.etoro.models import OrderStatus
    status = OrderStatus(status_id)

    config_dir = scratch_path / "device"
    config_dir.joinpath("config").mkdir(parents=True, exist_ok=True)
    config_dir.joinpath("config", "config.toml").write_text(
        '[trading]\nmode="dry_run"\n[etoro]\nenvironment="real"\n', encoding="utf-8"
    )
    config_dir.joinpath("config", ".secrets.env").write_text(
        "ETORO_PUBLIC_KEY=test-public\nETORO_PRIVATE_KEY=test-write\n", encoding="utf-8"
    )
    output_dir = scratch_path / "output"
    token_file = output_dir / "approval.token"
    output_dir.mkdir(exist_ok=True)
    for name in (
        "authorization_gate.json", "eligibility_raw.json", "order_lookup_001.json",
        "order_submit_raw.json", "portfolio_before.json", "portfolio_after.json",
        "probe_report.json", "submit_attempt.json", "take_profit_verdict.json",
    ):
        artifact = output_dir / name
        if artifact.exists():
            artifact.unlink()
    token_file.write_text("one-use-random-token", encoding="utf-8")
    position_raw = {
        "positionID": "position-1",
        "instrumentID": 1084,
        "units": 0.1,
        "amount": 10.0,
        "openRate": 100.0,
        "leverage": 1,
        "isBuy": True,
        "takeProfitRate": 1100.0,
        "isNoTakeProfit": False,
    }

    class FakeEtoroClient:
        writes = []

        def __init__(self, config, *, role, max_attempts):
            assert role == "write" and max_attempts == 1
            self.portfolio_reads = 0
            self.last_write_response_body = {"orderId": "order-1"}

        def probe_environment(self):
            assert not token_file.exists()
            return "real"

        def _fetch_portfolio_envelope(self):
            self.portfolio_reads += 1
            return {"positions": [] if self.portfolio_reads == 1 else [position_raw]}

        @staticmethod
        def _parse_position(raw):
            return SimpleNamespace(position_id=str(raw["positionID"]), raw=raw)

        @staticmethod
        def search_instrument(symbol):
            return SimpleNamespace(instrument_id=1084, raw={"internalSymbolFull": symbol})

        @staticmethod
        def check_eligibility_for_order(instrument_id, amount):
            assert instrument_id == 1084 and amount == 10.0
            return SimpleNamespace(
                allow_open_position=True,
                min_position_exposure=10.0,
                min_position_amount=10.0,
                raw={"allowOpenPosition": True},
            )

        def place_buy_order(self, request_id, instrument_id, amount):
            marker = json.loads((output_dir / "submit_attempt.json").read_text(encoding="utf-8"))
            assert marker["request_id"] == request_id
            assert instrument_id == 1084 and amount == 10.0
            self.writes.append(request_id)
            return "order-1"

        @staticmethod
        def lookup_order(*, order_id=None, reference_id=None):
            assert order_id == "order-1" and reference_id is None
            return SimpleNamespace(
                status=status,
                status_label=status_label,
                order_id="order-1",
                position_id="position-1",
                is_terminal=True,
                raw={"status": {"id": status_id, "name": status_label}},
            )

        @staticmethod
        def close():
            pass

    monkeypatch.setattr(client_module, "EtoroClient", FakeEtoroClient)
    monkeypatch.setenv("NIGHTWATCH_HOME", str(config_dir))
    first = run(str(config_dir), str(output_dir), CONFIRMATION, "one-use-random-token")
    report = json.loads((output_dir / "probe_report.json").read_text(encoding="utf-8"))
    second = run(str(config_dir), str(output_dir), CONFIRMATION, "one-use-random-token")
    capsys.readouterr()
    assert "VERIFIED" in first, first
    assert "REFUSED/INCOMPLETE" in second
    assert len(FakeEtoroClient.writes) == 1
    assert report["position_id"] == "position-1"
    assert report["verdict"] == "DEFAULT_11X_TAKE_PROFIT"


def test_tp_readback_accepts_only_explicit_no_tp_or_11x():
    common = {"positionID": "123", "instrumentID": 1084, "isBuy": True, "leverage": 1, "openRate": 100.0}
    no_tp = evaluate_position_readback(
        {**common, "isNoTakeProfit": True, "takeProfitRate": 0.0},
        expected_position_id="123",
        expected_instrument_id=1084,
    )
    assert no_tp["verdict"] == "NO_TAKE_PROFIT"
    assert no_tp["take_profit_rate_over_open_rate"] == 0.0

    default_tp = evaluate_position_readback(
        {**common, "isNoTakeProfit": False, "takeProfitRate": 1100.05},
        expected_position_id="123",
        expected_instrument_id=1084,
    )
    assert default_tp["verdict"] == "DEFAULT_11X_TAKE_PROFIT"
    assert default_tp["take_profit_rate_over_open_rate"] == pytest.approx(11.0005)

    for position, expected_id, expected_instrument in (
        ({**common, "isNoTakeProfit": False, "takeProfitRate": 1000.0}, "123", 1084),
        ({**common, "isNoTakeProfit": "false", "takeProfitRate": 1100.0}, "123", 1084),
        ({**common, "isNoTakeProfit": False, "takeProfitRate": 1100.0}, "other", 1084),
        ({**common, "isNoTakeProfit": False, "takeProfitRate": 1100.0}, "123", 7),
    ):
        with pytest.raises(ValueError):
            evaluate_position_readback(
                position,
                expected_position_id=expected_id,
                expected_instrument_id=expected_instrument,
            )
