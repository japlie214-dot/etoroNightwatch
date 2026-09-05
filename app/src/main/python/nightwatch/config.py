"""Typed configuration: ``config/config.toml`` plus ``config/.secrets.env``.

Two rules shape this module.

1. **Environment and credentials cannot disagree.**  eToro chooses demo vs real
   by URL path (``/execution/orders`` vs ``/execution/demo/orders``) and an API
   key is bound to one environment when it is created (DESIGN §1.1).  So the
   URL path segment and the credential set are *both derived* from one enum
   value; neither is separately settable.  There is no code path that can
   produce a real-money URL with demo keys or the reverse.
2. **Arming takes two keys.**  Live execution requires ``mode = "live"`` in the
   TOML *and* the ``NIGHTWATCH_ARM`` environment variable (DESIGN §5).  Neither
   alone is sufficient, and there is a single computed property that says so.

Nothing here touches the network, and secret *values* never appear in
``repr()`` output.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Mapping

from .host import Host, get_host

__all__ = [
    "ConfigError",
    "MissingCredentialError",
    "TradingMode",
    "EtoroEnvironment",
    "Credentials",
    "EtoroConfig",
    "StrategyConfig",
    "SymbolConfig",
    "TelegramConfig",
    "WebappConfig",
    "RiskConfig",
    "TiingoConfig",
    "MarketDataConfig",
    "Config",
    "TIER_CAPS",
    "TIER_DRAWDOWN_BOUNDS",
    "ARM_ENV_VAR",
    "load_config",
    "read_dotenv",
]

#: Environment variable that supplies the second arming key (DESIGN §5).
ARM_ENV_VAR: Final = "NIGHTWATCH_ARM"

#: Cumulative fraction of budget permitted at each SPY drawdown tier
#: (DESIGN §1.4).  These are hardcoded, not configurable: they were baked in at
#: import time by ``MARTINGALE_RATIO`` in the original strategy, so exposing
#: them as config would let the file lie about what the code does.
TIER_CAPS: Final[tuple[float, ...]] = (
    0.10070574586703619,
    0.22155264090747964,
    0.36656891495601180,
    0.54058844381425030,
    0.74941187844413660,
    1.0,
)

#: Lower drawdown bound of tiers 0..4; tier 5 is everything at or below -0.35.
#: Tier 0 applies while drawdown is above -0.10.
TIER_DRAWDOWN_BOUNDS: Final[tuple[float, ...]] = (-0.10, -0.15, -0.20, -0.25, -0.35)


class ConfigError(Exception):
    """Raised when configuration is missing, malformed or self-inconsistent."""


class MissingCredentialError(ConfigError):
    """Raised when a required secret is absent from ``.secrets.env``.

    Deliberately raised on *access*, not on load, so that commands which need
    no credentials (``initdb``, ``halt``, ``status``) work on a machine that has
    never been given keys.
    """


class TradingMode(StrEnum):
    """Whether the session may place orders at all."""

    DRY_RUN = "dry_run"
    LIVE = "live"


class EtoroEnvironment(StrEnum):
    """The eToro account environment.

    This single value selects both the URL path segment and the credential
    pair.  See the module docstring for why that matters.
    """

    DEMO = "demo"
    REAL = "real"


@dataclass(frozen=True, slots=True)
class Credentials:
    """An eToro ``x-api-key`` / ``x-user-key`` pair.

    ``repr`` is suppressed on the values so credentials cannot leak into a
    traceback or a log line.
    """

    label: str
    api_key: str = field(repr=False)
    user_key: str = field(repr=False)

    def as_headers(self) -> dict[str, str]:
        """Return the two static auth headers (DESIGN §1.1).

        The per-request ``x-request-id`` UUID is added by the client, not here.
        """
        return {"x-api-key": self.api_key, "x-user-key": self.user_key}


# Which secrets back which environment.  There is now a single shared public
# (application) key across every environment and role -- ETORO_PUBLIC_KEY --
# paired with a role/environment-specific per-user key.  The demo environment
# has one per-user key for both roles; the real environment splits write and
# read by privilege (DESIGN §5, "separate credentials by privilege").
#
# Corrected 2026-09-04 against a live call: this tuple is (x-api-key secret
# name, x-user-key secret name).  It previously read
# ("ETORO_DEMO_PRIVATE_KEY", "ETORO_PUBLIC_KEY") -- i.e. api_key
# came from the long, JWT-shaped per-user secret and user_key came from the
# short public one.  eToro's own docs (api-portal.etoro.com) say the
# opposite: x-api-key "identifies the application" (the short key) and
# x-user-key "identifies your user account" (the long one, generated via
# Settings > Trading > API Key Management). A live GET /api/v1/balances
# proved it: the old order 401'd, this order returns 200 with real demo
# balance data. See DESIGN §1.1 fact table for the corrected header mapping.
_CREDENTIAL_KEYS: Final[dict[EtoroEnvironment, dict[str, tuple[str, str]]]] = {
    EtoroEnvironment.DEMO: {
        "write": ("ETORO_PUBLIC_KEY", "ETORO_DEMO_PRIVATE_KEY"),
        "read": ("ETORO_PUBLIC_KEY", "ETORO_DEMO_PRIVATE_KEY"),
    },
    EtoroEnvironment.REAL: {
        "write": ("ETORO_PUBLIC_KEY", "ETORO_PRIVATE_KEY"),
        "read": ("ETORO_PUBLIC_KEY", "ETORO_READ_PRIVATE_KEY"),
    },
}


@dataclass(frozen=True, slots=True)
class EtoroConfig:
    """eToro connection settings.

    The class stores exactly one environment-selecting value, ``environment``.
    The URL path segment and the credential set are computed properties over
    it, so they are structurally incapable of disagreeing.  Callers must never
    build a URL by concatenating their own ``"demo"`` literal; use
    :meth:`orders_path` and :attr:`path_segment`.
    """

    environment: EtoroEnvironment
    base_url: str = "https://public-api.etoro.com"
    timeout_seconds: float = 30.0
    #: Raw secret mapping; excluded from repr so nothing leaks.
    secrets: Mapping[str, str] = field(default_factory=dict, repr=False)

    @property
    def is_real_money(self) -> bool:
        """True when this configuration points at the real-money account."""
        return self.environment is EtoroEnvironment.REAL

    @property
    def path_segment(self) -> str:
        """``"demo/"`` for the demo environment, ``""`` for real.

        Derived from :attr:`environment` — never stored separately.
        """
        return "demo/" if self.environment is EtoroEnvironment.DEMO else ""

    def orders_path(self) -> str:
        """Return the order-entry path for this environment (DESIGN §1.1)."""
        return f"/api/v2/trading/execution/{self.path_segment}orders"

    def _credentials(self, role: str) -> Credentials:
        if role not in ("write", "read"):
            raise ConfigError(f"unknown credential role {role!r}")
        api_name, user_name = _CREDENTIAL_KEYS[self.environment][role]
        api_key = self.secrets.get(api_name, "")
        user_key = self.secrets.get(user_name, "")
        missing = [n for n, v in ((api_name, api_key), (user_name, user_key)) if not v]
        if missing:
            raise MissingCredentialError(
                f"missing {', '.join(missing)} in config/.secrets.env "
                f"(required for the {role} role in the "
                f"{self.environment.value} environment)"
            )
        return Credentials(
            label=f"{self.environment.value}:{role}",
            api_key=api_key,
            user_key=user_key,
        )

    @property
    def write_credentials(self) -> Credentials:
        """Credentials used by the trading session. Raises if absent."""
        return self._credentials("write")

    @property
    def read_credentials(self) -> Credentials:
        """Read-only credentials used by the webapp. Raises if absent."""
        return self._credentials("read")

    def has_credentials(self, role: str = "write") -> bool:
        """Return True if the named role's credentials are present."""
        try:
            self._credentials(role)
        except MissingCredentialError:
            return False
        return True


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    """The live parameter set for Std Band Ladder Cash Recycle (DESIGN §1.4).

    Defaults are the documented live values.  ``CASH_BAND_FRACTION`` and
    ``MARTINGALE_RATIO`` are deliberately absent: both were inert in the source
    strategy and were dropped.  Tier caps are the module constant
    :data:`TIER_CAPS`, not a field.
    """

    core_fraction: float = 0.4
    core_floor_pct: float = 0.5
    order_divisor: int = 60
    #: Raised 10.0 -> 50.0 on 2026-09-04 against the live eligibility endpoint:
    #: SCHG's ``minPositionExposure`` is 50.0 and UPRO's is 10.0, so 50 is the
    #: smallest floor at which both managed symbols are always orderable.  The
    #: per-instrument minimum remains authoritative when it is higher.
    min_order_size: float = 50.0
    buy_threshold: float = -2.0
    sell_threshold: float = 3.0
    max_lots: int = 60
    #: Changed from 280 to 0 (DESIGN §1.4).  The indicator's own ``None``
    #: already gates trading until 265 bars exist; ``_bars`` is persisted, so a
    #: nonzero value here would strand a freshly restarted app in warmup.
    warmup_bars: int = 0
    cash_target_multiplier: float = 8.0
    cone_horizon: int = 5
    cone_center_mode: str = "close"
    exit_mode: str = "fixed"
    #: Inert while ``exit_mode == "fixed"``; retained for parity.
    trailing_stop_pct: float = 0.05
    dwell_bars: int = 1
    #: ``None`` means a flat z threshold at every tier.
    tier_z_thresholds: tuple[float, ...] | None = None
    reentry_tranches: int = 3
    bucket_expiry_bars: int = 20
    #: Non-null wins over the annual auto budget immediately (DESIGN §4.5).
    budget_override_usd: float | None = None
    #: Bars of history the cone indicator needs before it returns a value:
    #: 260-bar lookback + 5-bar shift (DESIGN §1.4).
    required_history_bars: int = 265
    #: The sleeve, by **exclusion** (DESIGN §4.5, product decision superseding
    #: the earlier inclusion model): the USD the operator withholds from the
    #: app for their own use, outside etoroNightwatch. ``sleeve_cash =
    #: max(0, broker_available_cash - cash_reserve_usd)`` is computed at
    #: runtime by the engine (step 9) -- this field is only the reserve input.
    #: ``0`` (also what ``None``/absent collapses to) means "use all available
    #: cash." This is a **product decision, not a safety rail**: if the
    #: resulting sleeve is too small to trade, the app warns on the UI and
    #: keeps running -- it never blocks a session or refuses to start over
    #: this (``strategy.min_allocation``).
    cash_reserve_usd: float = 0.0

    def __post_init__(self) -> None:
        if self.order_divisor <= 0:
            raise ConfigError("strategy.order_divisor must be positive")
        if self.cone_horizon <= 0:
            raise ConfigError("strategy.cone_horizon must be positive")
        if not 0.0 < self.core_fraction <= 1.0:
            raise ConfigError("strategy.core_fraction must be in (0, 1]")
        if not 0.0 <= self.core_floor_pct <= 1.0:
            raise ConfigError("strategy.core_floor_pct must be in [0, 1]")
        if self.buy_threshold >= self.sell_threshold:
            raise ConfigError("strategy.buy_threshold must be below sell_threshold")
        if self.warmup_bars < 0:
            raise ConfigError("strategy.warmup_bars must not be negative")
        if self.max_lots <= 0:
            raise ConfigError("strategy.max_lots must be positive")
        if self.reentry_tranches <= 0:
            raise ConfigError("strategy.reentry_tranches must be positive")
        if self.cone_center_mode not in ("close",):
            # "mid" was accepted here historically but has never had an
            # implementation anywhere in this codebase -- strategy/cone.py
            # only implements the "close" rule, and the reference indicator
            # this signal was ported from
            # (etoro_trader/indicators/volatility/expected_move_cone.py)
            # supports ("ema", "close"), not "mid" at all. Accepting "mid"
            # here let it pass validation and then silently compute "close"
            # anyway -- exactly this project's recurring silent-wrong-value
            # failure mode. "ema" is deliberately not accepted either: no
            # centering rule for it exists yet, and the live config
            # (std_band_ladder_martingale) only ever sets "close" -- adding
            # "ema" now would just recreate the same trap with a different
            # string. Extend this tuple only alongside a real "ema"
            # implementation in strategy/cone.py.
            raise ConfigError("strategy.cone_center_mode must be 'close'")
        if self.exit_mode not in ("fixed", "trailing"):
            raise ConfigError("strategy.exit_mode must be 'fixed' or 'trailing'")
        if self.budget_override_usd is not None and self.budget_override_usd <= 0:
            raise ConfigError("strategy.budget_override_usd must be positive or null")
        if self.cash_reserve_usd < 0:
            raise ConfigError("strategy.cash_reserve_usd must not be negative")
        if self.min_order_size <= 0:
            raise ConfigError("strategy.min_order_size must be positive")


