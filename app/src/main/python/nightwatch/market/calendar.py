"""US market calendar: holidays, early closes, and the next-fire computation
(DESIGN §1.3, §6.2). Built in step 3.

# TODO(step 3): Implement, all reasoning in zoneinfo("America/New_York"):
#   - is_trading_day(date) -> bool                 (weekends + NYSE holidays)
#   - session_close(date) -> time                  (16:00 ET normally, 13:00 ET
#     on documented early-close days)
#   - next_close_minus(minutes, after=None) -> datetime
#     The function AlarmScheduler.kt calls through host.py: "the next US
#     session's close minus N minutes, in America/New_York, converted by the
#     caller to device local time" (DESIGN §6.2). Must honor half-days.
#   - A maintained (or programmatically derived) NYSE holiday + early-close
#     table; do not hardcode a single year.
# No DST arithmetic by hand anywhere (DESIGN §1.3) — zoneinfo does it, and
# Android's APEX tzdata module keeps rules current independently of the OS.
"""
