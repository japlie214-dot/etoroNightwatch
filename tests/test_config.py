"""Config loading, the demo/real consistency guarantee, and two-key arming
(DESIGN §1.1, §5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nightwatch.config import (
    ARM_ENV_VAR,
    ConfigError,
    EtoroEnvironment,
    MissingCredentialError,
    TradingMode,
    load_config,
)
from nightwatch.host import DesktopHost


def test_load_config_defaults(config_path: Path, secrets_path: Path, host: DesktopHost) -> None:
    config = load_config(config_path=config_path, secrets_path=secrets_path, env={}, host=host)
    assert config.mode is TradingMode.DRY_RUN
    assert config.etoro.environment is EtoroEnvironment.DEMO
    assert config.symbols.all == ("SPY", "SCHG", "UPRO")


def test_missing_config_file_raises(tmp_path: Path, host: DesktopHost) -> None:
    with pytest.raises(ConfigError):
        load_config(config_path=tmp_path / "nope.toml", secrets_path=tmp_path / "nope.env", host=host)


def test_environment_selects_path_segment_and_credentials(
    config_path: Path, secrets_path: Path, host: DesktopHost
) -> None:
    """One config value drives both the URL path and the credential set
    (DESIGN §1.1) -- there is no way to make them disagree.

    Header mapping corrected 2026-09-04 against a live call: ``x-api-key``
    (``api_key``) is the short, application-identifying key
    (``ETORO_PUBLIC_KEY``); ``x-user-key`` (``user_key``) is the long,
    per-user key (``ETORO_DEMO_PRIVATE_KEY``) -- the reverse of what this
    test asserted before. See DESIGN §1.1 and ``config.py``'s
    ``_CREDENTIAL_KEYS`` comment.
    """
    secrets_path.write_text(
        "ETORO_DEMO_PRIVATE_KEY=demo-private\nETORO_PUBLIC_KEY=demo-public\n",
        encoding="utf-8",
    )
    config = load_config(config_path=config_path, secrets_path=secrets_path, env={}, host=host)
    assert config.etoro.path_segment == "demo/"
    assert config.etoro.orders_path() == "/api/v2/trading/execution/demo/orders"
    creds = config.etoro.write_credentials
    assert creds.api_key == "demo-public"
    assert creds.user_key == "demo-private"


def test_forbidden_manual_override_of_derived_fields(
    config_path: Path, secrets_path: Path, host: DesktopHost
) -> None:
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + '\n[etoro]\napi_key = "nope"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(config_path=config_path, secrets_path=secrets_path, env={}, host=host)


def test_missing_credential_raised_on_access_not_on_load(
    config_path: Path, secrets_path: Path, host: DesktopHost
) -> None:
    """Commands needing no credentials (initdb/halt/resume/status) must load
    config fine even with no secrets file at all."""
    config = load_config(config_path=config_path, secrets_path=secrets_path, env={}, host=host)
    with pytest.raises(MissingCredentialError):
        _ = config.etoro.write_credentials
    assert config.etoro.has_credentials("write") is False


@pytest.mark.parametrize(
    "mode_toml, env, expected",
    [
        ("dry_run", {}, False),
        ("live", {}, False),  # one key only: mode=live but no ARM env var
        ("dry_run", {ARM_ENV_VAR: "1"}, False),  # one key only: ARM set but mode=dry_run
        ("live", {ARM_ENV_VAR: "1"}, True),  # both keys: armed
    ],
)
def test_two_key_arming(
    config_path: Path,
    secrets_path: Path,
    host: DesktopHost,
    mode_toml: str,
    env: dict[str, str],
    expected: bool,
) -> None:
    """Live execution requires mode == 'live' AND NIGHTWATCH_ARM (DESIGN §5).
    Neither alone is sufficient."""
    text = config_path.read_text(encoding="utf-8").replace('mode = "dry_run"', f'mode = "{mode_toml}"')
    config_path.write_text(text, encoding="utf-8")
    config = load_config(config_path=config_path, secrets_path=secrets_path, env=env, host=host)
    assert config.is_armed is expected