@dataclass(frozen=True, slots=True)
class SymbolConfig:
    """The three instruments and their roles (DESIGN §1.4)."""

    signal: str = "SPY"
    core: str = "SCHG"
    risk: str = "UPRO"

    @property
    def all(self) -> tuple[str, str, str]:
        """All three tickers, signal first."""
        return (self.signal, self.core, self.risk)

    @property
    def traded(self) -> tuple[str, str]:
        """The two tickers we actually trade.  SPY is signal-only."""
        return (self.core, self.risk)


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    """Telegram settings (DESIGN §7).  Token and chat id come from secrets."""

    enabled: bool = True
    #: Hourly, per ``setExactAndAllowWhileIdle``'s 9-minute floor (DESIGN §6.6).
    poll_interval_seconds: int = 3600
    send_heartbeat: bool = True
    timeout_seconds: float = 15.0
    secrets: Mapping[str, str] = field(default_factory=dict, repr=False)

    @property
    def bot_token(self) -> str:
        """Bot token from ``TELEGRAM_BOT_TOKEN``. Raises if absent."""
        token = self.secrets.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            raise MissingCredentialError(
                "missing TELEGRAM_BOT_TOKEN in config/.secrets.env"
            )
        return token

    @property
    def allowed_chat_id(self) -> str:
        """The single allowlisted chat id (DESIGN §7.2). Raises if absent."""
        chat_id = self.secrets.get("TELEGRAM_CHAT_ID", "")
        if not chat_id:
            raise MissingCredentialError(
                "missing TELEGRAM_CHAT_ID in config/.secrets.env"
            )
        return chat_id

    @property
    def is_configured(self) -> bool:
        """True when both the token and the chat id are present."""
        return bool(
            self.secrets.get("TELEGRAM_BOT_TOKEN")
            and self.secrets.get("TELEGRAM_CHAT_ID")
        )

    @property
    def command_secret(self) -> str:
        """Shared HMAC secret for inbound command auth (DESIGN §7.2).

        Every inbound command carries an HMAC of ``(command, counter)`` keyed
        by this value. Raises if absent, mirroring :attr:`bot_token`.
        """
        secret = self.secrets.get("NIGHTWATCH_COMMAND_SECRET", "")
        if not secret:
            raise MissingCredentialError(
                "missing NIGHTWATCH_COMMAND_SECRET in config/.secrets.env"
            )
        return secret

    @property
    def has_command_secret(self) -> bool:
        """True when ``NIGHTWATCH_COMMAND_SECRET`` is present."""
        return bool(self.secrets.get("NIGHTWATCH_COMMAND_SECRET"))


