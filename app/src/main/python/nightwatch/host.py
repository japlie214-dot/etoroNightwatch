"""The one place where "which machine am I on?" is allowed to matter.

Everything else in :mod:`nightwatch` is plain CPython.  Host-specific
capabilities — where files live, battery level, Android permission state — are
reached only through the :class:`Host` protocol.  The desktop implementation
below is used for development, unit tests and the forward test.  The Android
build installs its own implementation at startup via :func:`set_host`; that
implementation lives in the Kotlin-facing bridge and is never imported here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = [
    "PowerStatus",
    "Host",
    "DesktopHost",
    "get_host",
    "set_host",
    "repo_root",
]

#: Environment variable that overrides the auto-detected project root.
HOME_ENV_VAR = "NIGHTWATCH_HOME"


def repo_root() -> Path:
    """Return the project root directory.

    On desktop this is derived from this file's location
    (``<root>/app/src/main/python/nightwatch/host.py``) unless
    ``NIGHTWATCH_HOME`` is set.  On Android the Android host overrides all
    directory accessors, so this function is not used there.
    """
    override = os.environ.get(HOME_ENV_VAR)
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[5]


@dataclass(frozen=True, slots=True)
class PowerStatus:
    """Snapshot of host power and scheduling permissions (preflight gate 2).

    On desktop the Android-specific fields are reported as satisfied, because
    there is no Doze, no App Standby and no exact-alarm permission to lose.
    """

    battery_percent: int | None
    is_charging: bool
    ignoring_battery_optimizations: bool
    can_schedule_exact_alarms: bool
    wake_lock_held: bool


@runtime_checkable
class Host(Protocol):
    """The capabilities the trading core needs from whatever machine it runs on."""

    name: str

    def config_dir(self) -> Path:
        """Directory holding ``config.toml`` and ``.secrets.env``."""

    def data_dir(self) -> Path:
        """Directory holding ``nightwatch.db``."""

    def logs_dir(self) -> Path:
        """Directory holding ``log_YYYY-MM-DD.txt``."""

    def halt_flag_path(self) -> Path:
        """Path of the ``HALT`` flag file checked by preflight gate 1."""

    def now(self) -> datetime:
        """Current instant, timezone-aware UTC.

        Routed through the host so the forward test can install a time-travel
        clock without monkeypatching :mod:`datetime`.
        """

    def power_status(self) -> PowerStatus:
        """Battery and Android permission state for preflight gate 2."""


class DesktopHost:
    """:class:`Host` implementation for Windows/Linux/macOS CPython."""

    name = "desktop"

    def __init__(self, root: Path | None = None) -> None:
        self._root = Path(root).resolve() if root is not None else repo_root()

    @property
    def root(self) -> Path:
        """Project root directory."""
        return self._root

    def config_dir(self) -> Path:
        """Return ``<root>/config``."""
        return self._root / "config"

    def data_dir(self) -> Path:
        """Return ``<root>/data``, creating it if needed."""
        path = self._root / "data"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def logs_dir(self) -> Path:
        """Return ``<root>/logs``, creating it if needed."""
        path = self._root / "logs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def halt_flag_path(self) -> Path:
        """Return ``<root>/HALT``."""
        return self._root / "HALT"

    def now(self) -> datetime:
        """Return the current UTC instant."""
        return datetime.now(timezone.utc)

    def power_status(self) -> PowerStatus:
        """Report a desktop as always mains-powered with no Android gates.

        A desktop has no Doze, no App Standby and no ``SCHEDULE_EXACT_ALARM``,
        so the Android-specific gates are reported satisfied rather than
        unknown.  Preflight gate 2 is therefore a no-op off-device, which is
        correct: those failure modes do not exist here.
        """
        return PowerStatus(
            battery_percent=None,
            is_charging=True,
            ignoring_battery_optimizations=True,
            can_schedule_exact_alarms=True,
            wake_lock_held=True,
        )


_host: Host = DesktopHost()


def get_host() -> Host:
    """Return the installed host implementation (desktop unless overridden)."""
    return _host


def set_host(host: Host) -> None:
    """Install a host implementation.

    Called once at startup by the Android bridge, and by the forward test to
    install a time-travel clock.
    """
    global _host
    _host = host
