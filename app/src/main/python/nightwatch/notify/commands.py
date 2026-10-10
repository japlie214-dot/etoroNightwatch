"""Inbound command handling: allowlist, HMAC auth, replay rejection, dispatch
(DESIGN §7.2, §8.2).

Supported commands are **read-only plus HALT/RESUME only** — nothing here can
change config, arm live mode, place a trade, or change the budget. The
rationale is one sentence from the design doc: *"a compromised Telegram
account must never be able to move money."*

**Commands are DATA, never executed as shell or eval.** A raw inbound string
is reduced to a bare word and matched, verbatim, against the fixed
:data:`SUPPORTED_COMMANDS` set below. There is no code path from message
content to ``subprocess``, ``os.system``, ``eval``/``exec``, or a filesystem
path built from message content.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import datetime

from ..cli import clear_halt
from ..config import Config, MissingCredentialError
from ..db.repo import Command, Repo
from ..engine.halt import read_halt, write_halt
from ..host import Host
from ..logging_setup import log_path_for_today

__all__ = [
    "SUPPORTED_COMMANDS",
    "CommandResult",
    "compute_hmac",
    "verify_hmac",
    "normalize_command",
    "handle",
]

_logger = logging.getLogger("nightwatch.notify.commands")

#: The fixed, read-only-plus-halt command table (DESIGN §7.2/§8.2).
#: Explicitly NOT here, ever: config changes, arming live mode, manual
#: trades, budget changes.
SUPPORTED_COMMANDS = frozenset(
    {"status", "positions", "pnl", "today", "logs", "next", "halt", "resume", "help"}
)

#: How many trailing lines of the current day's log file `/logs` returns.
_LOG_TAIL_LINES = 40


@dataclass(frozen=True, slots=True)
class CommandResult:
    """The outcome of handling one inbound command."""

    command: Command
    reply: str | None

    @property
    def accepted(self) -> bool:
        """True if the command passed every check and was dispatched."""
        return self.command.verdict == "accepted"


def normalize_command(raw: str) -> str:
    """Reduce a raw inbound string to its bare command word.

    Strips a leading ``/``, lowercases, and drops everything after the first
    whitespace, keeping the command surface a fixed set of literal words.
    """
    text = raw.strip()
    if text.startswith("/"):
        text = text[1:]
    if not text:
        return ""
    return text.split(maxsplit=1)[0].lower()


def _args(raw_command: str) -> str:
    words = raw_command.split(maxsplit=1)
    return words[1].strip() if len(words) == 2 else ""


def compute_hmac(command: str, counter: int, secret: str) -> str:
    """HMAC-SHA256 over ``"{command}:{counter}"`` (DESIGN §7.2, §8.2)."""
    message = f"{command}:{counter}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_hmac(command: str, counter: int, provided: str, secret: str) -> bool:
    """Constant-time HMAC verification — never compare with ``==``."""
    expected = compute_hmac(command, counter, secret)
    return hmac.compare_digest(expected, provided)


def handle(
    repo: Repo,
    host: Host,
    config: Config,
    *,
    channel: str,
    sender_id: str,
    raw_command: str,
    chat_id: str | None = None,
    counter: int | None = None,
    provided_hmac: str | None = None,
    message_ts: datetime | None = None,
) -> CommandResult:
    """Authorise, authenticate and (if accepted) dispatch one inbound command.

    Every attempt — accepted or rejected, and why — is written to the
    ``commands`` audit table before this function returns (DESIGN §3, §7.2):
    an inbound control channel must be auditable regardless of outcome.

    Args:
        repo: Open database handle.
        host: Host implementation (for the HALT flag path and log files).
        config: Loaded config (for the chat-id allowlist and the command
            HMAC secret).
        channel: ``"fcm"`` or ``"telegram"``.
        sender_id: The channel's own principal id (Telegram chat id, or the
            FCM sending principal). Used as the replay-counter namespace.
        raw_command: The literal inbound text, e.g. ``"/halt"``.
        chat_id: The Telegram chat id the message arrived on, when
            ``channel == "telegram"``. Checked against the allowlist.
        counter: The monotonic HMAC replay counter carried by the message.
        provided_hmac: The HMAC the sender attached, to be checked against
            :func:`compute_hmac` over the normalized command and counter.

    Returns:
        A :class:`CommandResult` — always, whether accepted or rejected.
    """
    command_word = normalize_command(raw_command)

    def _reject(reason: str) -> CommandResult:
        row_id = repo.insert_command(
            Command(
                channel=channel, sender_id=sender_id, chat_id=chat_id,
                command=raw_command, counter=counter, verdict="rejected",
                reason=reason,
            )
        )
        _logger.warning(
            "rejected inbound command: channel=%s sender=%s reason=%s",
            channel, sender_id, reason,
        )
        row = repo.get_command(row_id)
        assert row is not None  # pragma: no cover - just inserted it
        return CommandResult(command=row, reply=None)

    # -- 1. chat-id allowlist -------------------------------------------------
    # Any message from any chat id other than the configured one is ignored
    # and logged, never answered (DESIGN §7.2). FCM's equivalent binding is
    # the app's own registration token, enforced at the transport layer
    # before this function is ever called.
    if channel == "telegram":
        try:
            allowed_chat_id = config.telegram.allowed_chat_id
        except MissingCredentialError:
            return _reject("telegram_not_configured")
        if str(chat_id) != str(allowed_chat_id):
            return _reject("unauthorized")

    # Telegram guarantees chat identity; a typed message cannot carry an HMAC
    # (boss decision 2026-10-10). FCM keeps HMAC and counter replay protection.
    if channel != "telegram":
        if counter is None or not provided_hmac:
            return _reject("missing_hmac")
        try:
            secret = config.telegram.command_secret
        except MissingCredentialError:
            return _reject("command_secret_not_configured")
        if not verify_hmac(command_word, counter, provided_hmac, secret):
            return _reject("bad_hmac")
        if repo.is_replay(channel, sender_id, counter):
            return _reject("replay")

    # -- 4. the command must be in the fixed table -----------------------------
    if command_word not in SUPPORTED_COMMANDS:
        return _reject("unknown_command")

    # -- accepted ----------------------------------------------------------------
    args = _args(raw_command)
    row_id = repo.insert_command(
        Command(
            channel=channel, sender_id=sender_id, chat_id=chat_id,
            command=raw_command, counter=counter, verdict="accepted",
            reason=(args or None) if command_word in {"halt", "resume"} else None,
        )
    )
    command_row = repo.get_command(row_id)
    assert command_row is not None  # pragma: no cover - just inserted it
    reply = _dispatch(command_word, repo, host, config, args=args, message_ts=message_ts)
    return CommandResult(command=command_row, reply=reply)


def _dispatch(
    command_word: str, repo: Repo, host: Host, config: Config,
    *, args: str = "", message_ts: datetime | None = None,
) -> str:
    """Turn an accepted command word into its reply text.

    ``command_word`` has already been validated against
    :data:`SUPPORTED_COMMANDS` by :func:`handle` — it is compared here
    against literal Python string constants, never used to build a shell
    command, a file path, or passed to ``eval``.
    """
    if command_word == "halt":
        write_halt(host, reason=args or "halted via Telegram", source="telegram")
        return "HALT set. Preflight gate 1 will abort every future session until /resume."
    if command_word == "resume":
        if not args:
            return "Usage: /resume <reason>. HALT unchanged."
        h = read_halt(host)
        if h is None:
            return "HALT was not set."
        if (h["set_at"] is not None and message_ts is not None
                and message_ts <= datetime.fromisoformat(h["set_at"])):
            return (f"Ignored: this /resume was sent before the current HALT was set ({h['set_at']}). "
                    "Send /resume again.")
        clear_halt(host)
        return f"HALT cleared (reason: {args}). Previous HALT: {h['reason']} (source {h['source']})."
    if command_word == "status":
        return _status_reply(repo, host, config)
    if command_word == "positions":
        return _positions_reply(repo)
    if command_word == "pnl":
        return _pnl_reply(repo)
    if command_word == "today":
        return _today_reply(repo)
    if command_word == "logs":
        return _logs_reply(host)
    if command_word == "next":
        return _next_reply(repo)
    if command_word == "help":
        return "Supported commands: " + ", ".join(f"/{c}" for c in sorted(SUPPORTED_COMMANDS))
    raise AssertionError(  # pragma: no cover - unreachable, guarded by handle()
        f"unhandled command {command_word!r}; not in SUPPORTED_COMMANDS?"
    )


def _status_reply(repo: Repo, host: Host, config: Config) -> str:
    halted = host.halt_flag_path().exists()
    latest = repo.latest_session()
    lines = [config.describe(), f"halt: {'HALTED' if halted else 'not halted'}"]
    if latest is None:
        lines.append("latest session: none")
    else:
        lines.append(
            f"latest session: {latest.trading_date} state={latest.state} "
            f"verdict={latest.preflight_verdict} outcome={latest.outcome!r}"
        )
    return "\n".join(lines)


def _positions_reply(repo: Repo) -> str:
    lots = repo.open_lots()
    if not lots:
        return "no open positions"
    return "\n".join(
        f"{lot.symbol} [{lot.book}] {lot.units} units @ {lot.entry_price} "
        f"(entered {lot.entry_date})"
        for lot in lots
    )


def _pnl_reply(repo: Repo) -> str:
    snapshot = repo.latest_snapshot()
    if snapshot is None:
        return "no portfolio snapshot yet"
    return (
        f"equity={snapshot.equity} cash={snapshot.cash} "
        f"unrealized_pnl={snapshot.unrealized_pnl} realized_pnl={snapshot.realized_pnl} "
        f"(as of {snapshot.taken_at})"
    )


def _today_reply(repo: Repo) -> str:
    latest = repo.latest_session()
    if latest is None:
        return "no session yet"
    decision = repo.get_decision(latest.id)
    if decision is None:
        return f"{latest.trading_date}: no decision recorded"
    return (
        f"{decision.trading_date}: cone_z={decision.cone_z} tier={decision.tier} "
        f"intents={decision.intents or 'none'} reason={decision.reason}"
    )


def _logs_reply(host: Host) -> str:
    path = log_path_for_today(host)
    if not path.exists():
        return f"no log file yet ({path.name})"
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = lines[-_LOG_TAIL_LINES:]
    return "\n".join(tail) if tail else "(empty log)"


def _next_reply(repo: Repo) -> str:
    latest = repo.latest_session()
    if latest is None or latest.next_run_at is None:
        return "no scheduled run known yet"
    return f"next run: {latest.next_run_at}"