@dataclass(frozen=True, slots=True)
class WebappConfig:
    """Embedded FastAPI dashboard settings (DESIGN §8)."""

    enabled: bool = True
    host: str = "0.0.0.0"
    #: Must stay above 1024: binding lower requires root on Android.
    port: int = 8080

    def __post_init__(self) -> None:
        if not 1024 < self.port <= 65535:
            raise ConfigError(
                "webapp.port must be above 1024 (binding lower requires root)"
            )


@dataclass(frozen=True, slots=True)
class TiingoConfig:
    """Tiingo market-data credentials and network settings.

    Tiingo is the source of record for daily adjusted history and the
    provisional-close cross-check (DESIGN_market_data.md §2.7, §3.1, §9) --
    never eToro's rates endpoint, which §2.9 proves unsafe to read for a
    current price. The key is supplied as an ``Authorization: Token <key>``
    header, never a URL query parameter, so it cannot leak through server
    access logs or a copy-pasted URL.
    """

    base_url: str = "https://api.tiingo.com"
    #: Every network call gets an explicit per-call timeout (DESIGN_market_data
    #: §8 gotchas: a probe hung for exactly this reason last session).
    timeout_seconds: float = 10.0
    #: Raw secret mapping; excluded from repr so nothing leaks.
    secrets: Mapping[str, str] = field(default_factory=dict, repr=False)

    @property
    def api_key(self) -> str:
        """``TIINGO_KEY`` from ``.secrets.env``. Raises if absent."""
        key = self.secrets.get("TIINGO_KEY", "")
        if not key:
            raise MissingCredentialError("missing TIINGO_KEY in config/.secrets.env")
        return key

    @property
    def has_key(self) -> bool:
        """True when ``TIINGO_KEY`` is present, without raising."""
        return bool(self.secrets.get("TIINGO_KEY"))


