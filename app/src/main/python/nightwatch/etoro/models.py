"""Typed models for eToro API requests/responses (DESIGN §1.1, §4.3).

# TODO(step 2): Dataclasses/TypedDicts for at least:
#   - OrderRequest (open):  action='open', transaction='buy'|'sellShort',
#     instrumentId, orderType='mkt', leverage, amount, orderCurrency='usd',
#     settlementType='real'  (DESIGN §4.3)
#   - CloseRequest: positionId path param + InstrumentId, UnitsToDeduct (null
#     for a full close — we always full-close, one lot == one position)
#   - OrderStatus enum: 3=Filled, 4=Rejected, 5=PartiallyFilled,
#     11=WaitingForMarket (DESIGN §1.1)
#   - Candle (OneDay bar), Rate (bid/ask/lastExecution), Eligibility
#     (minPositionExposure, minPositionAmount, allowOpenPosition,
#     allowPartialClose), Balance/Account, Position.
# Keep these framework-free (plain dataclasses) so they import on both desktop
# and Chaquopy with no extra dependency.
"""
