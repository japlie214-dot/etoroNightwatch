"""Logging: one file per day, a console handler, and a hard redaction filter.

The user's requirement is literal: **one day = one log file**, at
``logs/log_YYYY-MM-DD.txt`` (DESIGN §2). A trading session can run past
midnight only in the sense that it starts a few minutes before the US close
converted to Jakarta time (02:xx-03:xx WIB) — the file for "today" is always
named from the *host's local date* at the moment a handler is created, and a
fresh call to :func:`configure_logging` (e.g. at the start of the next day's
session) opens the next day's file.

The redaction filter exists because secrets *will* eventually end up in a
formatted log message — a config dump, an exception with a header dict
in it, a raw HTTP request echoed for debugging. This module treats that as
inevitable and scrubs the rendered text rather than trusting every call site
to remember not to log secrets. It cannot be bypassed by a call site: it is
installed on the handlers, not opted into per-logger.
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Final, Iterable

from .host import Host, get_host

__all__ = [
    "REDACTED",
    "RedactionFilter",
    "configure_logging",
    "harden_third_party_logging",
    "log_path_for_today",
]

#: Marker attribute set on handlers this module creates and attaches to the
#: ROOT logger, so a later :func:`configure_logging` call can tell "a handler
#: we own, safe to replace" apart from "a handler some other code (or a
#: third-party library) attached", and remove only the former.
_OWNED_HANDLER_MARKER: Final = "_nightwatch_owned_handler"

#: Noisy HTTP client libraries whose DEBUG/INFO chatter (including request
#: lines that can contain auth headers) we don't want cluttering the log.
#: This is defence-in-depth ONLY, not the security control: the level here
#: can be overridden by any later `logging.getLogger(...).setLevel(...)` call
#: (including one made by the library itself, or by user config), so it must
#: never be relied on to keep secrets out of the log. The RedactionFilter
#: attached to every root handler is the actual control and holds regardless
#: of what level these loggers end up at.
_NOISY_HTTP_LOGGERS: Final[tuple[str, ...]] = ("httpx", "httpcore", "urllib3")

REDACTED: Final = "***REDACTED***"

#: Config/env field-name fragments whose *value* is always redacted, wherever
#: it appears in a formatted message (e.g. "ETORO_PRIVATE_KEY=abcd..." or a repr
#: that leaked one despite the dataclasses' repr=False fields).
_SENSITIVE_NAME_FRAGMENTS: Final[tuple[str, ...]] = ("key", "token", "secret", "pat")

# One name=value pair, e.g. "ETORO_PRIVATE_KEY=abc123" or "x-api-key: abc123".
# Matches common separators (=, :, "=>") with optional quoting.
_NAME_VALUE_RE = re.compile(
    r"""(?P<name>[A-Za-z0-9_\-]*(?:key|token|secret|pat)[A-Za-z0-9_\-]*)
        (?P<sep>\s*[:=]\s*)
        (?P<quote>["']?)
        (?P<value>[^\s"'&,;]+)
        (?P=quote)
    """,
    re.IGNORECASE | re.VERBOSE,
)

# GitHub personal access tokens: github_pat_<...>, ghp_<...>, gho_<...>, etc.
_GITHUB_PAT_RE = re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b")

# Telegram bot token shape: digits ':' base64-ish, e.g. 123456789:AAExxxxxxxx
_TELEGRAM_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b")

# A generic long hex or base64-ish blob, the shape of most API keys/secrets.
# Conservative length floor (24) to avoid redacting incidental long numbers.
_HEX_RE = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_BASE64ISH_RE = re.compile(r"\b[A-Za-z0-9+/_-]{24,}={0,2}\b")


def _looks_sensitive_name(name: str) -> bool:
    lowered = name.lower()
    return any(fragment in lowered for fragment in _SENSITIVE_NAME_FRAGMENTS)


class RedactionFilter(logging.Filter):
    """A ``logging.Filter`` that scrubs secret-shaped text from every record.

    Applied to handlers (not loggers), so it runs regardless of which logger
    produced the record and cannot be skipped by using a differently-named
    logger. Rewrites ``record.msg`` after the message and any ``%``-args have
    been merged, so it sees the final rendered text.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - never suppress logging on a bad record
            return True
        redacted = self.redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True

    @staticmethod
    def redact(text: str) -> str:
        """Return ``text`` with every secret-shaped substring replaced."""

        def _name_value(match: "re.Match[str]") -> str:
            return f"{match.group('name')}{match.group('sep')}{REDACTED}"

        text = _NAME_VALUE_RE.sub(_name_value, text)
        text = _GITHUB_PAT_RE.sub(REDACTED, text)
        text = _TELEGRAM_TOKEN_RE.sub(REDACTED, text)
        text = _HEX_RE.sub(
            lambda m: REDACTED if len(m.group(0)) >= 32 else m.group(0), text
        )
        text = _BASE64ISH_RE.sub(
            lambda m: REDACTED if len(m.group(0)) >= 24 else m.group(0), text
        )
        return text


def log_path_for_today(host: Host | None = None) -> Path:
    """Return today's log file path, ``logs/log_YYYY-MM-DD.txt``.

    The date is the host's local date (:meth:`Host.now` is UTC-aware; this
    module only uses it for the calendar date, which is what "one file per
    day" means operationally — the session itself reasons in ET for trading
    dates, but the log file is named by wall-clock day on the device).
    """
    active_host = host or get_host()
    today = active_host.now().date().isoformat()
    return active_host.logs_dir() / f"log_{today}.txt"


def _prune_old_logs(directory: Path, today: date, keep_days: int = 90) -> None:
    """Remove only dated daily logs strictly older than the retention window."""
    logger = logging.getLogger(__name__)
    cutoff = today - timedelta(days=keep_days)
    try:
        for path in directory.iterdir():
            if not re.fullmatch(r"log_\d{4}-\d{2}-\d{2}\.txt", path.name):
                continue
            try:
                log_date = date.fromisoformat(path.name[4:-4])
            except ValueError:
                continue
            if log_date < cutoff:
                try:
                    path.unlink()
                except OSError as exc:
                    logger.warning("Could not prune daily log %s: %s", path.name, exc)
    except OSError as exc:
        logger.warning("Could not scan daily logs for retention: %s", exc)


def configure_logging(
    level: int = logging.INFO,
    host: Host | None = None,
    extra_handlers: Iterable[logging.Handler] = (),
) -> logging.Logger:
    """Configure the root ``nightwatch`` logger: one file per day + console.

    Idempotent-ish: existing handlers on the ``nightwatch`` logger are removed
    first, so calling this again (e.g. because the wall-clock day rolled over
    during a long-lived process) cleanly points logging at the new day's file
    instead of stacking handlers.

    Both the file and console handlers get the redaction filter — belt and
    braces, since a handler-level filter cannot be bypassed by routing through
    a different logger name.

    Third-party loggers (``httpx``, a stray ``logging.basicConfig()``
    somewhere in a dependency, etc.) never go through the ``nightwatch``
    logger's handlers — they propagate to the ROOT logger instead. To make
    redaction unbypassable regardless of which logger a record travels
    through, this also:

    * attaches the same :class:`RedactionFilter` instance to every handler
      already present on the root logger (so a handler some other library
      installed still gets scrubbed);
    * attaches a redacted file handler pointed at the *same* daily
      ``logs/log_YYYY-MM-DD.txt`` file to the root logger, so third-party
      records land in the same log, redacted on the way in.

    ``nightwatch.propagate`` stays ``False`` so nightwatch's own records are
    not duplicated onto the root handlers as well.

    Reconfiguring is safe to call repeatedly: only handlers this module
    previously attached to root (tagged with ``_OWNED_HANDLER_MARKER``) are
    removed and replaced; handlers owned by other code are left in place
    (and simply get the filter (re)applied).
    """
    logger = logging.getLogger("nightwatch")
    logger.setLevel(level)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    redaction = RedactionFilter()

    log_path = log_path_for_today(host)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redaction)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(stream=sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(redaction)
    logger.addHandler(console_handler)

    for handler in extra_handlers:
        handler.addFilter(redaction)
        logger.addHandler(handler)

    logger.propagate = False

    # --- Harden the ROOT logger so records from any other logger name are
    # redacted too, no matter which handler ends up printing them. ---
    root_logger = logging.getLogger()

    # Remove only the root handlers *we* attached on a previous call; leave
    # anything else (app code, a library, the test harness) untouched.
    for handler in list(root_logger.handlers):
        if getattr(handler, _OWNED_HANDLER_MARKER, False):
            root_logger.removeHandler(handler)
            handler.close()

    # A dedicated file handler on root, writing to the same daily log file,
    # so third-party records are captured (and redacted) too.
    root_file_handler = logging.FileHandler(log_path, encoding="utf-8")
    root_file_handler.setFormatter(formatter)
    root_file_handler.addFilter(redaction)
    setattr(root_file_handler, _OWNED_HANDLER_MARKER, True)
    root_logger.addHandler(root_file_handler)

    if root_logger.level == logging.NOTSET or root_logger.level > level:
        root_logger.setLevel(level)

    # Belt and braces, not the control itself: quiet down known-noisy HTTP
    # client libraries so they don't spam DEBUG/INFO request lines. See the
    # comment on `_NOISY_HTTP_LOGGERS` above — this can be overridden by
    # other config, unlike the filter, which cannot.
    for name in _NOISY_HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    # Apply (or re-apply) the redaction filter to every handler currently on
    # root, including ones we don't own (e.g. one attached by other code
    # before configure_logging() ran, or a fresh one from a library that
    # calls logging.basicConfig() late).
    harden_third_party_logging()
    _prune_old_logs(log_path.parent, date.fromisoformat(log_path.name[4:-4]))

    return logger


def harden_third_party_logging() -> None:
    """(Re)apply the redaction filter to every handler on the ROOT logger.

    Call this again after anything that might install its own root handler
    late (a library calling ``logging.basicConfig()``, a dependency that
    configures logging on first use, etc.) to make sure that handler is
    scrubbed too. Safe to call any number of times: adding the same
    ``RedactionFilter`` *instance* to a handler more than once would let a
    record pass through ``filter()`` twice, so we only add it if a
    :class:`RedactionFilter` isn't already on that handler.
    """
    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        if not any(isinstance(f, RedactionFilter) for f in handler.filters):
            handler.addFilter(RedactionFilter())
