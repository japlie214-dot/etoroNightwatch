"""Outbound Telegram messages (DESIGN §8.1).

Two rules shape this module, both stated in DESIGN §8.1 nearly verbatim:

1. **A missing or broken Telegram config must never become a trading
   failure.** ``TelegramNotifier`` always constructs successfully. When
   ``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID`` are absent, or
   ``telegram.enabled = false``, it logs one warning and every :meth:`send`
   becomes a recorded no-op — never an exception. *"If Telegram is
   unreachable the session still completes; it does not become a trading
   failure."*
2. **Every send is auditable, including failed ones.** A ``notifications``
   row is written *before* the HTTP call and updated with the outcome after,
   so a failed alert is itself visible (DESIGN §3).

Every environment-carrying message is tagged unmistakably ``[DEMO]`` or
``[REAL]`` (DESIGN §6.F, safety group F) — never left ambiguous.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Sequence

import httpx

from ..config import TelegramConfig
from ..db.repo import Notification, Repo

__all__ = [
    "MESSAGE_LIMIT",
    "TelegramSendError",
    "TelegramNotifier",
    "split_message",
]

#: Telegram's documented hard cap on one ``sendMessage`` body (DESIGN §8.1,
#: §9.3).
MESSAGE_LIMIT: int = 4096

_API_ROOT = "https://api.telegram.org"

#: Bounded exponential backoff for transient failures (network error, 5xx).
#: 429 instead honours the server's own ``retry_after``.
_MAX_ATTEMPTS = 5
_BASE_DELAY_SECONDS = 1.0
_MAX_DELAY_SECONDS = 30.0

_logger = logging.getLogger("nightwatch.notify.telegram")


class TelegramSendError(Exception):
    """Raised when a send exhausts its retries or hits a permanent error.

    Always caught inside :class:`TelegramNotifier`; never escapes it. It
    exists so :meth:`TelegramNotifier._send_one` has one clean way to say
    "this attempt is over" to its caller.
    """


def _env_tag(environment: str) -> str:
    """``"REAL"`` or ``"DEMO"`` — unmistakable per DESIGN §6.F."""
    return "REAL" if environment.strip().lower() == "real" else "DEMO"


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split ``text`` into chunks that each respect Telegram's 4096-char cap.

    A message under the limit is returned unchanged, as a single-element
    list. A longer message is split into pieces and every piece beyond the
    first is marked ``" (i/n)"`` — the reader can always tell a message was
    split rather than silently truncated (DESIGN §8.1 "split or truncate
    with a clear marker").
    """
    if len(text) <= limit:
        return [text]
    # Reserve room for the largest plausible " (NN/NN)" suffix; 12 chars
    # comfortably covers up to 3-digit part counts.
    reserve = 12
    body_limit = limit - reserve
    raw_chunks: list[str] = []
    remaining = text
    while remaining:
        raw_chunks.append(remaining[:body_limit])
        remaining = remaining[body_limit:]
    total = len(raw_chunks)
    if total == 1:
        return raw_chunks
    return [f"{chunk} ({i}/{total})" for i, chunk in enumerate(raw_chunks, start=1)]