@dataclass(frozen=True, slots=True)
class MarketDataConfig:
    """Preflight gate 10 tolerances (DESIGN_market_data.md §6, §10.4).

    **Partially calibrated as of 2026-09-05.**
    ``provisional_close_deviation_pct`` now comes from a real observed
    distribution (272 dates of Tiingo-raw-vs-eToro deviation, see its own
    field docstring) -- §10.4's requirement is satisfied for that one field.
    ``max_rate_age_minutes`` and ``rate_vs_candle_deviation_pct`` remain
    provisional placeholders: §10.4 is explicit the real values must come
    from the forward test's observed per-symbol distribution (still
    unbuilt, DESIGN_market_data.md §12 step 7), not be guessed. Their
    defaults are chosen only to sit between the two magnitudes this
    session's live evidence established (DESIGN_market_data.md §2.9,
    §2.11(C)): eToro's own candle-vs-rates venue/timing noise is ~0.1%,
    while the UPRO rates-freeze bug measured +1.322%/+19.0h -- set to catch
    the latter while tolerating the former, pending real calibration.
    """

    #: Max age of ``Rate.timestamp`` before a live quote is considered stale,
    #: cross-checked against eToro's own candles (DESIGN_market_data §6 gate
    #: 10 item 1, §2.9's UPRO evidence: ~1140 minutes stale vs ~0 for a fresh
    #: quote). 15 minutes is comfortably above ordinary poll-to-poll gaps and
    #: comfortably below any real staleness incident observed so far.
    max_rate_age_minutes: float = 15.0
    #: Max fractional deviation between eToro's rate price and eToro's own
    #: candle close for the same symbol at the same time (the "free internal
    #: cross-check", DESIGN_market_data §5, §6 gate 10 item 1). 0.75% sits
    #: above the ~0.1% venue/timing noise §2.11(C) measured and below the
    #: 1.322% UPRO freeze error §2.9 measured.
    rate_vs_candle_deviation_pct: float = 0.0075
    #: Max fractional deviation between the provisional close actually used
    #: (Tiingo IEX primary, or eToro candle fallback) and eToro's own candle,
    #: when both are available to cross-check (DESIGN_market_data §3.1, §5.1,
    #: §8, §10.4).
    #:
    #: **Set from a real observed distribution, 2026-09-05** (HANDOFF_2026-09
    #: -05_evening.md §4; FINDINGS_window_live_2026-09-05.md §3): Tiingo raw
    #: ``close`` vs eToro candle ``close`` -- both raw, like-with-like, per
    #: the §4 basis split (an ``adjClose``-vs-eToro comparison at the window
    #: edge diverges 1.09% and would breach any sane tolerance; this
    #: comparison must never be, and today is not, that one --
    #: ``engine/preflight.py``'s ``_check_provisional_close`` compares
    #: ``ProvisionalClose.value`` (always raw: Tiingo IEX ``tngoLast`` or a
    #: raw eToro candle close) against ``Candle.close`` (also raw), verified
    #: 2026-09-05) -- across 272 common dates, 2025-08-01..2026-09-04: mean
    #: |dev| 0.128%, median 0.124%, max 0.640%. Set to 1.0% (0.010): ~56%
    #: headroom above the observed max (0.640%) so ordinary venue/timing
    #: noise never trips it, while staying comfortably below the +1.322%
    #: UPRO rates-freeze error (§2.9) this gate must still catch. Was the
    #: placeholder 0.0075 (0.75%), which the observed max sat uncomfortably
    #: close to -- revisit again once the forward test accumulates its own
    #: distribution (§10.4).
    provisional_close_deviation_pct: float = 0.010
    #: Max age of ``Candle.timestamp`` before an **intraday** candle
    #: (``OneMinute``, ``FiveMinutes``, ... ``FourHours`` --
    #: ``engine.preflight.INTRADAY_CANDLE_INTERVALS``) is considered stale
    #: (added 2026-09-05, closing the hole where the candle was the trusted
    #: reference by construction with its own freshness never checked).
    #: §10.4 applies here too -- provisional, pending forward-test
    #: calibration.
    #:
    #: This bound applies **only** to intraday intervals. It intentionally
    #: has no ``OneDay`` counterpart: a ``OneDay`` bar is legitimately hours
    #: old at T-30 relative to the *prior* session's close, and even eToro's
    #: own live-updating daily bar (§2.11 shows its close tracking the
    #: current price intraday) may carry a date-only timestamp with no
    #: intraday time component at all. Timestamp age is therefore not a
    #: meaningful freshness signal for a ``OneDay`` candle -- there is no
    #: honest minutes-based number to put here for it. Gate 10 instead
    #: relies on the existing value-agreement check (``_check_provisional_
    #: close`` comparing the provisional close to ``candle.close``) for that
    #: case; see ``engine/preflight.py``'s ``_check_candle_freshness`` for
    #: where that branch happens and what it does and does not guarantee.
    #: An unrecognised or missing ``Candle.interval`` fails closed rather
    #: than falling back to this bound by default -- guessing the wrong
    #: bound is worse than refusing to guess.
    max_candle_age_minutes_intraday: float = 20.0

    def __post_init__(self) -> None:
        if self.max_rate_age_minutes <= 0:
            raise ConfigError("market_data.max_rate_age_minutes must be positive")
        if not 0.0 < self.rate_vs_candle_deviation_pct < 1.0:
            raise ConfigError(
                "market_data.rate_vs_candle_deviation_pct must be in (0, 1)"
            )
        if not 0.0 < self.provisional_close_deviation_pct < 1.0:
            raise ConfigError(
                "market_data.provisional_close_deviation_pct must be in (0, 1)"
            )
        if self.max_candle_age_minutes_intraday <= 0:
            raise ConfigError(
                "market_data.max_candle_age_minutes_intraday must be positive"
            )


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """Hard caps enforced by preflight gate 12 (DESIGN §5)."""

    max_orders_per_session: int = 10
    max_notional_per_session_usd: float = 5000.0
    max_daily_loss_usd: float | None = None
    #: Gate 2 aborts below this when the host is on battery.
    min_battery_percent: int = 40

    def __post_init__(self) -> None:
        if self.max_orders_per_session <= 0:
            raise ConfigError("risk.max_orders_per_session must be positive")
        if self.max_notional_per_session_usd <= 0:
            raise ConfigError("risk.max_notional_per_session_usd must be positive")
        if not 0 <= self.min_battery_percent <= 100:
            raise ConfigError("risk.min_battery_percent must be between 0 and 100")


