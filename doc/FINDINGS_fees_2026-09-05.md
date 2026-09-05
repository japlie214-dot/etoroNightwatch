# Findings — the fee question, settled on live data (2026-09-05)

Source: on-device probe run, real account, read-only. Raw payloads pulled to
`pulled_probe_output/` (gitignored — they contain live position ids and amounts).
All 120 closed trades plus all 227 open positions were examined, not a sample.

## 1. The recorded totals were honest

`Σ fees = -$380.40`, `Σ netProfit = +$327.25`, reproduced from the per-trade rows to
within $6e-14. The figures carried in DESIGN_market_data.md §10.13 are confirmed
against source data, not merely re-copied.

## 2. What the fees actually are

**A carrying fee proportional to size × time.** Correlation of `|fees|` against:

| against | Pearson r |
|---|---|
| `investment × holding_days` | **0.9918** |
| `investment` alone | 0.9157 |
| `holding_days` alone | -0.0611 |

Neither a commission (which would track `investment` alone) nor a flat charge. The
`investment`-alone correlation is an artifact of the largest positions also being the
longest held.

**Not dividends, and not an account-level charge.** A scan of all 120 payloads found
**no field** with a dividend- or account-charge-shaped name. Those hypotheses are
unsupported by this API surface.

**Not leverage.** All fee-bearing rows are `leverage = 1`, `isBuy = true`, and every
open position is `settlementTypeID = 1`. The audit's own `leveraged_or_short` bucket
holds 2 trades with $0.00 of fees; the other **118 trades carry the entire -$380.40**.
`overnightFee` in `get_costs` reads `$0.00` for both SCHG and UPRO, which is a
*quoted* rate and is contradicted by realised `totalFees` — do not trust it.

## 3. Per-instrument, annualised

| instrument | open $ | totalFees | implied %/yr |
|---|---|---|---|
| 1084 SCHG | 40,867.17 | **-59.17** | **0.320%** |
| 3244 UPRO | 4,000.00 | 0.00 | n/a (held ≤17d, too short to accrue) |
| 4375 (unidentified) | 5,000.00 | -126.01 | **3.328%** |

Instrument **4375** accounts for **-$309.61 of the -$380.40** — 81% of all historical
fees — across 8 closed trades at a strikingly consistent 2.9–3.8%/yr, and still has a
$5,000 position open. It is not a managed symbol. Identifying it was not possible
offline (only SCHG/UPRO instrument metadata was fetched); resolve it on the next
device run.

**SCHG is not fee-free.** 0.320%/yr is small but real, and SCHG is the long-hold core
book, so it compounds against the position the strategy is least likely to close.
At the current ~$45k that is roughly $145/yr, scaling with the book.

## 4. What this means for the app's accounting

The operator's proposed model is **confirmed correct, for a different reason than they
proposed**:

- *"A $100 order gives a $100 position — ignore the fee at entry."* **Correct.**
  Position `amount` is a clean, unreduced figure; no fee is baked into it. The
  operator's hypothesis that the fee was *hidden inside* the position amount is
  **wrong** — the fee does not exist at entry at all. It accrues afterwards, over
  time, and is visible separately as `totalFees` on the open position.
- *"On selling, don't predict proceeds — just read the cash balance."* **Correct, and
  now demonstrably necessary rather than merely convenient.** Because the fee accrues
  with holding time and is settled on close, proceeds cannot be derived from
  `openRate`/`closeRate`/`units` at all. Reading realised cash is the only correct
  method.

The `$0.15 transactionFee` that `get_costs` discloses for SCHG — the disclosure that
prompted this whole investigation — is a *quote*, and the observed data shows no
corresponding reduction in position `amount`. Whether it is ever charged against cash
separately is **not settled** by this run; it needs a live order to observe, which the
operator has deferred to deployment.

## 5. Follow-ups

1. Identify instrument 4375 and confirm whether its ~3.3%/yr is intended. Unrelated to
   Nightwatch, but it is real money.
2. Decide whether SCHG's 0.320%/yr carrying cost should appear in the app's P&L
   surface. It is currently modelled nowhere.
3. `overnightFee` from `get_costs` reads $0.00 while realised `totalFees` is non-zero.
   Do not use the quoted value as evidence of anything.
