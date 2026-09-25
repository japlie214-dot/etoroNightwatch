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
    ReconciliationConfig,
    StrategyConfig,
    TradingMode,
    load_config,
)
from nightwatch.host import DesktopHost


def test_load_config_defaults(config_path: Path, secrets_path: Path, host: DesktopHost) -> None:
    config = load_config(config_path=config_path, secrets_path=secrets_path, env={}, host=host)
    assert config.mode is TradingMode.DRY_RUN
    assert config.etoro.environment is EtoroEnvironment.DEMO
    assert config.symbols.all == ("SPY", "SCHG", "UPRO")
    # ReconciliationConfig's defaults load even with no [reconciliation]
    # section in config.toml at all (MINIMAL_CONFIG_TOML has none) -- the
    # PROPOSED-but-unconfirmed N=3 default, and the auto-adopt size
    # tolerance, both come through untouched.
    assert config.reconciliation.non_terminal_order_sessions_before_abandoned == 3
    assert config.reconciliation.abandoned_intent_size_tolerance_pct == 0.02


def test_load_config_reconciliation_section_overrides_defaults(
    config_path: Path, secrets_path: Path, host: DesktopHost
) -> None:
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + "\n[reconciliation]\n"
        + "non_terminal_order_sessions_before_abandoned = 5\n"
        + "abandoned_intent_size_tolerance_pct = 0.05\n",
        encoding="utf-8",
    )
    config = load_config(config_path=config_path, secrets_path=secrets_path, env={}, host=host)
    assert config.reconciliation.non_terminal_order_sessions_before_abandoned == 5
    assert config.reconciliation.abandoned_intent_size_tolerance_pct == 0.05


# ---------------------------------------------------------------------------
# ReconciliationConfig (DESIGN_session.md §3.4/§14 item 2 -- the operator's
# 2026-09-06 "no forced human-in-the-loop" decision). N (the sessions-before-
# abandoned threshold) is PROPOSED, not confirmed -- these tests pin the
# validation behaviour, not the specific default value's correctness.
# ---------------------------------------------------------------------------


def test_reconciliation_config_defaults() -> None:
    config = ReconciliationConfig()
    assert config.non_terminal_order_sessions_before_abandoned == 3
    assert config.abandoned_intent_size_tolerance_pct == 0.02


def test_reconciliation_config_rejects_non_positive_session_threshold() -> None:
    with pytest.raises(ConfigError):
        ReconciliationConfig(non_terminal_order_sessions_before_abandoned=0)


def test_reconciliation_config_rejects_negative_session_threshold() -> None:
    with pytest.raises(ConfigError):
        ReconciliationConfig(non_terminal_order_sessions_before_abandoned=-1)


def test_reconciliation_config_rejects_zero_size_tolerance() -> None:
    with pytest.raises(ConfigError):
        ReconciliationConfig(abandoned_intent_size_tolerance_pct=0.0)


def test_reconciliation_config_rejects_size_tolerance_at_or_above_one() -> None:
    with pytest.raises(ConfigError):
        ReconciliationConfig(abandoned_intent_size_tolerance_pct=1.0)


def test_reconciliation_config_accepts_a_custom_valid_threshold() -> None:
    config = ReconciliationConfig(
        non_terminal_order_sessions_before_abandoned=7,
        abandoned_intent_size_tolerance_pct=0.10,
    )
    assert config.non_terminal_order_sessions_before_abandoned == 7
    assert config.abandoned_intent_size_tolerance_pct == 0.10


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


def test_cone_center_mode_mid_rejected() -> None:
    """"mid" was accepted by StrategyConfig.__post_init__'s old validation
    tuple ("close", "mid") despite having no implementation anywhere --
    strategy/cone.py only implements the "close" rule, and the reference
    indicator this signal was ported from (expected_move_cone.py) supports
    ("ema", "close"), not "mid" at all. Setting cone_center_mode="mid" used
    to pass validation and then silently compute "close" anyway -- this
    project's recurring silent-wrong-value failure mode. It must now raise.
    """
    with pytest.raises(ConfigError):
        StrategyConfig(cone_center_mode="mid")


def test_cone_center_mode_ema_rejected() -> None:
    """"ema" is not accepted either -- no centering rule for it exists in
    strategy/cone.py, and the live config only ever sets "close". Accepting
    "ema" now, with no implementation behind it, would just recreate the
    same silent-wrong-value trap under a different string.
    """
    with pytest.raises(ConfigError):
        StrategyConfig(cone_center_mode="ema")


def test_cone_center_mode_close_accepted() -> None:
    """The one implemented, live value must keep working."""
    config = StrategyConfig(cone_center_mode="close")
    assert config.cone_center_mode == "close"


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
