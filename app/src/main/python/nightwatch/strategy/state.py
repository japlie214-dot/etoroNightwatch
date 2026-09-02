"""Bridge between db.repo.StrategyState and the live strategy instance
(DESIGN §1.4). Built in step 4.

# TODO(step 4): Implement load()/save() around db.repo.Repo.get_strategy_state
# / save_strategy_state, and the "warmup trap" fix: `bars` MUST be persisted
# and restored every session so a freshly constructed strategy instance does
# not restart at 1 and sit in warmup forever (DESIGN §1.4). warmup_bars is 0
# in live config, so the indicator's own None already gates trading — this
# module's job is only to make `_bars` durable across restarts, not to
# reimplement the gate.
"""
