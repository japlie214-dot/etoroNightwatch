"""The twelve-step daily session sequence (DESIGN §4.1). Built in step 6.

# TODO(step 6): Implement run_session(trading_date) orchestrating, in order:
#   [1]  preflight.run_all()                       -> abort on any gate fail
#   [2]  market.bars.settle_previous() for SPY/SCHG/UPRO
#   [3]  sleep until exactly T-10min (market.calendar, honoring half-days)
#   [4]  market.bars.write_provisional() from live rates
#   [5]  strategy.cone.cone_5_z() over settled+provisional history
#   [6]  strategy.ladder.decide() -> OrderIntents, persisted via
#        db.repo.Repo.insert_decision
#   [7]  arm gate: config.is_armed AND NOT halt -> else log + Telegram
#        "would have done X" and stop cleanly (still a successful session)
#   [8]  executor.execute() per intent: insert_intent BEFORE the HTTP call
#        (the idempotency anchor, DESIGN §5), then POST, then poll to terminal
#   [9]  persist fills; update lots/buckets/strategy_state; snapshot portfolio
#   [10] notify.telegram report, or the daily heartbeat if nothing happened
#   [11] reschedule the next run BEFORE releasing anything — a crash here must
#        not orphan the alarm
#   [12] release the wake lock / stop the foreground service (this call
#        returns; host.py's Android bridge does the actual release)
# One session per trading_date is enforced by db.repo.Repo.create_session's
# UNIQUE constraint (DESIGN §3) — check get_session_by_date() first so a
# double-fire from the safety-net alarm is a clean no-op, not an exception.
"""
