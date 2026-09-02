"""Broker-vs-database position reconciliation (DESIGN §1.4 fix #3, §5 gate 11).
Built in step 6.

# TODO(step 6): Implement reconcile() -> list[Discrepancy] comparing eToro's
# live open positions against db.repo.Repo.open_lots():
#   - A broker position with no matching lot (by position_id, via
#     db.repo.Repo.get_lot_by_position_id) is a discrepancy -- NEVER fabricate
#     a lot for it (that was bug #3 in the original backtest code: it loses
#     book/vintage/ATH metadata). Surface it as a preflight gate 11 failure.
#   - A lot marked open with no matching broker position is also a
#     discrepancy (e.g. a close filled but was never recorded).
# preflight.py gate 11 calls this and fails loudly on any non-empty result --
# this module does not decide policy, only detects and reports.
"""
