"""HALT persistence and gate 1, using a temporary host only."""

from datetime import datetime, timedelta, timezone

from nightwatch.engine.halt import read_halt, write_halt
from nightwatch.engine.preflight import gate_1_halt


def test_write_first_cause_and_gate(host):
    assert read_halt(host) is None
    assert gate_1_halt(host).passed
    path = write_halt(host, reason="residual -60", source="session",
                      now=datetime(2026, 10, 8, 15, 30, 12, 999,
                                   tzinfo=timezone(timedelta(hours=-4))))
    assert read_halt(host) == {"reason": "residual -60", "source": "session",
                               "set_at": "2026-10-08T19:30:12+00:00"}
    before = path.read_bytes()
    assert before.endswith(b"\n")
    assert write_halt(host, reason="second", source="cli") == path
    assert path.read_bytes() == before
    assert not path.with_name(path.name + ".tmp").exists()
    assert not gate_1_halt(host).passed


def test_old_plain_text_kept(host):
    path = host.halt_flag_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("halted by nightwatch.cli halt\n", encoding="utf-8")
    assert read_halt(host) == {"reason": "halted by nightwatch.cli halt",
                               "source": "unknown", "set_at": None}
    write_halt(host, reason="new", source="session")
    assert path.read_text(encoding="utf-8") == "halted by nightwatch.cli halt\n"
