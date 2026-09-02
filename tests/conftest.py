"""Shared fixtures for the nightwatch test suite.

Everything runs against a temp directory ``DesktopHost`` so tests never touch
the real ``config/``, ``data/`` or ``logs/`` of the working tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nightwatch.host import DesktopHost
from nightwatch.db.repo import Repo

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
