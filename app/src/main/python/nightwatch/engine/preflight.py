"""The twelve preflight gates (DESIGN §5). Built in step 6.

# TODO(step 6): Implement each gate as a function returning a
# db.repo.PreflightCheck, run in order, ALL must pass:
#   1. Halt switch          - host.halt_flag_path() exists, or /halt was set
#   2. Power & permissions   - battery >=40% or charging; battery-optimization
#      exemption granted; AlarmManager.canScheduleExactAlarms(); wake lock
#      acquired (host.power_status())
#   3. Network               - DNS/HTTPS to public-api.etoro.com within 90s
#   4. Clock                 - local vs API Date header, <= 60s skew
#   5. Database              - opens; schema_version matches; no unfinished
#      prior session (db.repo.Repo.unfinished_sessions())
#   6. Auth                  - GET /balances == 200
#   7. Environment match     - config says real/demo and the key is bound the
#      same way, PROBED not trusted
#   8. Instruments           - UPRO/SCHG/SPY resolve; allowOpenPosition;
#      minPositionExposure <= order_size
#   9. Calendar              - is_trading_day(); resolves early closes
#  10. Price data            - >= 265 settled SPY bars; no still-provisional
#      bar; live rate present and non-zero
#  11. Reconciliation        - broker positions vs db lots agree (delegates to
#      engine/reconcile.py)
#  12. Risk caps             - session order count/notional vs config.risk
#
# Any failure: persist ALL attempted PreflightCheck rows via
# db.repo.Repo.record_preflight_check, send the Telegram abort message naming
# the gate, and — per DESIGN §5 — hold the wake lock rather than returning to
# Doze, so run_preflight()'s caller must not release on failure.
"""
