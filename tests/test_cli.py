"""CLI subcommands that are implemented for real (DESIGN §2 cli.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

import nightwatch.host as host_module
from nightwatch import cli
from nightwatch.engine.halt import read_halt
from nightwatch.host import DesktopHost
from nightwatch.db.repo import Repo, default_db_path
from .test_notify_commands import _config
from .test_notify_poll import FakeNotifier, update


@pytest.fixture(autouse=True)
def _install_temp_host(host: DesktopHost):
    """Point nightwatch.host.get_host() at the temp host for the CLI calls,
    then restore the previous installed host afterward."""
    previous = host_module.get_host()
    host_module.set_host(host)
    yield
    host_module.set_host(previous)


def test_initdb_creates_database(host: DesktopHost, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["initdb"])
    assert rc == 0
    db_path = host.data_dir() / "nightwatch.db"
    assert db_path.exists()
    out = capsys.readouterr().out
    assert "commands" in out  # table list includes the commands table


def test_halt_then_resume(host: DesktopHost) -> None:
    assert cli.main(["halt"]) == 0
    assert host.halt_flag_path().exists()
    flag = read_halt(host)
    assert flag["reason"] == "halted by nightwatch.cli halt" and flag["source"] == "cli"
    assert flag["set_at"]
    assert cli.main(["resume"]) == 0
    assert not host.halt_flag_path().exists()


def test_status_runs_without_a_database(host: DesktopHost, capsys: pytest.CaptureFixture[str]) -> None:
    # config.toml is not written by this fixture set, so status must not crash
    # even when config is missing -- only initdb/halt/resume/status may run
    # with no keys at all.
    (host.config_dir()).mkdir(parents=True, exist_ok=True)
    (host.config_dir() / "config.toml").write_text(
        '[trading]\nmode = "dry_run"\n[etoro]\nenvironment = "demo"\n', encoding="utf-8"
    )
    rc = cli.main(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "not created yet" in out


def test_status_after_initdb_shows_session_count(host: DesktopHost, capsys: pytest.CaptureFixture[str]) -> None:
    (host.config_dir()).mkdir(parents=True, exist_ok=True)
    (host.config_dir() / "config.toml").write_text(
        '[trading]\nmode = "dry_run"\n[etoro]\nenvironment = "demo"\n', encoding="utf-8"
    )
    cli.main(["initdb"])
    capsys.readouterr()
    rc = cli.main(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "sessions: 0" in out
    assert "latest session: none" in out


@pytest.mark.parametrize("command", ["seed", "webapp"])
def test_not_yet_implemented_commands_exit_non_zero(command: str) -> None:
    rc = cli.main([command])
    assert rc != 0


def test_poll_processes_commands_and_closes_notifier(host, monkeypatch, capsys):
    config = _config()
    notifier = FakeNotifier([update(1, "/halt why"), update(2, "/resume")], [])
    made = []

    def make_notifier(telegram, repo, **kwargs):
        made.append((telegram, repo, kwargs))
        return notifier

    monkeypatch.setattr(cli, "load_config", lambda **kwargs: config)
    monkeypatch.setattr(cli, "TelegramNotifier", make_notifier)
    assert cli.main(["poll"]) == 0
    assert capsys.readouterr().out == "2 command(s) processed\n"
    assert notifier.closed
    assert made[0][0] is config.telegram
    assert made[0][2] == {"environment": config.environment.value}
    assert notifier.calls[-1] == ("get_updates", 3)
    assert notifier.sent[1][0] == "Usage: /resume <reason>. HALT unchanged."
    assert read_halt(host)["reason"] == "why"
    with Repo.open(default_db_path(host), host=host) as repo:
        assert [row.command for row in repo.get_commands()] == ["/resume", "/halt why"]
