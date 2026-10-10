"""Pull and apply pending Telegram commands, then acknowledge the batch."""

from datetime import datetime, timezone
import logging

from ..config import Config
from ..db.repo import Repo
from ..host import Host
from .commands import CommandResult, handle
from .telegram import TelegramNotifier


_logger = logging.getLogger(__name__)


def process_pending(
    notifier: TelegramNotifier, repo: Repo, host: Host, config: Config,
) -> list[CommandResult]:
    updates = notifier.get_updates()
    if not updates:
        return []
    updates = sorted(updates, key=lambda update: update["update_id"])
    results = []
    for update in updates:
        try:
            message = update.get("message", {})
            text = message.get("text", "")
            if not text.startswith("/"):
                continue
            ts = datetime.fromtimestamp(message["date"], timezone.utc)
            result = handle(
                repo, host, config, channel="telegram",
                sender_id=str(message["from"]["id"]) if "from" in message else "",
                raw_command=text, chat_id=str(message["chat"]["id"]), message_ts=ts,
            )
            results.append(result)
            if result.reply is not None:
                notifier.send(result.reply, event="command_reply")
        except Exception as exc:
            _logger.warning("Telegram update %s processing failed: %s", update["update_id"], exc)
    notifier.get_updates(offset=updates[-1]["update_id"] + 1)
    return results