@dataclass(frozen=True, slots=True)
class Config:
    """The whole resolved configuration."""

    mode: TradingMode
    etoro: EtoroConfig
    strategy: StrategyConfig
    symbols: SymbolConfig
    telegram: TelegramConfig
    webapp: WebappConfig
    risk: RiskConfig
    tiingo: TiingoConfig = field(default_factory=TiingoConfig)
    market_data: MarketDataConfig = field(default_factory=MarketDataConfig)
    #: Source paths, for ``status`` output and diagnostics.
    config_path: Path | None = None
    secrets_path: Path | None = None
    #: Whether ``NIGHTWATCH_ARM`` was set in the environment at load time.
    arm_flag_present: bool = False

    @property
    def is_armed(self) -> bool:
        """True only when *both* arming keys are present (DESIGN §5).

        ``mode = "live"`` in the TOML **and** ``NIGHTWATCH_ARM`` set in the
        environment.  Anything else is a dry run: the session still computes and
        reports a decision, it just does not place orders.  The HALT flag is a
        separate, third veto, checked by preflight gate 1.
        """
        return self.mode is TradingMode.LIVE and self.arm_flag_present

    @property
    def environment(self) -> EtoroEnvironment:
        """Shorthand for ``config.etoro.environment``."""
        return self.etoro.environment

    def describe(self) -> str:
        """Return a one-line human summary containing no secrets."""
        arm = "ARMED" if self.is_armed else "not armed"
        flag = "set" if self.arm_flag_present else "unset"
        return (
            f"mode={self.mode.value} environment={self.environment.value} "
            f"({arm}; {ARM_ENV_VAR}={flag})"
        )


