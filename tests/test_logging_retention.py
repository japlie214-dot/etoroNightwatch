from datetime import date, datetime, timedelta, timezone
import logging
from pathlib import Path

import pytest

from nightwatch.logging_setup import _prune_old_logs, configure_logging


TODAY = date(2026, 11, 7)


def test_prune_old_logs_keeps_boundary_recent_and_future_logs(tmp_path):
    for age in (91, 90, 89, 0, -1):
        (tmp_path / f"log_{TODAY - timedelta(days=age)}.txt").write_text("keep contents")

    _prune_old_logs(tmp_path, TODAY)

    assert {path.name for path in tmp_path.iterdir()} == {
        f"log_{TODAY - timedelta(days=age)}.txt" for age in (90, 89, 0, -1)
    }
    assert all(path.read_text() == "keep contents" for path in tmp_path.iterdir())


@pytest.mark.parametrize("name", [
    "log_2020-02-30.txt", "log_2020-1-01.txt", "log_not-a-date.txt",
    "other_2020-01-01.txt", "log_2020-01-01.txt.bak", "notes.txt",
])
def test_prune_old_logs_ignores_unrecognized_names(tmp_path, name):
    path = tmp_path / name
    path.write_text("unrelated")
    _prune_old_logs(tmp_path, TODAY)
    assert path.read_text() == "unrelated"


def test_prune_old_logs_warns_and_continues_on_permission_error(tmp_path, monkeypatch, caplog):
    blocked = tmp_path / "log_2020-01-01.txt"
    removable = tmp_path / "log_2020-01-02.txt"
    blocked.touch()
    removable.touch()
    unlink = Path.unlink

    def deny_one(path, *args, **kwargs):
        if path == blocked:
            raise PermissionError("synthetic permission denial")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_one)
    with caplog.at_level(logging.WARNING):
        _prune_old_logs(tmp_path, TODAY)
    assert blocked.exists()
    assert not removable.exists()
    assert "Could not prune daily log" in caplog.text
    assert "synthetic permission denial" in caplog.text


def test_prune_old_logs_warns_on_directory_scan_error(tmp_path, monkeypatch, caplog):
    def deny_scan(path):
        raise PermissionError("synthetic scan denial")

    monkeypatch.setattr(Path, "iterdir", deny_scan)
    with caplog.at_level(logging.WARNING):
        _prune_old_logs(tmp_path, TODAY)
    assert "Could not scan daily logs for retention" in caplog.text


def test_prune_old_logs_does_not_delete_directories(tmp_path, caplog):
    directory = tmp_path / "log_2020-01-01.txt"
    directory.mkdir()
    _prune_old_logs(tmp_path, TODAY)
    assert directory.is_dir()
    assert "Could not prune daily log" in caplog.text


def test_configure_logging_prunes_and_still_writes_redacted_daily_log(host, monkeypatch):
    monkeypatch.setattr(host, "now", lambda: datetime(2026, 11, 7, tzinfo=timezone.utc))
    old = host.logs_dir() / "log_2020-01-01.txt"
    old.touch()
    boundary = host.logs_dir() / f"log_{TODAY - timedelta(days=90)}.txt"
    boundary.write_text("boundary")

    logger = configure_logging(host=host)
    logger.warning("retention test token=synthetic-sensitive-value")
    for handler in logger.handlers:
        handler.flush()

    assert not old.exists()
    assert boundary.read_text() == "boundary"
    output = (host.logs_dir() / f"log_{TODAY}.txt").read_text(encoding="utf-8")
    assert "retention test token=***REDACTED***" in output
    assert "synthetic-sensitive-value" not in output