class TelegramNotifier:
    """Sends outbound Telegram alerts and records every attempt (DESIGN §8.1).

    Construction never touches the network and never raises: a missing
    ``TELEGRAM_BOT_TOKEN``/``TELEGRAM_CHAT_ID`` puts the notifier into a
    disabled, no-op mode with a single warning log line, rather than
    crashing a trading session that has nothing to do with Telegram.
    """

    def __init__(
        self,
        config: TelegramConfig,
        repo: Repo,
        *,
        environment: str = "demo",
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._repo = repo
        self._environment = environment
        self._client = client or httpx.Client(timeout=config.timeout_seconds)
        self._owns_client = client is None
        self._sleep = sleep

        if not self.enabled:
            _logger.warning(
                "Telegram notifier disabled (%s). Outbound alerts and inbound "
                "commands are recorded no-ops until config/.secrets.env has "
                "both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.",
                self.disabled_reason,
            )

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        """Close the underlying HTTP client, if this notifier owns one."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "TelegramNotifier":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def enabled(self) -> bool:
        """True only when ``telegram.enabled`` is set AND both secrets exist."""
        return self._config.enabled and self._config.is_configured

    @property
    def disabled_reason(self) -> str:
        """Human-readable reason :attr:`enabled` is False, for diagnostics."""
        if not self._config.enabled:
            return "telegram.enabled = false in config.toml"
        if not self._config.is_configured:
            return "TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID missing from config/.secrets.env"
        return "unknown"  # pragma: no cover - unreachable while enabled is False

    # -- core send path --------------------------------------------------------

    def send(
        self,
        text: str,
        *,
        parse_mode: str | None = None,
        event: str = "message",
        session_id: int | None = None,
        trading_date: str | None = None,
    ) -> Notification:
        """Send one message, splitting it if needed. Never raises.

        A ``notifications`` row is inserted with ``status='pending'`` before
        any HTTP call, then updated to ``'sent'``, ``'failed'`` or
        ``'skipped'`` (disabled mode) once the outcome is known — so a failed
        or skipped send is exactly as visible in the audit trail as a
        successful one (DESIGN §3).
        """
        chat_id = self._config.secrets.get("TELEGRAM_CHAT_ID") if self.enabled else None
        notification_id = self._repo.insert_notification(
            Notification(
                event=event, body=text, session_id=session_id,
                trading_date=trading_date, chat_id=chat_id, status="pending",
            )
        )

        if not self.enabled:
            self._repo.set_notification_status(
                notification_id, "skipped", error=self.disabled_reason
            )
            return self._require(self._repo.get_notification(notification_id))

        try:
            last_message_id: str | None = None
            for chunk in split_message(text):
                last_message_id = self._send_one(chunk, parse_mode=parse_mode)
        except TelegramSendError as exc:
            _logger.error("Telegram send failed for event %r: %s", event, exc)
            self._repo.set_notification_status(notification_id, "failed", error=str(exc))
            return self._require(self._repo.get_notification(notification_id))

        self._repo.set_notification_status(
            notification_id, "sent", telegram_message_id=last_message_id
        )
        return self._require(self._repo.get_notification(notification_id))

    @staticmethod
    def _require(notification: Notification | None) -> Notification:
        assert notification is not None  # pragma: no cover - just wrote it
        return notification

    def _send_one(self, text: str, *, parse_mode: str | None) -> str | None:
        """POST one chunk, retrying transient failures with bounded backoff.

        Retries: a network error, HTTP 429 (honouring ``retry_after``), and
        any 5xx. Any other 4xx is treated as permanent and raised
        immediately without retrying — this deliberately never retries a
        malformed request or bad credentials into a tighter rate-limit hole.
        """
        url = f"{_API_ROOT}/bot{self._config.bot_token}/sendMessage"
        payload: dict[str, Any] = {
            "chat_id": self._config.allowed_chat_id,
            "text": text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        last_error: Exception | str = "unknown error"
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = self._client.post(url, json=payload)
            except httpx.RequestError as exc:
                last_error = exc
                if attempt == _MAX_ATTEMPTS:
                    break
                self._sleep(self._backoff(attempt))
                continue

            if response.status_code == 200:
                return self._message_id(response)

            if response.status_code == 429:
                retry_after = self._retry_after(response)
                last_error = f"429 rate limited (retry_after={retry_after}s)"
                if attempt == _MAX_ATTEMPTS:
                    break
                self._sleep(retry_after)
                continue

            if 500 <= response.status_code < 600:
                last_error = f"{response.status_code} server error: {response.text}"
                if attempt == _MAX_ATTEMPTS:
                    break
                self._sleep(self._backoff(attempt))
                continue

            # Any other 4xx (bad token, bad chat id, malformed request, ...)
            # is permanent: never retried.
            raise TelegramSendError(
                f"{response.status_code} from Telegram sendMessage: {response.text}"
            )

        raise TelegramSendError(str(last_error))

    @staticmethod
    def _message_id(response: httpx.Response) -> str | None:
        try:
            data = response.json()
        except ValueError:
            return None
        result = data.get("result") if isinstance(data, dict) else None
        message_id = result.get("message_id") if isinstance(result, dict) else None
        return None if message_id is None else str(message_id)

    @staticmethod
    def _retry_after(response: httpx.Response) -> float:
        """Read Telegram's requested backoff, from the body or the header."""
        try:
            data = response.json()
            parameters = data.get("parameters", {}) if isinstance(data, dict) else {}
            retry_after = parameters.get("retry_after")
            if retry_after is not None:
                return float(retry_after)
        except ValueError:
            pass
        header = response.headers.get("Retry-After")
        if header:
            try:
                return float(header)
            except ValueError:
                pass
        return _BASE_DELAY_SECONDS

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(_BASE_DELAY_SECONDS * (2 ** (attempt - 1)), _MAX_DELAY_SECONDS)

    # -- inbound polling (DESIGN §8.2 tiers 2/3) ------------------------------

    def get_updates(
        self, offset: int | None = None, timeout: int = 0
    ) -> list[dict[str, Any]]:
        """Return pending updates via ``getUpdates``.

        This is the opportunistic/escalation-only inbound check (DESIGN
        §8.2): a single call piggybacked on an already-open wake, never a
        standing connection. Disabled mode and any transport error both
        return ``[]`` rather than raising — inbound polling failing must
        never fail the caller's session either.
        """
        if not self.enabled:
            return []
        url = f"{_API_ROOT}/bot{self._config.bot_token}/getUpdates"
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        try:
            response = self._client.get(url, params=params)
        except httpx.RequestError as exc:
            _logger.warning("Telegram getUpdates failed: %s", exc)
            return []
        if response.status_code != 200:
            _logger.warning("Telegram getUpdates returned HTTP %s", response.status_code)
            return []
        try:
            data = response.json()
        except ValueError:
            return []
        result = data.get("result") if isinstance(data, dict) else None
        return list(result) if isinstance(result, list) else []

    # -- typed outbound events (DESIGN §8.1) ----------------------------------
    #
    # Every event below is tagged [DEMO] or [REAL] and carries the trading
    # date, per DESIGN §6.F: "Every Telegram message ... states DEMO or REAL
    # unmistakably -- no ambiguity about which environment a number belongs
    # to."

    def session_start(
        self,
        *,
        trading_date: str,
        target_time: str,
        mode: str,
        session_id: int | None = None,
    ) -> Notification:
        """Step [1]: the run has started (DESIGN §4.1, §8.1)."""
        text = (
            f"[{_env_tag(self._environment)}] Session start — {trading_date}\n"
            f"target: {target_time}\n"
            f"mode: {mode}"
        )
        return self.send(
            text, event="session_start", session_id=session_id, trading_date=trading_date
        )

    def preflight_result(
        self,
        *,
        trading_date: str,
        passed: bool,
        gate: int | None = None,
        gate_name: str | None = None,
        reason: str | None = None,
        session_id: int | None = None,
    ) -> Notification:
        """Preflight pass, or the specific gate that failed and why (§5, §8.1)."""
        tag = _env_tag(self._environment)
        if passed:
            text = f"[{tag}] Preflight PASS — {trading_date}"
        else:
            text = (
                f"[{tag}] Preflight FAIL — {trading_date}\n"
                f"gate {gate} ({gate_name}): {reason}"
            )
        return self.send(
            text, event="preflight", session_id=session_id, trading_date=trading_date
        )

    def decision(
        self,
        *,
        trading_date: str,
        cone_z: float | None,
        spy_close: float | None,
        drawdown: float | None,
        tier: int | None,
        budget: float | None,
        cash: float | None,
        intents: Sequence[dict[str, Any]],
        reason: str | None = None,
        session_id: int | None = None,
    ) -> Notification:
        """``cone_z``, SPY, drawdown, tier, budget, cash and the intents or
        hold reason (DESIGN §8.1)."""
        tag = _env_tag(self._environment)
        lines = [
            f"[{tag}] Decision — {trading_date}",
            f"cone_z={cone_z} spy={spy_close} drawdown={drawdown} tier={tier}",
            f"budget={budget} cash={cash}",
        ]
        if intents:
            lines.append("intents:")
            lines.extend(f"  {intent}" for intent in intents)
        else:
            lines.append(f"hold: {reason or 'no intents'}")
        return self.send(
            "\n".join(lines), event="decision", session_id=session_id,
            trading_date=trading_date,
        )

    def fill(
        self,
        *,
        trading_date: str,
        symbol: str,
        side: str,
        amount_usd: float | None,
        units: float | None,
        price: float | None,
        order_id: str | None,
        session_id: int | None = None,
    ) -> Notification:
        """One fill: symbol, side, USD/units, price, order id (DESIGN §8.1)."""
        text = (
            f"[{_env_tag(self._environment)}] Fill — {trading_date}\n"
            f"{side} {symbol} amount=${amount_usd} units={units} "
            f"price={price} order_id={order_id}"
        )
        return self.send(text, event="fill", session_id=session_id, trading_date=trading_date)

    def session_end(
        self,
        *,
        trading_date: str,
        positions_summary: str,
        equity: float | None,
        cash: float | None,
        pnl: float | None,
        next_run_at: str | None,
        session_id: int | None = None,
    ) -> Notification:
        """Positions, equity, cash, P&L, next scheduled run (DESIGN §8.1)."""
        text = (
            f"[{_env_tag(self._environment)}] Session end — {trading_date}\n"
            f"positions: {positions_summary}\n"
            f"equity={equity} cash={cash} pnl={pnl}\n"
            f"next run: {next_run_at}"
        )
        return self.send(
            text, event="session_end", session_id=session_id, trading_date=trading_date
        )

    def abort(
        self,
        *,
        trading_date: str,
        reason: str,
        session_id: int | None = None,
    ) -> Notification:
        """Any abort or exception: full reason, device staying awake (§4.1, §8.1)."""
        text = (
            f"[{_env_tag(self._environment)}] ABORT — {trading_date}\n"
            f"{reason}\n"
            "device is staying awake for diagnosis"
        )
        return self.send(text, event="abort", session_id=session_id, trading_date=trading_date)

    def heartbeat(
        self,
        *,
        trading_date: str,
        next_run_at: str | None,
        message: str = "ran, nothing to do",
        session_id: int | None = None,
    ) -> Notification:
        """The daily heartbeat, sent even on no-trade days.

        DESIGN §8.1: *"Without it, a killed app and a quiet market look
        identical. With it, silence is unambiguously a failure."*
        """
        text = (
            f"[{_env_tag(self._environment)}] Heartbeat — {trading_date}\n"
            f"{message}\n"
            f"next run: {next_run_at}"
        )
        return self.send(
            text, event="heartbeat", session_id=session_id, trading_date=trading_date
        )
