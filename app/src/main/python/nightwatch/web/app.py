"""FastAPI application factory for the embedded dashboard (DESIGN §8). Built
in step 8.

# TODO(step 8): Implement create_app(config, repo_factory) -> FastAPI, bound
# to config.webapp.host / .port (must stay above 1024, enforced already in
# nightwatch.config.WebappConfig). Pages/JSON per DESIGN §8:
#   - Dashboard: equity, cash, available_cash (credit - sum(pending order
#     amounts), NOT totalBalance), unrealized P&L, budget/order_size/
#     cash_target/recyclable_cash, today's cone_z/drawdown/tier.
#   - Positions table, trade history (joined to lots so each trade shows WHY:
#     cone_z, tier, book, bucket id), chart (SPY + cone bands + z-score +
#     trade markers -- overlays are registered indicators, never
#     strategy-specific metadata, per the carried-over note), strategy state
#     (open lots by book, bucket tranche progress/expiry), sessions & logs,
#     host panel (battery, exemptions, canScheduleExactAlarms, next alarm,
#     last boot, vivo checklist last-verified date).
# Use the read-only eToro key (config.etoro.read_credentials) and throttle
# through etoro.ratelimit during the trading window so the dashboard cannot
# eat the session's API budget (DESIGN §8).
"""
