> **DRAFT — for operator review before sending to eToro support.** Content below is assembled from
> internal engineering documentation (`DESIGN_market_data.md` §2.9, §2.11, §10.6 and
> `board nightwatch/handoff-2026-09-04-market-data.md` §4). All timestamps, values, and identifiers are quoted
> verbatim from those sources; anything the operator must supply is marked with a placeholder.
> Please fill in the placeholders and confirm the account/contact details before sending.

# Bug report: `GET /api/v1/market-data/instruments/rates` serves a stale, frozen quote for a tradable instrument during live market hours

## Summary

On 2026-09-04, during US market hours, the public rates endpoint
`GET /api/v1/market-data/instruments/rates` served instrument **UPRO** (instrument ID **3244**) a
quote timestamped **2026-09-03T19:59:54.8409896Z** with `lastExecution = 153.67` — the *prior
session's closing price* — across five or more polls spanning roughly 40 minutes, on **both a
demo and a live account**. The `date` and `lastExecution` fields were byte-identical on every
poll. In the same batched requests, **SPY** (ID 3000) and **SCHG** (ID 1084) advanced their
timestamps and prices normally on every poll. At the same time, eToro's own mobile app displayed
the correct live price for UPRO, and eToro's own `history/candles` endpoint (all granularities
down to `OneMinute`) returned correct, current data for the same instrument on the same account.
Instrument eligibility was independently confirmed: `allowOpenPosition: true` for UPRO on both
accounts. This rules out an eligibility/entitlement explanation and confines the defect to the
rates endpoint specifically.

## Affected endpoint, instrument, and scope

- **Endpoint:** `GET /api/v1/market-data/instruments/rates`
- **Instrument affected:** UPRO, instrument ID **3244**
- **Instruments unaffected in the same batch request:** SPY (ID 3000), SCHG (ID 1084) — both
  ticked forward normally on every poll
- **Environments affected:** both demo and live accounts, observed simultaneously in the same
  session
- **Not affected:** `GET .../history/candles` for the same instrument, same account, same time
  window, at every granularity tested (`OneMinute`, `FiveMinutes`, `TenMinutes`, `FifteenMinutes`,
  `ThirtyMinutes`, `OneHour`, `FourHours`)
- **Account IDs:** `[DEMO ACCOUNT ID — operator to fill]`, `[LIVE ACCOUNT ID — operator to fill]`
- **API key / user key used for the probe:** `[API KEY / USER KEY — operator to fill; not
  recorded in source docs and must not be pasted into this file in plain text]`

## Reproduction steps

1. Authenticate against the public API with read-only market-data scope (search/rates/candles).
2. Resolve instrument IDs for SPY, UPRO, and SCHG (3000, 3244, 1084 respectively, as of
   2026-09-04).
3. Call `GET /api/v1/market-data/instruments/rates` for all three instrument IDs in a single
   batched request, during US market hours.
4. Repeat the same batched request at least 3–5 times, spaced several seconds to tens of minutes
   apart.
5. Compare the `date` and `lastExecution` fields per instrument across polls.
6. In parallel, call `GET .../history/candles` for UPRO at `OneMinute` granularity and compare
   against the rates response for the same instrument at the same wall-clock time.
7. Independently confirm the instrument is tradable for the account, e.g. via
   `POST /api/v2/trading/info/demo/eligibility` for the same instrument IDs.

This was performed on 2026-09-04 starting ~10:58 ET (US market open) via device-side `curl`
(credentials supplied over stdin, not argv or disk) against the demo account, and repeated on the
live account at 11:23–11:24 ET.

## Observed vs. expected

**Observed (demo account, two polls ~7s apart, ~10:58 ET):**

| Symbol | Poll | `lastExecution` | bid/ask | raw `date` | age vs. wall clock |
|---|---|---|---|---|---|
| SPY | 1 | 769.62 | 769.62 / 769.64 | 2026-09-04T14:58:25.65Z | ~0s |
| SPY | 2 | 769.64 | 769.64 / 769.66 | 2026-09-04T14:58:32.71Z | ~0s |
| UPRO | 1 | 153.67 | 153.67 / 153.71 | 2026-09-03T19:59:54.84Z | ~68,311s (~19.0h) |
| UPRO | 2 | 153.67 | 153.67 / 153.71 | 2026-09-03T19:59:54.84Z | ~68,318s (~19.0h) |
| SCHG | 1 | 35.5 | 35.5 / 35.51 | 2026-09-04T14:58:25.37Z | ~0s |
| SCHG | 2 | 35.5 | 35.5 / 35.51 | 2026-09-04T14:58:32.97Z | ~0s |

Three further polls at 10:58, 11:11, and 11:15 ET all returned UPRO `date =
2026-09-03T19:59:54.8409896Z`, byte-identical down to the sub-second digits, `lastExecution =
153.67`, while SPY and SCHG ticked forward to the second in the same batch request each time.

**Observed (live account, two polls 65s apart, 11:23–11:24 ET):** both polls returned UPRO
`date = 2026-09-03T19:59:54.8409896Z`, `lastExecution = 153.67` — bit-for-bit identical to each
other and to the demo readings above. SPY and SCHG advanced second-by-second in the same requests.

**Expected:** `date` and `lastExecution` for UPRO should advance during market hours the same way
they do for SPY and SCHG in the same request, and should reflect a current, tradable price rather
than the prior session's close.

**Quantified error**, cross-checked against an independent market-data vendor (values as recorded
in our internal documentation; the vendor is not eToro and is not named here):