def read_dotenv(path: Path) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` secrets file.

    A deliberately tiny reader — no external dependency, no interpolation, no
    shell semantics.  Blank lines and ``#`` comments are skipped, a leading
    ``export `` is tolerated, and one layer of matching single or double quotes
    is stripped.  A missing file yields an empty mapping.
    """
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def _table(data: Mapping[str, Any], name: str) -> dict[str, Any]:
    section = data.get(name, {})
    if not isinstance(section, dict):
        raise ConfigError(f"[{name}] must be a table in config.toml")
    return dict(section)


def _enum(raw: Any, enum: type[StrEnum], name: str) -> Any:
    try:
        return enum(str(raw))
    except ValueError:
        allowed = ", ".join(repr(m.value) for m in enum)
        raise ConfigError(f"{name} must be one of {allowed}, got {raw!r}") from None


def _tier_thresholds(raw: Any) -> tuple[float, ...] | None:
    # TOML has no null literal, so both "absent" and "empty list" mean null:
    # one flat threshold at every drawdown tier.
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)):
        raise ConfigError("strategy.tier_z_thresholds must be null or a list of numbers")
    if len(raw) == 0:
        return None
    if len(raw) != len(TIER_CAPS):
        raise ConfigError(
            f"strategy.tier_z_thresholds must have {len(TIER_CAPS)} entries "
            f"(one per drawdown tier), got {len(raw)}"
        )
    return tuple(float(v) for v in raw)


def load_config(
    config_path: Path | None = None,
    secrets_path: Path | None = None,
    env: Mapping[str, str] | None = None,
    host: Host | None = None,
) -> Config:
    """Load and validate the full configuration.

    Args:
        config_path: ``config.toml`` location.  Defaults to the host's config
            directory.
        secrets_path: ``.secrets.env`` location.  Defaults to the host's config
            directory.  A missing file is not an error here — credentials are
            demanded only when they are used.
        env: Environment mapping, defaulting to :data:`os.environ`.  Injectable
            so the arming logic is testable without mutating the process.
        host: Host implementation, defaulting to the installed one.

    Returns:
        A fully populated, frozen :class:`Config`.

    Raises:
        ConfigError: if ``config.toml`` is missing, unparseable, or contains a
            value outside its permitted range.
    """
    active_host = host or get_host()
    environ = os.environ if env is None else env
    config_path = config_path or (active_host.config_dir() / "config.toml")
    secrets_path = secrets_path or (active_host.config_dir() / ".secrets.env")

    if not config_path.exists():
        raise ConfigError(f"config file not found: {config_path}")
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"could not parse {config_path}: {exc}") from exc

    secrets = read_dotenv(secrets_path)

    trading = _table(data, "trading")
    mode = _enum(trading.get("mode", "dry_run"), TradingMode, "trading.mode")

    etoro_table = _table(data, "etoro")
    environment = _enum(
        etoro_table.get("environment", "demo"), EtoroEnvironment, "etoro.environment"
    )
    # Guard against a config that tries to set the path segment or a credential
    # set by hand.  Both are derived; silently ignoring such a key would be the
    # exact inconsistency this module exists to prevent.
    for forbidden in ("path_segment", "demo", "api_key", "user_key", "credentials"):
        if forbidden in etoro_table:
            raise ConfigError(
                f"etoro.{forbidden} is not settable: the URL path segment and the "
                "credential set are both derived from etoro.environment"
            )
    etoro = EtoroConfig(
        environment=environment,
        base_url=str(
            etoro_table.get("base_url", "https://public-api.etoro.com")
        ).rstrip("/"),
        timeout_seconds=float(etoro_table.get("timeout_seconds", 30.0)),
        secrets=secrets,
    )

    st = _table(data, "strategy")
    strategy = StrategyConfig(
        core_fraction=float(st.get("core_fraction", 0.4)),
        core_floor_pct=float(st.get("core_floor_pct", 0.5)),
        order_divisor=int(st.get("order_divisor", 60)),
        min_order_size=float(st.get("min_order_size", 50.0)),
        buy_threshold=float(st.get("buy_threshold", -2.0)),
        sell_threshold=float(st.get("sell_threshold", 3.0)),
        max_lots=int(st.get("max_lots", 60)),
        warmup_bars=int(st.get("warmup_bars", 0)),
        cash_target_multiplier=float(st.get("cash_target_multiplier", 8.0)),
        cone_horizon=int(st.get("cone_horizon", 5)),
        cone_center_mode=str(st.get("cone_center_mode", "close")),
        exit_mode=str(st.get("exit_mode", "fixed")),
        trailing_stop_pct=float(st.get("trailing_stop_pct", 0.05)),
        dwell_bars=int(st.get("dwell_bars", 1)),
        tier_z_thresholds=_tier_thresholds(st.get("tier_z_thresholds")),
        reentry_tranches=int(st.get("reentry_tranches", 3)),
        bucket_expiry_bars=int(st.get("bucket_expiry_bars", 20)),
        budget_override_usd=(
            None
            if st.get("budget_override_usd") in (None, "")
            else float(st["budget_override_usd"])
        ),
        required_history_bars=int(st.get("required_history_bars", 265)),
        cash_reserve_usd=(
            0.0
            if st.get("cash_reserve_usd") in (None, "")
            else float(st["cash_reserve_usd"])
        ),
    )

    symbols_table = _table(data, "symbols")
    symbols = SymbolConfig(
        signal=str(symbols_table.get("signal", "SPY")).upper(),
        core=str(symbols_table.get("core", "SCHG")).upper(),
        risk=str(symbols_table.get("risk", "UPRO")).upper(),
    )

    tg = _table(data, "telegram")
    telegram = TelegramConfig(
        enabled=bool(tg.get("enabled", True)),
        poll_interval_seconds=int(tg.get("poll_interval_seconds", 3600)),
        send_heartbeat=bool(tg.get("send_heartbeat", True)),
        timeout_seconds=float(tg.get("timeout_seconds", 15.0)),
        secrets=secrets,
    )

    web = _table(data, "webapp")
    webapp = WebappConfig(
        enabled=bool(web.get("enabled", True)),
        host=str(web.get("host", "0.0.0.0")),
        port=int(web.get("port", 8080)),
    )

    rk = _table(data, "risk")
    risk = RiskConfig(
        max_orders_per_session=int(rk.get("max_orders_per_session", 10)),
        max_notional_per_session_usd=float(rk.get("max_notional_per_session_usd", 5000.0)),
        max_daily_loss_usd=(
            None
            if rk.get("max_daily_loss_usd") in (None, "")
            else float(rk["max_daily_loss_usd"])
        ),
        min_battery_percent=int(rk.get("min_battery_percent", 40)),
    )

    tg_market = _table(data, "tiingo")
    tiingo = TiingoConfig(
        base_url=str(tg_market.get("base_url", "https://api.tiingo.com")).rstrip("/"),
        timeout_seconds=float(tg_market.get("timeout_seconds", 10.0)),
        secrets=secrets,
    )

    md = _table(data, "market_data")
    market_data = MarketDataConfig(
        max_rate_age_minutes=float(md.get("max_rate_age_minutes", 15.0)),
        rate_vs_candle_deviation_pct=float(md.get("rate_vs_candle_deviation_pct", 0.0075)),
        provisional_close_deviation_pct=float(
            md.get("provisional_close_deviation_pct", 0.010)
        ),
        max_candle_age_minutes_intraday=float(
            md.get("max_candle_age_minutes_intraday", 20.0)
        ),
    )

    return Config(
        mode=mode,
        etoro=etoro,
        strategy=strategy,
        symbols=symbols,
        telegram=telegram,
        webapp=webapp,
        risk=risk,
        tiingo=tiingo,
        market_data=market_data,
        config_path=config_path,
        secrets_path=secrets_path,
        arm_flag_present=bool(environ.get(ARM_ENV_VAR, "").strip()),
    )
