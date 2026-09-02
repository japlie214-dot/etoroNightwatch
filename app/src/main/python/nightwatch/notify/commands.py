"""Inbound command handling: allowlist, replay rejection, dispatch
(DESIGN §7.2). Built in step 7.

# TODO(step 7): Implement handle(channel, sender_id, chat_id, raw_command,
# counter) -> db.repo.Command:
#   1. Reject anything from a chat id other than
#      config.TelegramConfig.allowed_chat_id -- log via
#      db.repo.Repo.insert_command(verdict='rejected', reason='unauthorized'),
#      never reply (DESIGN §7.2 "Any message from any other chat id is
#      ignored and logged, never answered").
#   2. Reject a replay: db.repo.Repo.is_replay(channel, sender_id, counter) ->
#      insert_command(verdict='rejected', reason='replay').
#   3. Parse the command as DATA against the fixed table in DESIGN §7.2
#      (/status, /positions, /pnl, /today, /logs, /next, /halt, /resume) --
#      nothing from the message is ever passed to a shell, a file path, or an
#      eval. Anything else is rejected, reason='unknown_command'.
#   4. On success, insert_command(verdict='accepted') and dispatch:
#        /halt, /resume  -> write/remove host.halt_flag_path()
#        everything else -> a read-only report assembled from db.repo reads
#      Explicitly NOT available, ever: config changes, arming live mode,
#      manual trades, budget changes (DESIGN §7.2) -- a compromised Telegram
#      account must never be able to move money.
"""