| Quantity | Value |
|---|---|
| UPRO 2026-09-03 official close (independent vendor) | 153.73 |
| eToro's frozen "live" quote | 153.67 (−0.039% vs. the prior close — i.e. it *is* yesterday's close) |
| UPRO true price at time of probe (independent vendor) | 151.665 |
| **eToro quote vs. true current price** | **+1.322% (+$2.00/share)** |
| SPY same window (for reference) | 773.17 → 769.89 (−0.424%) |

The +1.322% pricing error and the leverage-scaled index move (SPY −0.424% × 3 ≈ −1.273%, opposite
sign because the frozen quote is stale-high on a down day) are consistent with each other to
within 0.05%, i.e. the served quote is exactly yesterday's close, not a randomly wrong number.

## Evidence that the defect is confined to the rates endpoint

Three independent, contemporaneous sources agreed with each other and disagreed with the rates
endpoint:

1. **eToro's own mobile app** (screenshots taken 11:28–11:29 ET, four minutes after the live API
   poll) displayed UPRO at **151.43 (−1.50%)**, with the position group correctly valued at
   $4,038.98 for a 26.672246-unit holding (26.67225 × 151.43 = $4,038.96) — an internally
   consistent, current price, not the frozen 153.67.
2. **eToro's own `history/candles` endpoint**, probed ~11:32 ET the same session, returned UPRO's
   current daily candle as `O=152.96 H=153.43 L=151.15 C=151.19` — current and correct — while in
   the same run the rates endpoint for the same instrument, same account, same moment still
   returned `lastExecution=153.67`, `date=2026-09-03T19:59:54Z`. All seven intraday granularities
   tested (`OneMinute` through `FourHours`) returned HTTP 200 with current data; `OneMinute`'s
   latest bar closed at 151.19 at 11:32:00 EDT. SPY and SCHG ticked live on `OneMinute` in the
   same run. Separately, UPRO's prior-day candle close was exactly 153.67 — the same number the
   rates endpoint is frozen on, confirming 153.67 is genuinely the prior close and not an
   unrelated stale value.
3. **An independent third-party market-data vendor** put UPRO at 151.665 (and, at a slightly
   different timestamp, 151.295) at the same time — consistent with the app and the candles
   endpoint, not with the rates endpoint.

Because the app and the candles endpoint (both eToro's own systems) show the correct price for the
same instrument, same account, same instant that the rates endpoint does not, this is not a case
of "the price is unknown to eToro." It narrows to the rates endpoint specifically.

**Eligibility was separately checked and ruled out as an explanation.** `POST
/api/v2/trading/info/demo/eligibility`, called for SPY, UPRO, and SCHG in one request on
2026-09-04, returned `allowOpenPosition: true` for all three, with UPRO's record structurally
identical to SPY's and SCHG's (`minPositionExposure: 10.0`, `maxUnitsPerOrder: 13421`,
`allowClosePosition` / `allowMitOrders` / `allowEntryOrders` / `allowExitOrders` /
`allowTrailingStopLoss` all `true`, populated `leverageConfigs`). `notFoundInstrumentIds` and
`notFoundSymbols` were both empty. UPRO is fully tradable on this account; the frozen quote is not
a symptom of restricted or ineligible status.

## Impact

An automated client reading `lastExecution` from the rates endpoint has no price-based way to
detect this condition: 153.67 is a plausible UPRO price, it breaches no sanity bound, and the
accompanying bid/ask are frozen at the same stale values, so a bid/ask-midpoint fallback is
equally stale. Only the `date` field, checked against wall-clock time, reveals the problem.

The error observed is **one-directionally adverse**: because it is the prior session's close, it
sits above the true price whenever the market has moved down since that close. On the day
measured, a mild −0.42% SPY move produced a **+1.322% (+$2.00/share)** UPRO pricing error via the
frozen quote — this figure is an **observation**, not a projection.

Our internal documentation additionally *projects* — clearly labelled there as projections, not
observations — that because the error scales with the leveraged instrument's day-over-day move,
a −3% SPY day would imply roughly a 9% UPRO pricing error via this mechanism, and a 2020-03-16-scale
(~−12% SPY) day would imply roughly 36%. We have not observed either of those larger scenarios
directly; they follow from applying the same day's-move × leverage relationship measured on
2026-09-04 to larger hypothetical market moves.

The defect was observed on both a demo account and a live account with an open position (7 UPRO
units observed, cost basis and entry price on file internally) simultaneously and identically,
consistent with a shared market-data layer feeding both rather than an account-specific artifact.

## Questions for eToro

1. Is this a known issue with `/api/v1/market-data/instruments/rates`, and if so, is there an
   existing incident or ticket reference we can track?
2. Is a fix expected, and on what rough timeline?
3. Is there a supported, documented way for API consumers to detect staleness on this endpoint
   (e.g. a reliable "quote age" or "market status" field) short of independently cross-checking
   against the candles endpoint or another data source?
4. Is the `date` field on the rates payload intended to be trustworthy as a freshness indicator?
   If so, why did it remain byte-identical (`2026-09-03T19:59:54.8409896Z`) across 5+ polls
   spanning ~40 minutes while sibling instruments in the same batched request advanced normally?
5. Is there any per-instrument mechanism (caching layer, feed subscription, symbol rotation) that
   could explain why the affected instrument changes over time rather than being a fixed,
   permanently-broken symbol?

## Supporting detail available on request

- Full raw request/response payloads for the polls described above (redacted of credentials).
- Screenshots of the eToro app referenced above.
- Additional cross-checks against a second, earlier-observed instance of the same class of
  staleness on a different instrument (SCHG) while the market was closed, if useful for pattern
  confirmation.

---

`[OPERATOR: please add — support ticket number once filed, preferred contact channel, and any
NDA/confidentiality caveats before sending]`
