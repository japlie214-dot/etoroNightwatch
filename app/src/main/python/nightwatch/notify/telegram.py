"""Outbound Telegram messages (DESIGN §7.1). Built in step 7.

# TODO(step 7): Implement send(event, body, session_id=None, trading_date=None):
#   1. db.repo.Repo.insert_notification() BEFORE the HTTP call (so a failed
#      send is itself visible in the notifications table, DESIGN §7.1).
#   2. POST to the Telegram Bot API sendMessage, using
#      config.TelegramConfig.bot_token / allowed_chat_id.
#   3. db.repo.Repo.set_notification_status('sent'|'failed', error=...).
# Cover every event named in DESIGN §7.1: session_start, preflight (pass or
# named failing gate), decision, each fill, session_end, abort/exception, and
# the daily heartbeat (send_heartbeat, even on no-trade days -- "silence is
# unambiguously a failure"). A Telegram outage must not fail the session --
# catch and record, never raise past this module on a send failure.
"""
