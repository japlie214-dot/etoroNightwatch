"""Shared fixtures for the nightwatch test suite.

Everything runs against a temp directory ``DesktopHost`` so tests never touch
the real ``config/``, ``data/`` or ``logs/`` of the working tree.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

_env_guard_enabled = True
_env_test_root: Path | None = None


def _guard_env_open(event, args):
    if not _env_guard_enabled or event != "open":
        return
    path = args[0]
    if isinstance(path, int):
        return
    path = Path(os.fsdecode(path))
    if path.name.lower() not in {".secrets.env", ".env"}:
        return
    path = path.resolve()
    if _env_test_root is None or not path.is_relative_to(_env_test_root):
        pytest.fail(f"Test suite refused env-file open outside tmp_path: {path}")


# Audit hooks cover builtins.open, io.open/Path.open and os.open, including
# collection-time imports before any fixture can protect real credentials.
sys.addaudithook(_guard_env_open)


def pytest_configure(config):
    global _env_guard_enabled
    _env_guard_enabled = True


def pytest_unconfigure(config):
    global _env_guard_enabled
    _env_guard_enabled = False


@pytest.fixture(autouse=True)
def _allow_env_files_only_in_tmp_path(tmp_path):
    global _env_test_root
    _env_test_root = tmp_path.resolve()
    try:
        yield
    finally:
        _env_test_root = None


from nightwatch.host import DesktopHost
from nightwatch.db.repo import Repo
from nightwatch.logging_setup import _NOISY_HTTP_LOGGERS

MINIMAL_CONFIG_TOML = """
[trading]
mode = "dry_run"

[etoro]
environment = "demo"

[symbols]
signal = "SPY"
core = "SCHG"
risk = "UPRO"

[strategy]

[telegram]
enabled = false

[webapp]
enabled = true
port = 8080

[risk]
"""


@pytest.fixture
def host(tmp_path: Path) -> DesktopHost:
    """A DesktopHost rooted at a fresh temp directory."""
    return DesktopHost(root=tmp_path)


@pytest.fixture
def config_path(host: DesktopHost) -> Path:
    """Write a minimal, valid config.toml under the temp host's config dir."""
    path = host.config_dir()
    path.mkdir(parents=True, exist_ok=True)
    toml_path = path / "config.toml"
    toml_path.write_text(MINIMAL_CONFIG_TOML, encoding="utf-8")
    return toml_path


@pytest.fixture
def secrets_path(host: DesktopHost) -> Path:
    """Path (not necessarily existing) for a .secrets.env under the temp host."""
    return host.config_dir() / ".secrets.env"


@pytest.fixture
def repo(host: DesktopHost) -> Repo:
    """An open, migrated repo backed by a fresh SQLite file under tmp_path."""
    r = Repo.open(host.data_dir() / "nightwatch.db", host=host)
    yield r
    r.close()


@pytest.fixture(autouse=True)
def _restore_logging_state():
    """Undo any global ``logging`` state ``configure_logging()`` may have set.

    ``configure_logging()`` (logging_setup.py) does not confine itself to the
    logger it hands back: it also sets the ``nightwatch`` logger's level,
    handlers and ``propagate`` flag, adds an owned file handler to the ROOT
    logger (and may touch the root level), and lowers a few noisy
    third-party loggers (httpx/httpcore/urllib3) to WARNING. None of that is
    undone when the test that triggered it ends, so whichever test calls
    ``configure_logging()`` first leaves the mutation in place for every
    test that runs after it in the same process.

    The dangerous one is ``nightwatch.propagate = False``: pytest's
    ``caplog`` attaches its capture handler to the ROOT logger, so once
    ``propagate`` is False, no ``nightwatch.*`` record reaches ``caplog``
    again for the rest of the run -- a guard test can pass or fail purely
    depending on whether some earlier, unrelated test happened to call
    ``configure_logging()`` first. That is test *order* dependence, which is
    exactly why the same suite disagreed between the system Python and this
    repo's ``.venv`` (see board nightwatch/handoff-2026-09-07.md): the two interpreters
    collect/order tests differently, so the pollution bit one and not the
    other.

    Restoring the mutated state here, once, for every test means no future
    test file has to know about or individually defend against this -- see
    the (now largely redundant) per-file workarounds in test_market_bars.py
    and test_notify_telegram.py, written before this fixture existed.
    """
    nightwatch_logger = logging.getLogger("nightwatch")
    root_logger = logging.getLogger()

    nw_level = nightwatch_logger.level
    nw_propagate = nightwatch_logger.propagate
    nw_handlers = list(nightwatch_logger.handlers)
    root_level = root_logger.level
    root_handlers = list(root_logger.handlers)
    noisy_levels = {name: logging.getLogger(name).level for name in _NOISY_HTTP_LOGGERS}

    try:
        yield
    finally:
        # Close and drop any handler added during the test (by
        # configure_logging() or otherwise) before restoring the pre-test
        # handler list, so file descriptors aren't leaked and handlers don't
        # accumulate test over test.
        for handler in nightwatch_logger.handlers:
            if handler not in nw_handlers:
                handler.close()
        nightwatch_logger.handlers[:] = nw_handlers
        nightwatch_logger.setLevel(nw_level)
        nightwatch_logger.propagate = nw_propagate

        for handler in root_logger.handlers:
            if handler not in root_handlers:
                handler.close()
        root_logger.handlers[:] = root_handlers
        root_logger.setLevel(root_level)

        for name, level in noisy_levels.items():
            logging.getLogger(name).setLevel(level)
