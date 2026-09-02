"""Order execution with crash-recovery idempotency (DESIGN §4.3, §5). Built
in step 6.

# TODO(step 6): Implement execute(intent) -> Order:
#   1. db.repo.Repo.insert_intent(intent)  -- COMMITTED before any HTTP call.
#      request_id is the x-request-id UUID AND eToro's referenceId.
#   2. Check db.repo.Repo.get_order_by_reference_id(request_id) FIRST -- if a
#      prior crashed run already submitted this exact intent, recover instead
#      of resubmitting (this is the whole point of the idempotency anchor).
#   3. Otherwise call etoro.client: open (buy) or close (full close only, one
#      lot == one position, UnitsToDeduct=null) per DESIGN §4.3.
#   4. Poll GET orders:lookup to a terminal status (3 Filled, 4 Rejected,
#      5 PartiallyFilled) or handle 11 WaitingForMarket by continuing to poll.
#   5. On fill: db.repo.Repo.insert_lot / close_lot, set_lot_position_id.
# Also expose recover_pending() -> list[Order], used at startup to resume any
# intents left in 'pending'/'sent' status by db.repo.Repo.pending_intents() --
# this is the exact crash-recovery scenario DESIGN §9 criterion 5 tests.
"""
