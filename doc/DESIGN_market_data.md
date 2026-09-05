# Market data integrity — design addendum

**Status:** design agreed; gate 10 items 1 and 3 built 2026-09-05 (§6), the rest not yet built.
Written 2026-09-04, substantially rewritten/extended 2026-09-05 (§4.7, §10.9–§10.13 in
particular — see HANDOFF_2026-09-05.md).
**Supersedes:** DESIGN.md §3 (`bars` table), §4.2 (the live-bar problem / `is_provisional`),
§6.G (bad-data safety), and specifies gates 9–10 of §5.
**Resolves:** the two High risks tracked in DESIGN.md §12 (unchecked rate staleness;
undetected missing days).

---

## 0. The decision in one line

**Stop accumulating price history on the phone. Fetch it fresh every session, use it, discard it.**

The phone keeps its curated SQLite for everything else — sessions, intents, orders,
`lot_annotations` (§4), buckets, strategy state, audit. It stores **no price series at all**.

### 0.1 Superseded figures — read this before trusting any dollar figure below

This document family has driven a wrong, estimated number into the design **twice** as if it were
measured (§10.11, §10.12) — a recorded failure mode, not a one-off. Its lesson: a superseded
number's corpse needs to stay visible, or it quietly gets re-trusted the next time someone reads
past it. This table is that corpse. **Do not treat any figure below as current** — the "replaced
by" column is the one to use, and its own section is the one to cite.

| Superseded figure | Estimated as | Replaced by (measured) | Corrected |
|---|---|---|---|
| Per-position SCHG `amount` | "mostly ~$10 each" (extrapolated from 3 unrepresentative 2024-09 rows, §10.9) | mean $202.31 / median $100 across all 202 positions, multi-modal ($10–$3,497.95) — §10.12 | 2026-09-05 |
| SCHG sleeve value (derived from the above) | ~$2,600 (202 × ~$10) | **$44,867** (1,263.502255 units × $35.51, §10.11) | 2026-09-05 |
| `sleeve_cash` / broker cash | ~$1,000 (estimated) | **$1,264.59** (`credit` $1,514.59 − pending orders $250.00) — §10.11 | 2026-09-05 |
| Sleeve-cap sizing decision (§4.7 "Sleeve accounting") | operator-set cap, adopted equity decoupled from sizing | adopted lots counted **in full**, no cap — §4.7 | 2026-09-05 |
| `_parse_trade_history_entry` mismatch characterisation (§10.13, models.py) | "one row, a data anomaly unrelated to fees" (119/120 sample) | 4/120 rows disagree; units-based formula only 117/120 (max error $398.51); `investment`-based formula is the one that holds, 120/120 (max error $0.0091) — §10.13 | 2026-09-05 |
| SCHG `minPositionExposure` (DESIGN.md §1.1/§4.3's live-verified figure, cited as the reason `min_order_size` was raised 10→50) | $50.00, "verified live 2026-09-04" | **$10.00** — same as UPRO — per the 2026-09-05 live eligibility payload (`minPositionExposure: 10.0` for both, `allowOpenPosition: true` for both, `maxUnitsPerOrder` 28,321 SCHG / 13,421 UPRO) — §10.14 | 2026-09-05 |
| Count of unmanaged instruments in the account (18 positions) | "5 instruments" (§4.5, §4.7, §7) | **6 instruments** — ids 1149, 1390, 1865, 4365, 4375, 6365, matching the 6 tickers (IAU, VONG, OEF, FISV, STZ, MINT) already listed in §10.11 — the "5" in prose never matched the "6" already in that table | 2026-09-05 |

**Not wrong, but easy to misread as wrong — kept here for the same reason:** the position *count*
(202 SCHG + 7 UPRO = 209, or 227 including the 18 unmanaged positions) was measured correctly from
the first probe and never revised. Only the *per-position amount* was ever an estimate. Recording
provenance per quantity, not per sentence, is the fix (§10.12's own stated lesson).

**The mirror trap — a correct figure sitting next to a deliberate decision, easy to conflate in
either direction.** `min_order_size = 50` in `config/config.toml` is **not** a stale number
tracking the (now-corrected) $50 SCHG figure above. The operator has independently, deliberately
**demanded `min_order_size = 50`** regardless of what the broker's real per-instrument floor is —
see §10.14. Do not "fix" it down to 10 because its original justification turned out to be wrong;
the justification and the decision are two different things, and only the justification was
wrong. Both facts — the corrected $10 floor, and the deliberate $50 config value — must be read
together, or a future pass will get this wrong in one direction or the other.

---

## 1. Why the old design was structurally wrong

The bad-tick guard in `market/bars.py` tries to answer two different questions with one number:

- **Q1 — is today's tick real?** A deviation band is a weak proxy; a second source answers it.
- **Q2 — is my stored history a faithful, consistently-adjusted series?** A deviation band
  cannot answer this at all. Only re-derivation can.

Every observed symptom follows from that conflation.

**On split day, Q1's answer is "yes, real" and Q2's is "no, corrupt."** The band fires, reports
"bad tick," is wrong about the cause, and aborts the session — accidentally right that something
is broken. Worse, the obvious fix makes it dangerous: if the band were replaced by cross-source
validation *without* fixing the history, every source would agree the price dropped 75%, the bar
would be accepted, and a 260-bar rolling stdev computed against unadjusted history would read a
phantom crash and generate a maximum-conviction buy. **A loud abort would become a silent
catastrophic trade.**

This is not hypothetical. Two of the three managed symbols split on a ~2–3 year cadence and both
are due (§2.1).

---

## 2. Findings — all empirical

### 2.1 Splits are a scheduled event, not a tail risk

Verified via yfinance, 2026-09-04:

| Symbol | Splits |
|---|---|
| **UPRO** | 2011-02-25 (3:1), 2013-06-10 (2:1), 2015-05-20 (2:1), 2018-05-24 (3:1), **2022-01-13 (2:1)** |
| **SCHG** | 2022-03-11 (2:1), **2024-10-11 (4:1)** |
| **SPY** | none in range |

A 4:1 split is a 75% price drop. UPRO has split five times and is past its historical interval.
There is **zero split handling in the codebase today** — confirmed by full-repo grep; every hit
for "split" is `str.split()` or Telegram message chunking.

**eToro's own candles are back-adjusted too** — confirmed by direct probe (§10.1). This does not
change the split-handling architecture in this design, which never reads eToro candles as a
history source (§3); it matters only for §10.2's cross-check question.

### 2.2 Dividend adjustment epochs contaminate any accumulated store

All three symbols pay quarterly. SPY pays ~$1.90 on a ~$600 price — **~0.32% per quarter**.

With `auto_adjust`-style data, **every dividend retroactively rescales all prior prices**. An
append-only store therefore becomes a patchwork of adjustment epochs, injecting a ~0.32% phantom
move into the return series four times a year. Far too small to trip any bad-tick band, and
directly polluting a 260-day rolling standard deviation. Silent degradation — the worst failure
mode available.

**`activity_server` has exactly this bug**, and it is instructive:

- `tools/stock_price/fetcher.py:139` hard-codes `auto_adjust: True` on every fetch.
- `tools/stock_actions/extractor.py:98-106` — the only new-action detection in the codebase —
  keys solely on `stock_splits != 1.0`. Dividends are written to `sa_corporate_actions` one line
  earlier and then **never compared, never flagged, never trigger a rebuild**.
- `apply_pending_splits` filters them out by construction (`WHERE ... stock_splits != 1.0`).
- Its daily gap-fill only ever appends `[cache_max+1, end]`, so rows fetched under older
  adjustment epochs are never revisited.

**Root cause, and the design principle that follows:** the epoch bug exists *because adjusted
prices are stored*. An adjusted price is a function of every corporate action after its own bar
date — a value that changes retroactively. Storing a retroactively-changing value and appending
to it over time produces patchwork by construction. No rebuild logic fixes the category error.

### 2.3 The trading calendar is far better than feared

`market/calendar.py` diffed against `exchange_calendars` 4.13.2, `pandas_market_calendars`
5.4.0, and 2,935 observed SPY sessions, across **4,748 dates (2015-01-01 → 2027-12-31)**.

**Exactly two wrong dates:**

| Date | calendar.py | Reality |
|---|---|---|
| 2018-12-05 | trading day | Closed — George H.W. Bush national day of mourning |
| 2025-01-09 | trading day | Closed — Jimmy Carter national day of mourning |

Zero early-close disagreements. Zero false negatives. Every algorithmic rule correct across 13
years — Good Friday all 13, Juneteenth's 2022 gating, weekend observance for July 4 and
Christmas, and the deliberately asymmetric NYSE Rule 7.2 New Year's carve-out.

Both misses are **ad hoc presidential-funeral closures**, announced days ahead and derivable
from no rule. `exchange_calendars` only gets them right because it ships a hardcoded override
table. **No algorithmic calendar can ever catch these.**

**The important consequence: the staleness gate is the backstop for the calendar's only real
weakness.** On 2025-01-09 the app would wake, poll, and get a stale-or-absent price — and gate
10 halts. Neither mechanism alone suffices; together they close the hole.

### 2.4 Yahoo is not a dependable on-device source, in any form

- `yfinance` **0.2.58** made `curl_cffi` a hard dependency; every version through 1.7.0 keeps it.
- `curl_cffi` is **impossible on Chaquopy** — it needs `cffi>=2.0.0` and no compatible wheel
  exists in Chaquopy's index. Verified by real pip resolution failure.
- `yfinance==0.2.57` is the last installable version and **does build successfully** on
  Chaquopy — but it relies on plain `requests`, which is precisely the mechanism Yahoo's
  anti-bot TLS fingerprinting defeated. That is *why* the maintainers added `curl_cffi`.
- Plain `curl` to `query1.finance.yahoo.com` from this network returned **HTTP 429** on the
  first attempt.
- Hitting Yahoo's `chart` JSON endpoint directly with `httpx` does **not** escape this — `httpx`
  presents a Python TLS fingerprint exactly as `requests` does. Same vulnerability, less code.

Pinning `0.2.57` would mean deliberately choosing the broken fetch mechanism, frozen, with no
upgrade path. **Rejected for on-device use.**

### 2.5 Chaquopy dependency verdicts — empirical

Real `installDebugPythonRequirements` runs, one package at a time, in an isolated
`android-probe-deps/` copy (Chaquopy 17.0.0, Python 3.13, arm64-v8a, minSdk 24).

| Package | Verdict | Notes |
|---|---|---|
| `httpx==0.27.2` | **YES** | pure-Python; `anyio`, `httpcore`, `h11`, `certifi`, `idna`, `sniffio` all pure |
| `numpy` | **YES** | Chaquopy prebuilt **1.26.2** |
| `pandas` | **YES** | Chaquopy prebuilt **2.1.3** |
| `lxml` | **YES** | Chaquopy prebuilt 5.3.0 |
| `exchange_calendars` | **YES** | resolves to **4.11.1** on py3.13 |
| `curl_cffi` | **NO** | `cffi>=2.0.0` unavailable |
| `yfinance` ≥0.2.58 | **NO** | pulls `curl_cffi` |
| `yfinance==0.2.57` | yes, but rejected | see §2.4 |

**Caveat: build-time resolution only — no device was connected.** A wheel resolving is not a
wheel importing and running. `numpy` 1.26.2 / `pandas` 2.1.3 on arm64 under Chaquopy remain
unverified at runtime. See §11.

### 2.6 `exchange_calendars` 4.11.1 is safe to ship

Full session-list diff, 4.11.1 vs 4.13.2, XNYS, 2015–2027: **3,186 sessions in both, zero
differing dates.** Both funeral closures present in `adhoc_holidays` in both versions.

Two implementation traps:

- **Do not call `is_session()`.** It raises `DateOutOfBounds` for *every* date — including
  ordinary trading days — under pandas 3.0.5. Use membership against `.sessions` and
  `.adhoc_holidays`, which are authoritative and unaffected. This matters because the desktop
  venv has pandas **3.0.5** while Chaquopy ships **2.1.3**: code against the API that works on
  both.
- **The default calendar range ends 2027-09-03.** Adequate for T-30 scheduling; construct with
  explicit bounds if anything ever needs sessions further out.

### 2.7 Tiingo is the source

Only provider whose own documentation confirms all four fields in one free response:

```
close        →  raw, as-traded
adjClose     →  split- and dividend-adjusted
splitFactor  →  per-day split ratio
divCash      →  per-day dividend
```

50 req/hour, 1000/day (we need 3/day) · plain `httpx` GET with a query-param key · no JS
challenge · reachable from this network.

**The real ceiling, confirmed from the operator's Tiingo dashboard, 2026-09-05 — see §10.4.**
This project had been sizing its fetch window against the *request* quota above while the
account's actual binding limit is *bytes*: Tiingo's free tier is **1 GB bandwidth/month, 1000
requests/day, 50 requests/hour, 500 unique symbols/month**. The bandwidth figure appeared nowhere
in this document before this correction. §10.4 has the full sizing math this unblocks.

**History depth, corrected — verified 2026-09-04.** The original "30+ years" figure was checked
only against SPY. Full per-symbol verification shows it does not generalize:

| Symbol | Earliest row | Row count |
|---|---|---|
| SPY | 1993-01-29 | 8,457 |
| UPRO | 2009-06-25 | 4,325 |
| SCHG | 2009-12-11 | 4,207 |

**These are instrument-inception limits, not Tiingo tier limits** — UPRO and SCHG simply did not
exist before 2009. Tiingo returns exactly what trading history exists for each fund; the ~300-day
window §3.1 fetches is comfortably inside all three histories regardless.

**Tiingo returns no rate-limit headers at all** — no `X-RateLimit-*` or equivalent on any
response observed. The 50/hour, 1000/day budget cannot be read back from the API; the app must
count its own calls locally and self-throttle against the documented limits rather than react to
a server-reported remaining count.

**Constraint to respect: Tiingo's free tier is contractually personal-use only.** The data must
not be redistributed or shared onward. Fine for a personal tool; it means the series cannot be
published through the Worker (§9) — only derived signals and account state may be.

Rejected alternatives:

- **Alpha Vantage** — `TIME_SERIES_DAILY_ADJUSTED` (the ideal one-call response) has **moved to
  Premium**. The free `TIME_SERIES_DAILY` gives raw close only, no splits or dividends.
- **EODHD** — viable fallback (raw `close` + `adjusted_close` in one response) but 20 req/day and
  only 1 year of lookback; no separate split/dividend fields on that endpoint.
- **Stooq** — now serves a JavaScript proof-of-work bot challenge instead of CSV. Dead as a
  scriptable source.
- Finnhub / Twelve Data / Marketstack / FMP — historical candles or corporate actions gated,
  tier-ambiguous, or quotas too tight.

### 2.8 The Kominfo block is live on the dev machine

| Host | ISP resolver | Cloudflare DoH |
|---|---|---|
| `public-api.etoro.com` | `202.137.1.74` (sinkhole, drops packets) | `104.18.39.131`, `172.64.148.125` |
| `www.etoro.com` | `202.137.1.74` (same sinkhole) | — |
| `query1.finance.yahoo.com` | correct | correct |

The Private DNS fix (HANDOFF.md §2, DESIGN.md §7.4b) is applied on the **z10 only**. The
development PC still resolves every eToro hostname to the sinkhole, and it presents as a bare
12-second TCP timeout with no diagnostic hint.

**Operational consequence: no eToro-touching probe, forward test, or integration test can run
from the dev PC.** All nine candidate data-provider hostnames, by contrast, resolve correctly and
connect fine from the same machine — eToro is specifically blocklisted, not caught by a generic
redirect.

### 2.9 Live-rate staleness, reproduced during market hours

Probed on the physical z10, 2026-09-04, ~10:58 ET — **US market open**. Method: `curl` executed
on the device via `adb shell`, with credentials piped to `curl --config -` over stdin so the key
never touched device disk or argv. Read-only search/rates/candles endpoints only. The dev PC
cannot reach eToro at all (§2.8); the device's Private DNS resolves `public-api.etoro.com` to the
real Cloudflare IP `104.18.39.131`, confirming §2.8's fix works on the device.

Instrument ids resolved: SPY = 3000, UPRO = 3244, SCHG = 1084. Two polls ~7s apart:

| Symbol | Poll | lastExecution | bid/ask | raw `date` | age vs wall clock |
|---|---|---|---|---|---|
| SPY | 1 | 769.62 | 769.62/769.64 | 2026-09-04T14:58:25.65Z | ~0s |
| SPY | 2 | 769.64 | 769.64/769.66 | 2026-09-04T14:58:32.71Z | ~0s |
| UPRO | 1 | 153.67 | 153.67/153.71 | 2026-09-03T19:59:54.84Z | 68,311s (~19.0h) |
| UPRO | 2 | 153.67 | 153.67/153.71 | 2026-09-03T19:59:54.84Z | 68,318s (~19.0h) |
| SCHG | 1 | 35.5 | 35.5/35.51 | 2026-09-04T14:58:25.37Z | ~0s |
| SCHG | 2 | 35.5 | 35.5/35.51 | 2026-09-04T14:58:32.97Z | ~0s |

SPY and SCHG timestamps advanced between polls; UPRO's was byte-identical across both — frozen at
the prior session's 16:00 ET close.

**This reproduces the staleness finding recorded elsewhere in this design (§6, §12), on a
different symbol and under more dangerous conditions.** The earlier observation caught SCHG stale
with the market **closed**; this catches **UPRO stale with the market open** — exactly the window
the app trades in. The stale symbol **rotates** — SCHG before, UPRO now — so it cannot be handled
with a per-symbol exception or allowlist.

**Why this is the worst case:** UPRO is the leveraged instrument the strategy actually buys. A
19-hour-old price during a moving market produces both a wrong signal and a wrong fill.

**Critically: no price-based check catches this.** `_rate_price()` reads `lastExecution`, falling
back to the bid/ask midpoint. Here `lastExecution` is present (not `None`), so the fallback never
triggers — and bid/ask are frozen at the same stale values anyway, so the midpoint is equally
stale. 153.67 is a perfectly plausible UPRO price; it breaches no sanity bound (§5, layer 1) and
no deviation band (§5, layer 2). **Only the timestamp reveals it.** This is the empirical
justification for gate 10's freshness check being the highest-value single fix in this design
(§12).

**The alternative explanation is now RESOLVED — refuted.** UPRO may have been quoted stale
because eToro does not trade it for this account/entity, rather than because the feed is broken.
DESIGN.md §12 tracked "UPRO may not be tradable on your eToro entity" as a Medium risk, and
preflight gate 8 checks `allowOpenPosition`. That check has now been run: `POST
/api/v2/trading/info/demo/eligibility` (HTTP 200), probed 2026-09-04 for SPY (3000), UPRO (3244),
and SCHG (1084) in one call, returns `allowOpenPosition: true` for **all three**. UPRO's record is
structurally identical to SPY's and SCHG's — `minPositionExposure: 10.0`, `maxUnitsPerOrder:
13421`, `allowClosePosition`/`allowMitOrders`/`allowEntryOrders`/`allowExitOrders`/
`allowTrailingStopLoss` all `true`, populated `leverageConfigs`, `requiresW8Ben: true`.
`notFoundInstrumentIds` and `notFoundSymbols` are both empty. No restriction, suspension, or
regional-ineligibility marker anywhere. **UPRO is fully tradable on this account.** The staleness
is therefore a genuine feed bug, not a gate-8 concern — the two causes are separated, and only one
of them is real.

**The error, quantified.** Three polls at 10:58, 11:11, and 11:15 ET all returned UPRO `date =
2026-09-03T19:59:54.8409896Z`, byte-identical down to the sub-second digits, `lastExecution =
153.67`, while SPY and SCHG ticked forward to the second in the same batch request each time.
Cross-checked against Tiingo (§10.2):

| Quantity | Value | Deviation |
|---|---|---|
| UPRO 2026-09-03 official close (Tiingo) | 153.73 | — |
| eToro's frozen "live" quote | 153.67 | −0.039% — it **is** yesterday's close, not a fresh error |
| UPRO true price now (Tiingo) | 151.665 | — |
| **eToro quote vs. true current price** | **+1.322%** (**+$2.00/share**) | the actual pricing error |
| SPY today, same window | 773.17 → 769.89 | −0.424% |
| Implied UPRO move at 3× leverage | | −1.273% |

The pricing error (+1.322%) and the leverage-scaled index move (−1.273%, opposite sign because
the frozen quote is stale-high on a down day) close to within 0.05%. **eToro is serving
yesterday's close as if it were a live quote, and the resulting error equals the day's
accumulated move, tripled by leverage.**

**The scaling danger — the most important consequence.** The cone fires at `z < -2`: large *down*
moves. That is precisely where this error is largest, because the error is the leveraged move
itself. Today's mild −0.42% SPY day already produces a 1.3% UPRO pricing error; a −3% SPY day
implies roughly 9%; 2020-03-16's −12% SPY day implies roughly 36%. **The error is not random —
it is always adverse.** The stale prior close sits above the true price on any down day, so a buy
sized against it is sized against a price far too high, on exactly the day the strategy most wants
to buy. This is not a symmetric noise source; it is a one-directional sizing hazard that gets
worse as the trade gets more attractive.

**The rotation threat.** The symbol caught stale was SCHG on the earlier (market-closed) probe and
is UPRO on this one (§2.9 above). Nothing in the evidence excludes SPY rotating in next. SPY is
not a buy instrument here — it is the **signal** instrument, feeding `cone_5_z`. A frozen SPY
quote would not just mis-size a trade; it would corrupt the z-score computation itself and
manufacture or suppress the entry decision outright. Today's UPRO failure, bad as it is, is the
mild version of what this bug can do.

**Partial insulation from §4.** Because §4's sidecar design makes the broker authoritative for
`entry_price` after the fill, a stale quote at decision time corrupts *sizing intent*, not the
recorded position — the next portfolio read carries eToro's own fill price and self-corrects.
Under the superseded `lots`-authoritative design (§4, "Status: superseded"), the wrong provisional
price would have been written into the lot book and stayed there permanently. This is a real
mitigation, not a reason to relax gate 10: the trade still gets sized wrong at the moment it is
placed, and units once bought cannot be un-bought.

**Confirmed on the LIVE account, 2026-09-04, market open — this is not a demo artifact.** Every
reading above this line in §2.9 was taken on the demo account. A read-only probe against the real
account, same session, removes the last room for "maybe it's a demo-only quirk": two live polls
65s apart (11:23 / 11:24 ET) both returned UPRO `date = 2026-09-03T19:59:54.8409896Z`,
`lastExecution = 153.67` — bit-for-bit identical to each other, and bit-for-bit identical to the
demo readings above. SPY and SCHG advanced second-by-second in the same requests. **The freeze is
present on live and demo simultaneously**, which is consistent with a single shared market-data
layer feeding both — the rates path carries no demo/real segment to diverge on.

This is not academic for this account: it holds 7 UPRO positions, 26.672246 units, cost basis
$3,999.9994, weighted-average entry $149.9686.

**Resolved by direct observation of eToro's own app** (screenshots taken 11:28-11:29 ET, four
minutes after the live API poll). The app displays UPRO at **151.43 (-1.50%)** and the position
group at **$4,038.98 (+$38.98)** — and 26.67225 x 151.43 = $4,038.96. Tiingo independently put
UPRO at 151.295 at 11:24 ET. The app's -2.30 day change implies a prior close of 153.73, which is
exactly Tiingo's official 2026-09-03 close.

So **eToro's app marks the position at the correct live price, and the account's displayed P&L is
accurate.** An earlier draft of this section inferred a $63.35 / 1.57% unrealized-P&L
overstatement on the account. **That inference was wrong and is retracted** — the app does not
inherit the staleness.

**The defect is confined to the public API's rates endpoint.** eToro's internal pricing is
correct and its own app renders it correctly; only `/api/v1/market-data/instruments/rates` serves
a stale `lastExecution` for UPRO. That narrows the bug's blast radius but does not reduce it for
this project, because **etoroNightwatch is an API consumer**: at T-30 it would have taken 153.67
as today's provisional close while the true price was 151.4, and nothing in the app would have
noticed.

It also makes this an unusually well-evidenced vendor bug report — the vendor's own app serves as
the control, alongside an exact frozen timestamp, five polls across two environments, and an
independent third-party price (§10.6).

---

### 2.10 No eToro streaming feed exists

Probed 2026-09-04 to check whether a push/streaming feed could sidestep the polling staleness in
§2.9. It cannot — none is reachable.

- `ws.etoro.com` is a CNAME to `www.etoro.com.cdn.cloudflare.net`. All five probed paths (`/`,
  `/ws`, `/stream`, `/signalr`, `/socket.io/?EIO=4&transport=websocket`) return **403 Cloudflare**
  with a bot-check HTML body. This is the marketing CDN, not an API gateway — there is nothing
  behind this hostname to negotiate a WebSocket with.
- `streaming.etoro.com` and `push.etoro.com` (both resolve to `128.251.126.175`) and
  `api.etoro.com` (Azure API Management, `52.233.200.252`) all **reset the TLS connection
  immediately after ClientHello** — no HTTP response is obtainable, not even an error page.
- This is not a client limitation: the device's `curl` build supports `ws`/`wss`
  (`Protocols: file http https ipfs ipns mqtt ws wss`).

**Caveat, stated plainly because the cause is not fully separated:** a post-ClientHello reset
could be a server-side WAF/JA3 fingerprint block, or SNI-based filtering somewhere upstream.
`public-api.etoro.com` works from the same device over the same connection, which makes
domain-wide ISP blocking (of the kind in §2.8, but broader) unlikely — but it does not rule out
per-hostname SNI filtering, and the two causes are not fully distinguished by this probe.

**This closes a gap in DESIGN.md's ground-truth work.** DESIGN.md §1.1's API survey never covered
a streaming feed at all; this probe is the first evidence either way, and the answer is negative.
The architecture must keep assuming polling-only (§3, §6 gate 10) — there is no push alternative
to fall back to.

`/api/v1/market-data/instruments` was also checked as a possible alternative source of
tradability signal, since it is unauthenticated and would be cheaper to poll than the eligibility
endpoint. It does not carry one: it returns only `distributionType, exchangeID,
hasExpirationDate, images, instrumentDisplayName, instrumentID, instrumentTypeID,
isInternalInstrument, priceSource, symbolFull` (SPY and SCHG additionally carry
`stocksIndustryID`; UPRO does not — a classification difference, not a tradability signal).
`allowOpenPosition` and `minPositionExposure` live only on the eligibility endpoint, which
`etoro/client.py:725` (`get_eligibility`) already implements correctly.

---

### 2.11 The candles endpoint is unaffected by the rates freeze

Probed on the z10, 2026-09-04, ~11:32 ET — market open, demo read credentials — specifically to
find out whether §2.9's freeze is a property of eToro's price data in general or of the rates
endpoint alone.

**A. The candles endpoint is NOT affected by the rates freeze.** In a single run:

```
CANDLES  UPRO 2026-09-04  O=152.96 H=153.43 L=151.15 C=151.19   <- current & correct
CANDLES  UPRO 2026-09-03  C=153.67                              <- yesterday's close
RATES    UPRO lastExecution=153.67  date=2026-09-03T19:59:54Z   <- still frozen
RATES    SPY  769.12 @ 15:32:18Z (live)   SCHG 35.47 @ 15:32:17Z (live)
```

Today's UPRO daily candle closes at 151.19, consistent with eToro's own app (151.43, §2.9) and
Tiingo (151.295, §2.9). Yesterday's candle closes at exactly 153.67 — the same value the rates
endpoint is frozen on, confirming 153.67 is genuinely the prior close, not a random stale number.

**B. All intraday granularities work and are current.** `OneMinute`, `FiveMinutes`,
`TenMinutes`, `FifteenMinutes`, `ThirtyMinutes`, `OneHour`, `FourHours` all returned HTTP 200
with current data in the same run — no 400s. UPRO's latest bars: `OneMinute` 11:32:00 EDT close
151.19; `FiveMinutes` 11:30 close 151.19; `ThirtyMinutes` 11:30 close 151.17; `OneHour` 11:00
close 151.19. SPY and SCHG tick live on `OneMinute` in the same run.

**C. eToro daily closes are NOT official NYSE closes.** Cross-checked against Tiingo:

| Date | eToro candle close (SPY) | Tiingo official close | Diff |
|---|---|---|---|
| 2026-09-03 | 772.38 | 773.17 | −0.102% |
| 2026-09-02 | 764.10 | 765.16 | −0.139% |
| 2026-09-01 | 761.91 | 761.78 | +0.017% |

The difference is variable in sign and magnitude, so it is **not** an adjustment-factor artifact
(§10.1's split-adjustment finding does not explain a sign flip) — it is a different close
convention (venue/timing), on the order of 0.1%. **eToro candles are not a substitute for an
official close**, even though (A)/(B) show them to be reliably current.

**The key implication.** The rates-freeze defect (§2.9) is isolated to
`/api/v1/market-data/instruments/rates` — the candles path, at every granularity down to
one minute, is unaffected and current. A working, current, self-timestamped alternative exists
inside the same API this app already calls. This does not make eToro candles equivalent to an
official close (C) — it means they are a reliable *current-price* source, which is a different
property than the rates endpoint was ever meant to supply and failed to.

---

## 3. Architecture — fetch, derive, discard

### 3.1 Every session

```
[fetch]    Tiingo: ~300 trading days of daily bars for SPY, UPRO, SCHG   (3 requests)
[derive]   adjClose series → cone_5_z, ath, drawdown, tier
[splice]   today's provisional close from Tiingo IEX tngoLast (primary);
           cross-check against eToro's own candles (today's OneDay close, or
           the latest OneMinute bar) — NEVER from the eToro rates endpoint
[decide]   strategy
[discard]  the price series is never written to SQLite
```

**The provisional close is never taken from the eToro rates endpoint.** §2.9 proved that
endpoint serves a stale `lastExecution` — 19 hours old on UPRO, with the market open, on both
demo and live accounts — with no price-based signature distinguishing the stale value from a
real one. §2.11 confirms the defect is confined to that one endpoint.

- **Primary source: Tiingo IEX `tngoLast`.** This makes the *entire* 265-bar window share one
  close convention — Tiingo `adjClose` for history, Tiingo IEX for today — so there is no
  cross-vendor seam at all (§8). It is also already the live-tick cross-check this design
  specifies for the guard (§5.1, §10.2), so acquiring the provisional close this way costs no
  extra request beyond what §5.1 already makes.
- **Fallback / cross-check: eToro's own candles** — today's `OneDay` candle close, or, better,
  the most recent `OneMinute` bar, which is self-timestamped at minute granularity rather than
  only date-stamped (§2.11). Used when Tiingo IEX is unreachable, and always available as a
  second opinion regardless of which source is primary.
- **Never the rates endpoint**, for the provisional close or for anything else it is asked to
  supply — §2.9 and §10.6 establish it is not safe to read for a current price.

eToro remains the **execution venue** — orders are still placed and filled through eToro; this
change concerns only where the app reads the number that decides whether and how much to buy.

HTTP requests per session now cover Tiingo history, corporate actions, the Tiingo IEX
provisional-close read, and the eToro candles cross-check — still comfortably inside Tiingo's
50/hour, 1000/day budget (§2.7).

### 3.2 What this deletes rather than mitigates

| Problem | Old design | Now |
|---|---|---|
| Missing trading days | detect → alert → re-seed | **not expressible** — every session fetches a complete window |
| Split adjustment of history | detect → rebuild → re-adjust | **not needed** — a fresh `adjClose` window is coherent by construction |
| Dividend adjustment epochs | not handled (would have inherited activity_server's bug) | **impossible** — one fetch is one epoch |
| Provisional bar poisoning the 260-bar window (§4.2) | settle next session, flip `is_provisional=0`, fail preflight if unsettled | **impossible** — the provisional close lives in memory for one session and is never persisted. Tomorrow's fetch carries the real official close, already adjusted |
| Gap-aware compounding tolerance, 10× cap, `STALE_REFERENCE_TRADING_DAYS` | needed because the reference close could be weeks old and mis-adjusted | **deleted** — the reference is always exactly one session old and correctly adjusted |

DESIGN.md §4.2's central worry — *"if we leave the provisional value in place, every future
signal is computed against a slightly wrong history, and the error compounds silently"* — is
**removed, not reduced**. Nothing persists to compound.

The `is_provisional` column, the `settle_previous()` flow, and step [2] of the session sequence
all go away.

### 3.3 Availability trade-off, stated plainly

With no cached history, **no data at T-30 means no trade.** There is no stale-cache fallback.
This is fail-closed and correct — trading on a stale window is worse than not trading — but it
makes the session's availability depend on Tiingo and eToro both being reachable inside the
T-30 window. Quota is not the constraint (3 of 1000 daily requests); reachability is.

Accepted deliberately. A missed session costs one day of signal; a wrong session costs money.

### 3.4 One property we lose, and a cheap recovery

Never storing prices means we cannot compare today's fetch against yesterday's, so **a silent
upstream revision of history becomes undetectable.**

Recovery without storing price data: persist a **fingerprint** per symbol per session —
window start/end date, bar count, and a SHA-256 over the `(date, adjClose)` series. A few dozen
bytes, no price series, and it restores revision detection: if yesterday's overlapping window
hashes differently today, the provider changed history and that deserves an alert.

Recommended, and offered as a judgment call — it is a fingerprint, not a price store, but it is
adjacent to the line drawn in §0.

---

## 4. Positions live at the broker; the sidecar holds strategy state

**Status: superseded 2026-09-04.** The original version of this section kept the local `lots`
table authoritative for positions and, on a split, halted and applied ratio math to it. That is
wrong for the same reason §1–§3 threw out accumulated price history: it stores, and then tries to
patch, a value someone else derives authoritatively. The decision below replaces it.

**eToro is the system of record for the financial facts of a position. The local store keeps only
split-invariant strategy annotations, keyed by eToro's `position_id`.**

### 4.1 The asymmetry that makes it work

A SCHG 4:1 rewrites `units` and `entry_price`. It does **not** touch `book`, `bucket_id`, or any
`entry_spy_*` field — those are SPY-referenced, and SPY did not split. The financial facts are
exactly the ones a split corrupts; the strategy facts are split-invariant.

Split handling for positions therefore dissolves by the same mechanism as split handling for
prices (§3): stop storing what someone else derives authoritatively. No ratio math anywhere, no
halt-and-adjust, no `processed_at` bookkeeping on lots.

### 4.2 What the broker gives — CORRECTED against a live 227-position payload

`_parse_position()` (`etoro/client.py:441`) was written to read `currentRate`, `invested`,
`unrealizedPL`, and `symbol` off each position object. **A live, non-empty portfolio (227 open
positions, probed 2026-09-04) confirms none of these keys exist on a broker position.** Confirmed
present instead: `positionID`, `instrumentID`, `units`, `openRate`, `amount`,
`unitsBaseValueDollars`, `isBuy`, `leverage`, `openDateTime`, plus a long tail of fields the design
does not currently use (`totalFees`, `initialUnits`, `isSettled`, `settlementTypeID`, …). **There
is no symbol string anywhere on a position object.**

The consequence is silent, not a crash: every `Position` parsed from a live portfolio today comes
back with `current_price=None`, `invested=None`, `unrealized_pnl=None`, `symbol=None`, while
`position_id`, `units`, `entry_price`, `leverage`, `is_buy`, and `open_date` populate correctly
from the fields that do exist (`positionID`→`position_id`, `openRate`→`entry_price`, etc.). See
§10 for the open-question writeup of the parser bug itself.

Two design consequences follow directly:

- **Symbol matching must key on `instrumentID`, never on a symbol string.** There is no symbol on
  a position to match against. The `instruments` table already maps ticker → `instrument_id`
  (§7), so every place this design says "symbol match" (§4.5 invariant 4, §4.6's re-link key)
  means an `instrumentID` join, not a string comparison.
- **Mark-to-market must be computed, not read.** `current_price` and `unrealized_pnl` do not exist
  on the broker payload at all — not "sometimes null," genuinely absent as keys. This is not extra
  work: §3 already fetches Tiingo/eToro prices every session for the signal, so the same fetched
  price is what marks a position, using `amount` (§4.5) as cost basis rather than the
  nonexistent `invested`.

What genuinely exists and survives into `Position`: `position_id`, `instrument_id`, `units`,
`entry_price` (from `openRate`), `amount` (cost basis — see §4.4, §4.5), `leverage`, `is_buy`,
`open_date`.

`TradeHistoryEntry`, from `GET /trading/info/trade/history` (`models.py`): **re-verified 2026-09-05
against 120 real closed-trade rows** (the same probe that closed §10.5). The parser previously read
`positionID`, `symbol`, `action`, `profit`, `openDateTime`, `closeDateTime` — none of those names
exist on a trade-history row. The real keys are `positionId`/`instrumentId` (lowercase `d` — trade
history's own casing, **not** the portfolio endpoint's capital-`D` `positionID`/`instrumentID`;
eToro is inconsistent across its own endpoints), `netProfit`, `openTimestamp`, `closeTimestamp`,
plus `investment`, `fees`, `isBuy`, `leverage`. There is no `symbol` or `action` field, mirroring
`Position` — `symbol` must be resolved from `instrument_id` through the `instruments` table, and
`is_buy`/`leverage` are the real equivalents of the fictitious `action`. `netProfit` maps to
`profit`. **Corrected 2026-09-05 (§10.13) — do not read the numbers below as current.** An earlier
pass here checked `profit ≈ units × (closeRate − openRate)` against a 119-row sample, called the
one mismatching row "a data anomaly," and stopped there. A full 120-row check found **three more
rows like it** (4/120 total): the units-based formula actually matches only 117/120, max error
$398.51, and is worse still once `fees` is added (mean abs error 3.20 vs. 0.0032). **The formula
that actually holds is `investment × (closeRate / openRate − 1)`, matching `profit` within $0.02 on
120/120 rows, max error $0.0091** — `investment`, not `units`/`openRate`, is the reliable quantity
on a closed trade, mirroring `amount` on an open one (§4.5, §4.6). `profit` is **not** already net
of `fees` despite the field's broker-side name, and a caller wanting an all-in realized P&L must
subtract `fees` itself. See §10.13 for the full, corrected writeup. `TradeHistoryEntry` now
carries: `position_id`, `instrument_id`, `units`, `is_buy`, `leverage`, `open_rate`, `close_rate`,
`profit`, `investment`, `fees`, `open_date`, `close_date`.

Together these replace the `lots` columns `position_id`, `units`, `entry_price`, `cost_basis`,
`entry_date`, `is_open`, `exit_price`, `exit_date`, `realized_pnl`.

### 4.3 What the broker cannot give

| Column | Why not derivable |
|---|---|
| `book` | `CHECK (book IN ('catapult','reentry'))` — two ladders per symbol, and a broker position carries no hint which one it belongs to. |
| `bucket_id` | Links to `buckets(sale_date, proceeds, tranche_amount, tranches_bought, last_reference_spy)`. Sold positions vanish from the portfolio; tranche counters are pure local bookkeeping. |
| `entry_reference_spy` | Tracks `buckets.last_reference_spy` — path-dependent, not a function of the SPY series. |
| `entry_spy_ath`, `entry_spy_close`, `entry_spy_drawdown`, `entry_tier` | Re-derivable **in principle** from `open_date` plus a long SPY series — but MUST NOT be. |

The last row needs its own reasoning. The value stamped at entry was the **provisional T-30
close**, not the official close (DESIGN.md §4.2). Re-deriving from history yields the official
close instead, so every ladder reference would silently shift. `entry_tier` is **discrete**, so
even a small drift can flip a tier at a boundary — changing ladder behaviour categorically, not
marginally. Stamp at entry, keep forever.

### 4.4 The sidecar table

```sql
CREATE TABLE IF NOT EXISTS lot_annotations (
    position_id          TEXT PRIMARY KEY,   -- eToro's id; the join key
    symbol               TEXT NOT NULL,
    book                 TEXT NOT NULL CHECK (book IN ('catapult', 'reentry')),
    entry_reference_spy  REAL,
    entry_spy_ath        REAL,
    entry_spy_close      REAL,
    entry_spy_drawdown   REAL,
    entry_tier           INTEGER,
    bucket_id            TEXT REFERENCES buckets (bucket_id),
    entry_order_id       INTEGER REFERENCES orders (id),
    exit_order_id        INTEGER REFERENCES orders (id),
    -- Re-link key. Stamped once at open, never updated, never read for P&L or
    -- strategy. Present ONLY so a sidecar row can be re-matched to a broker
    -- position if position_id does not survive a corporate action.
    --
    -- This is `amount` (cost basis), not units or entry price, because a live
    -- 227-position probe (2026-09-04) proved units and openRate are NOT
    -- immutable: eToro retroactively rescales both on a split (15 pre-split
    -- SCHG positions, opened before the 2024-10-11 4:1, now carry post-split
    -- units and openRate). `amount` is preserved exactly across the same
    -- split — verified `amount == units * openRate` on 227 of 227 live
    -- positions (§4.5) — making it the only one of the three quantities that
    -- actually survives a corporate action. The original design used
    -- `entry_units_at_open` + `entry_price_at_open` as the "immutable" keys;
    -- that was wrong (§4.6) and is corrected here.
    entry_date           TEXT NOT NULL,
    entry_amount         REAL NOT NULL,
    is_open              INTEGER NOT NULL DEFAULT 1 CHECK (is_open IN (0, 1)),
    -- Provenance, not an access gate. Records whether the strategy annotations
    -- on this row were stamped at entry by the app ('app') or reconstructed
    -- from history for a position that predates the app ('adopted') — see
    -- §4.7. Both kinds are equally managed and equally closeable; this column
    -- exists only so a future reader can tell derived values from stamped
    -- ones, not to restrict what the executor may do.
    origin               TEXT NOT NULL CHECK (origin IN ('app', 'adopted')),
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
) WITHOUT ROWID;
```

### 4.5 Gate 11 inverts

Reconciliation stops being "broker and local must agree on numbers" — there is nothing to
disagree about once the broker owns them — and becomes a membership + invariant check:

1. Every open broker position in a managed symbol has a sidecar row, else it was opened outside
   the app or the sidecar was lost — **halt**.
2. Every open sidecar row has a live broker position, else it was closed outside the app —
   **halt**.
3. `amount ≈ units × openRate` per position — the workable form of this invariant, since
   `invested` does not exist on the broker payload (§4.2). **Verified 227/227, zero mismatches,
   on a live account (2026-09-04).** Catches a broker-side split applied to units but not
   openRate — **halt**.
4. `instrumentID` and `is_buy` match, leverage is 1 (DESIGN.md §6.A — never margin, never short).
   This was originally specified as a symbol match; there is no symbol on a broker position
   (§4.2), so the match must be on `instrumentID`, joined through the `instruments` table.

DESIGN.md §6.D is preserved: the executor may only close a `position_id` present in the sidecar
— the sidecar is the allowlist. **This is not a hypothetical guard.** The same live probe found
227 open positions across 8 instruments — 202 SCHG, 7 UPRO, and 18 across **6** instruments this
app does not manage (ids 1149, 1390, 1865, 4365, 4375, 6365 — corrected 2026-09-05, §0.1; earlier
text in this section said "5", which never matched the 6 tickers already listed in §10.11) —
meaning **220 of 227 open positions are outside the managed set**. Gates 1–2
and DESIGN.md §6.D's allowlist are protecting a large amount of unrelated capital that sits in the
same account, not a corner case.

Invariant 1 above would halt every session against these 209 managed-symbol positions, since none
of them has a sidecar row today. §4.7 is the migration that satisfies invariant 1 before gate 11
ever runs against a live sidecar — the gate's logic is unchanged; what changes is that the sidecar
is backfilled first.

### 4.6 Three broker behaviours — two now ANSWERED against a live account, refuting part of §4.4's original design

The first two were previously untestable on a demo account holding zero positions. A live account
holding 202 SCHG positions, 15 of them opened before the 2024-10-11 4:1 split, answers both —
probed 2026-09-04:

```
2024-09-03  units=0.3964  openRate=25.2275  amount=10.0   units*openRate=10.00
2024-09-03  units=0.4000  openRate=24.9975  amount=10.0   units*openRate=10.00
2024-09-06  units=0.4082  openRate=24.4975  amount=10.0   units*openRate=10.00
```

SCHG's raw price on 2024-09-03 was ~$100; eToro reports `openRate` ≈ $25 (÷4) and `units` scaled
up ×4 to match, with `amount` unchanged at $10.

- **(a) Does eToro adjust `entry_price` on a split, or only `units`? — ANSWERED: both.** eToro
  retroactively adjusts **both** `units` and `openRate` on a split, and preserves `amount` (cost
  basis) exactly. This is correct broker behaviour, and it is also the reason invariant 3 (§4.5)
  exists regardless of the answer.
- **(b) Does `position_id` survive a split? — ANSWERED (inferred, not directly observed): yes.**
  `openDateTime` on the pre-split rows above is still 2024-09-03 — the pre-split date. A
  close-and-reopen would have reset it to the split date; it did not. Positions are therefore
  adjusted **in place**, and the sidecar's `position_id` join key holds across a split. **This is
  inferred from `openDateTime` surviving, not from directly watching a split event happen** — no
  split occurred during the probe window itself.
- **(c) Is eToro's trade history windowed? — still OPEN.** Not exercised by this probe (it only
  read open positions). Tracked as §10.5.

**This refutes part of §4.4's original design.** §4.4 specified `entry_units_at_open` and
`entry_price_at_open` as the *immutable* re-link keys, on the theory that units and entry price
would survive a corporate action unchanged. (a) proves the opposite: eToro mutates both
retroactively, so a value stored at open would silently diverge from the broker's current value —
exactly when a re-link would need to trust it. **The split-invariant quantity is `amount` (cost
basis)**, verified preserved exactly across the SCHG split above and verified `amount == units ×
openRate` on 227 of 227 live positions (§4.5). The DDL (§4.4) has been corrected accordingly:
`entry_units_at_open` and `entry_price_at_open` are replaced by a single `entry_amount REAL NOT
NULL`, and the re-link key is now `(instrumentID, openDateTime, amount)` — all three of which
survive a split — rather than a `(symbol, entry_date, notional)` tuple keyed on a symbol string
that does not exist on a broker position (§4.2).

`corporate_actions` (§7) survives with a narrower job: it is the **tripwire** telling a session
to expect `position_id` churn and run the sidecar re-link path (§4.6b), plus the input to the
ex-dividend seam correction (§8). It is no longer an adjustment input for anything.

### 4.7 Adopting pre-existing positions

**Status: operator decision, 2026-09-04.**

The same live probe (§4.2, §4.5) found 227 open positions predating this app entirely: **202
SCHG and 7 UPRO** — both managed symbols — plus **18 positions across 6 unrelated instruments** (corrected 2026-09-05, §0.1).
Gate 11 invariant 1 (§4.5) requires every open broker position in a managed symbol to have a
sidecar row, else halt. None of the 209 has one. Left alone, the app would halt on its first
session and every session after — this is not a corner case invariant 1 can be relaxed around,
so a migration step is mandatory, not optional.

**The decision: adopt all 209 into `lot_annotations` as fully managed.** Quarantine — recording
the pre-existing positions as visible-but-untouchable (`origin='legacy'`, executor forbidden from
closing them) — was offered as an alternative and considered. The operator declined it: the
executor gets access to all SCHG and UPRO positions, not only positions the app itself opens
going forward.

**Derivable from the broker, exactly as for any position (§4.2):** `position_id`, `symbol` (via
`instrumentID` — there is no symbol string on a position object), `entry_date` (from
`openDateTime`), `entry_amount` (from `amount`). Nothing unusual here; these 209 positions parse
the same way any other broker position does.

**Must be derived from history, not from the broker:** `entry_spy_ath`, `entry_spy_close`,
`entry_spy_drawdown`, `entry_tier` — computed from each position's `openDateTime` against a long
Tiingo SPY series (§2.7). This needs its own justification, because §4.3 forbids exactly this
re-derivation for the same four fields. **§4.3's prohibition does not apply here.** §4.3 exists
because, for an app-created position, a value was already stamped at entry — the provisional T-30
close — and re-deriving from the official close would silently overwrite it with a different
number, at a discrete field (`entry_tier`) where the difference can flip a ladder decision. These
209 positions predate the app: nothing was ever stamped, so there is nothing to overwrite and
nothing to silently shift. Derivation from official closes is not a workaround here — it is the
only source of these fields that exists, and it is legitimate. A future reader comparing this
subsection to §4.3 should not read them as contradicting each other; §4.3 is about protecting a
stamped value from being clobbered, and there is no stamped value to protect for an adopted
position.

**Must be assigned, not derived or stamped:** `book`. `CHECK (book IN ('catapult', 'reentry'))`
cannot be recovered from a broker position or from history — nothing distinguishes which ladder a
pre-existing position belongs to. **All 209 adopted positions default to `catapult`** unless the
operator specifies otherwise per-position. `bucket_id` is left `NULL`: there is no originating
sale for any of these positions to recycle proceeds from, and the column is already nullable
(§4.4).

**The honest consequence.** The ladder's exit logic will make sell decisions about these 209
positions using reconstructed context, not stamped context. `entry_tier` is discrete — a derived
drawdown that lands near a tier boundary may resolve to a different tier than a live stamping at
the time would have produced. This is a direct, accepted cost of the operator's decision to adopt
rather than quarantine, not an oversight in the migration. It is recorded here rather than
smoothed over.

**What does not change.** The 18 positions in the 6 unrelated instruments remain excluded by
instrument filter — they get no sidecar row and are never touched. DESIGN.md §6.D's rule is
unchanged: the executor may only close a `position_id` present in the sidecar. Adoption does not
loosen that rule; it simply means all 209 managed-symbol positions now qualify for a row under it,
where previously none did.

**Sleeve accounting — DECIDED 2026-09-05: adopted positions count in full, no cap.**

**This supersedes an earlier entry made the same day** in this same subsection, which specified an
operator-set sleeve cap with sizing decoupled from adopted equity. That entry was wrong. It is
recorded as superseded rather than deleted, because *why* it was wrong is the useful part.

The operator's decision, in their own words: the pre-existing SCHG stack **"will be transferred to
the strategy. I won't be handling it anymore — the entire point of why we are building
nightwatch."** The DCA stack is not a legacy holding the app has to tolerate; it **is** the core
book the strategy was built to manage.

**Consequence:** `sleeve_equity = sleeve_cash + market value of our UPRO lots + market value of our
SCHG lots` (DESIGN.md §4.5) with adopted lots **fully included**. No cap, no sizing base separate
from `sleeve_equity`, no `is_managed`-based exclusion for sizing. `portfolio_snapshots.is_managed`
(§7) keeps its reporting job of separating the 18 untouched positions in unrelated instruments
from the managed set — it does **not** gate sizing.

**Why the superseded entry was wrong, three ways:**

1. **A cap would have crippled the strategy, not restrained it.** *(This argument was rewritten
   2026-09-05 after the operator corrected the SCHG figure — see §10.11. The original version
   claimed adoption barely moved order size, on an estimated ~$2.6k of SCHG. The real figure is
   **$44,000**, and the conclusion inverts.)* Adopted value is roughly **$48k** — $44,000 SCHG
   (operator-stated) plus $4,039 UPRO (observed, §2.9's app screenshot) — far above the **$7,500**
   sleeve threshold at which `order_size` stops being clamped to the `$50` `min_order_size` floor
   (DESIGN.md §4.5's tier-2 threshold, `strategy/min_allocation`). Adoption is therefore what makes
   the ladder viable at all: **counted**, `sleeve_equity` ≈ $49k gives a first-year budget of
   ~$19.6k and `order_size` ≈ **$327**; **not counted**, `sleeve_equity` is just the $1,000 of cash,
   giving a $400 budget clamped to a `$50` order size and about eight fundable rungs out of sixty.
   A cap would have pinned the strategy near that floor while the capital it was built to deploy
   sat in the same account, untouched.
2. **It re-introduced the property the operator had already rejected.** A fixed absolute figure as
   the sizing base is precisely what inclusion was rejected for (DESIGN.md §4.5: *"it cannot grow
   as the account grows without an explicit, manual edit every time"*), and it is adjacent to the
   `max_sleeve_cash_usd` ceiling DESIGN.md §4.4 records as considered and declined, with the flat
   instruction **"Not an open question; do not re-propose it."**
3. **The real exposure is on the sell side, which a buy-side cap does not touch at all.** See
   directly below.

**The core floor is what actually protects the stack, and counting is the protective choice.**
DESIGN.md §1.4: SCHG is sold *only* to fund a ladder buy, and **never below**
`core_floor_pct × sleeve_equity` = `0.5 × sleeve_equity`. Because adopted lots sit inside
`sleeve_equity`, counting them **raises the floor and thereby defends them**. Excluding them would
compute the floor against a near-zero equity, satisfy it trivially, and leave the entire adopted
stack sellable to fund UPRO buys. The intuition runs backwards here and is worth stating flatly:
**including adopted positions in the sleeve is the conservative option, not the permissive one.**

**A coupling that is not obvious and is not written down in DESIGN.md §4.5 — it should be.**
Raising `cash_reserve_usd` lowers `sleeve_cash`, which lowers `sleeve_equity`, which lowers the
core floor, which makes **more** SCHG sellable. Withholding cash to be cautious quietly makes the
app freer to sell the core book. Anyone tuning the reserve needs to know it moves the floor too.

**`cash_reserve_usd` staying `0.0` — CLOSED, 2026-09-05, no longer a go-live blocker.** It is
`0.0` in the shipped `config/config.toml`, which means the app's cash sleeve is currently **the
entire account cash balance** — in an account that also holds 18 positions across 6 instruments
this app never touches. **This is now confirmed as the operator's deliberate, explicit decision:
the entire account cash balance is intended to be tradable by the strategy.** Measured live figure
as of 2026-09-05: `broker_available_cash = $1,264.59` (§10.11). Under exclusion, any deposit above
the reserve is immediately tradable with no explicit act (DESIGN.md §4.4) — the operator manages
exposure by raising the reserve **before** a deposit lands, per §4.4 above, not by pinning
`cash_reserve_usd` above zero as a standing posture. HANDOFF_2026-09-05.md §6, §7 item 6, and §8
previously listed this as a go-live blocker; it is removed from that list as of this decision.

---

## 5. The guard, radically simplified

The guard survives — with one job instead of three, because its reference became trustworthy.

**Layer 1 — absolute sanity.** Unchanged. Present, finite, positive, above `MIN_PLAUSIBLE_PRICE`.
Costs nothing, needs no network, and is the only layer that works when everything is down. Keep.

**Layer 2 — deviation from the previous adjusted close.** Now meaningful: the reference is
always exactly one session old and correctly adjusted, so the band can be **tight**. Retain
symbol-awareness (UPRO genuinely moves ~3× as hard). The specific tolerances should be set from
the forward test's observed daily-return distribution per symbol, not guessed.

**Layer 3 — gap-aware compounding.** **Deleted.** `compounded_deviation_pct()`,
`_DEVIATION_CEILING_MULTIPLE`, `STALE_REFERENCE_TRADING_DAYS`, and `sessions_elapsed()` all
existed solely to survive gaps that can no longer occur.

**A free internal cross-check, requiring no external vendor.** §2.11 shows eToro's own candles
endpoint is unaffected by the rates-endpoint freeze. Comparing eToro's two own paths —
`rates.lastExecution` against the day's `OneDay`/latest `OneMinute` candle — would have caught
§2.9's bug outright, with zero Tiingo involvement: two eToro reads, one from each path, that
should agree and did not. This costs nothing beyond a request already worth making for other
reasons (§3.1) and should run **regardless of which source is primary for the provisional
close** — it is a sanity check on eToro's own internal consistency, not a substitute for the
Tiingo cross-check in §5.1 below, which validates against an independent vendor.

### 5.1 Resolution protocol for an anomalous tick

At T-30, Tiingo's EOD data for **today does not exist yet**, so it cannot validate today's live
tick. Today's price is single-sourced by necessity. What changed is that the *reference* it is
judged against is now trustworthy.

When the tick breaches the band:

| eToro tick | Independent check | Meaning | Action |
|---|---|---|---|
| Deviates | Confirms the move | Real market move | **Trade.** This is the day the strategy exists for |
| Deviates | Contradicts eToro | Bad tick | **Halt + alert** |
| Deviates | Unavailable | No second opinion | **Halt + alert** |

Row 1 is the one the current flat band gets wrong: it would reject UPRO's genuine −35% on
2020-03-16 as a bad tick, halting on exactly the kind of day the strategy is built to buy.

**Where the independent check comes from is now answered (§10.2, closed): Tiingo's IEX endpoint**,
compared by value against eToro's quote, not by timestamp. Until this design's code lands, rows 2
and 3 collapse together and the honest behaviour remains: **breach the band → halt and alert.**
That is strictly safer than today and never silently degrades.

Whatever the second opinion turns out to be, it must be **fail-open and off the critical path**:
if cross-validation is what *authorizes* an anomalous trade, then its absence must mean no
anomalous trade — never a routine can't-trade-today.

**The protocol is now validated on live data, not just reasoned about.** Comparing Tiingo's
`tngoLast` against eToro's quote (§2.9) is exactly this cross-validation mechanism, run for real,
and it exposed UPRO's 19-hour staleness immediately — the first time this protocol has caught a
live anomaly rather than a hypothetical one.

**Critically, the mechanism that worked is value comparison, not a second timestamp.** It is
tempting to assume the "independent check" could simply compare two freshness timestamps instead
of two prices. It cannot, on Tiingo's free tier: the IEX endpoint's `timestamp` field is
response-generation time, not a trade or quote time, and both `lastSaleTimestamp` and
`quoteTimestamp` are `null` on the free tier. There is no independent freshness signal available —
only an independent *value*, compared against eToro's. Any text elsewhere describing this as a
timestamp cross-check is corrected by this finding; see §10.2.

---

## 6. Gates 9 and 10, specified

**Status as of 2026-09-05.** Gate 9 is still a comment-only stub. Gate 10 is partially built:
items 1 (freshness of the provisional close, including the rates-vs-candles cross-check) and 3
(sanity) are implemented and tested in `engine/preflight.py`; items 2 (window completeness), 4
(corporate actions), and 5 (signal sanity) remain stubs, blocked on the fetch-derive-discard
window assembly (build steps 3–4, §12) that has not been built yet.

### Gate 9 — Calendar

- `exchange_calendars` XNYS via `.sessions` membership (**not** `is_session()`, §2.6).
- Resolve early closes; today's T-30 derives from the actual session close.
- Keep `market/calendar.py` as a cross-check, not the authority. Its two known misses are ad hoc
  closures no algorithm can derive; gate 10 catches those.
- **Known ordering hazard (DESIGN.md §5):** `EASTERN = ZoneInfo("America/New_York")` evaluates at
  *module import*, before any gate runs, so a tz failure crashes the import and no gate can
  report it. Move tz resolution behind a function call, or validate it in gate 4 before any
  calendar module is imported.

### Gate 10 — Price data

1. **Freshness of the provisional close.** This must validate whichever source supplies the
   provisional close — Tiingo IEX `tngoLast` (primary, §3.1) or eToro's candles (fallback,
   §3.1) — not the rates endpoint, which §2.9 and §3.1 establish is never used for this value.
   Where eToro rates are read at all (e.g. for other purposes), `get_rates` already parses
   eToro's timestamp into `Rate.timestamp` (`etoro/client.py:663`); it is dropped in
   `write_provisional`, which never reads it. **Additionally, compare eToro rates against eToro
   candles whenever both are available** (§5's internal cross-check), for every managed symbol
   independently, including SPY, since the stale symbol rotates (§2.9: SCHG stale with the
   market closed on the earlier probe, UPRO stale with the market **open** on 2026-09-04); halt
   on disagreement beyond a configured tolerance. SPY is never the buy instrument, but it drives
   `cone_5_z`; excluding it because "we don't trade it" would leave the signal itself unguarded.
   `_rate_price()` reads `last_execution` or the bid/ask midpoint, so this is exactly the field
   that went stale — and neither guard layer catches it (§5), because a stale price is not
   necessarily an implausible one. **The candles path is self-timestamped at the bar level**
   (each `OneMinute` bar carries its own bar time, §2.11) — a stronger freshness signal than the
   daily candle's date alone, and stronger than trusting a single date field. A failure halts,
   holds the wake lock, and alerts, naming the stale symbol and its age. **On the probe date this
   check would have fired on UPRO's rates path** (153.67, ~19.0h old, a +1.322%/+$2.00 pricing
   error against Tiingo's concurrent read — §2.9) while its candles path (§2.11) would have shown
   a current, agreeing price — exactly the disagreement this check is built to catch.
2. **Window completeness.** The fetched window must contain ≥265 sessions, and its date set must
   match `exchange_calendars`' expected sessions for the range — **excluding today**, whose bar
   does not exist until after the close. A naive date-set comparison would otherwise call today a
   holiday.
3. **Sanity.** Live rate present and non-zero for every managed symbol.
4. **Corporate actions.** No unprocessed `splitFactor != 1.0` or unexpected `divCash` since the
   last session (§4).
5. **Signal sanity.** `|cone_5_z| > 10` aborts (retained from §6.G).

Any failure: **halt, hold the wake lock, alert over Telegram.** An `ERROR` log is insufficient —
this phone lives away from the user.

`missing_trading_days()` (`market/bars.py:550-573`) is promoted from dead diagnostic to gate 10's
window-completeness check, adapted to validate a fetched window rather than a stored one.

---

## 7. Data model changes

**Dropped:** `bars`. With it, `is_provisional`, `settle_bar()`, `settle_previous()`,
`upsert_bar/upsert_bars`, `provisional_bars()`, and the `source` enum
(`candles|live_rate|seed`).

**Replaced:** `lots` → `lot_annotations` (§4). The financial columns — `units`, `entry_price`,
`cost_basis`, `exit_price`, `exit_proceeds`, `realized_pnl` — are dropped because the broker owns
them (§4.2). The strategy columns — `book`, `bucket_id`, `entry_reference_spy`,
`entry_spy_ath`/`entry_spy_close`/`entry_spy_drawdown`/`entry_tier` — survive, keyed by
`position_id` instead of a local autoincrement id (§4.3–§4.4).

**Added:**

```sql
-- Corporate actions. Tripwire only: signals a session to expect position_id
-- churn and run the sidecar re-link path (§4.6b), plus the ex-dividend seam
-- correction (§8). NOT a price-adjustment input, NOT a lot-adjustment input.
-- Dividends and splits are first-class equals -- the generalization
-- activity_server never made (§2.2).
CREATE TABLE IF NOT EXISTS corporate_actions (
    symbol        TEXT NOT NULL,
    date          TEXT NOT NULL,           -- ex-date, ET
    split_factor  REAL NOT NULL DEFAULT 1, -- >1 forward, <1 reverse
    div_cash      REAL NOT NULL DEFAULT 0,
    source        TEXT NOT NULL,           -- 'tiingo'
    detected_at   TEXT NOT NULL,
    processed_at  TEXT,                    -- NULL = re-link path not yet run
    PRIMARY KEY (symbol, date)
) WITHOUT ROWID;

-- Per-session provenance of the fetched window. No price series (§3.4).
CREATE TABLE IF NOT EXISTS price_window_fingerprints (
    trading_date   TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    window_start   TEXT NOT NULL,
    window_end     TEXT NOT NULL,
    bar_count      INTEGER NOT NULL,
    series_sha256  TEXT NOT NULL,          -- over (date, adjClose)
    source         TEXT NOT NULL,
    source_fetched_at TEXT NOT NULL,
    PRIMARY KEY (trading_date, symbol)
) WITHOUT ROWID;
```

**Extended:** `decisions` should record the single provisional close actually used per symbol,
its eToro `Rate.timestamp`, and the resulting `cone_5_z` inputs (mean, stdev, window bounds).
That is a **trade record**, not a price series — it is what answers "why did it buy on this
date?" six months later, and it is required for reconciliation regardless.

Everything else in DESIGN.md §3 is unaffected and remains the reason the SQLite store exists:
`instruments`, `sessions`, `preflight_checks`, `decisions`, `intents`, `orders`,
`buckets`, `strategy_state`, `schg_daily_values`, `budget_history`, `portfolio_snapshots`,
`notifications`, `commands`.

Forward-only migration, per `db/migrations/`.

---

## 8. The seam: adjusted history + raw live tick

The signal is computed on **adjusted** closes. Today's provisional close is a **raw** eToro
quote. These splice correctly, but the reason is worth recording.

Back-adjustment scales *past* prices; the **most recent** `adjClose` equals its raw close
(factor 1.0). So a raw tick appended to an adjusted window is consistent at the seam.

**One exception: an ex-dividend date.** On the morning a dividend goes ex, the raw tick is
post-ex while the prior `adjClose` is pre-ex, producing a ≤0.33% discontinuity for SPY. Small,
but it is a *known* systematic error rather than noise, and `corporate_actions` already carries
`div_cash`. Either adjust the tick by the known dividend on ex-dates, or document the residual
explicitly. Do not leave it implicit.

**A second, separate seam if the provisional close is eToro-sourced.** §2.11(C) establishes that
eToro's own daily closes differ from official (Tiingo) closes by a variable ~0.1% — not a fixed
adjustment factor, since the sign and magnitude both vary day to day, but a different close
convention (venue/timing). Splicing an eToro-sourced provisional close onto a Tiingo-sourced
adjusted window would therefore introduce **two** convention mismatches stacked on top of each
other: the raw/adjusted question this section already discusses, *and* an independent
eToro-vs-Tiingo close-convention gap that has nothing to do with adjustment at all. Neither is
large enough to trip the guard (§5), which is exactly what makes stacking them worth avoiding
rather than accepting.

Taking the provisional close from **Tiingo IEX** instead (§3.1's revised primary source) removes
this second seam entirely — the whole window, history and today alike, is then a single vendor's
convention throughout. This is a major reason §3.1 chose Tiingo IEX over an eToro live read for
the provisional close, independent of the rates-endpoint staleness bug (§2.9) that ruled out the
rates endpoint specifically. The eToro-candles fallback (§3.1) still carries this seam when it is
actually used — a design trade-off accepted for availability, not something eliminated for it.

---

## 9. Dependencies

Add to the Chaquopy `pip { install(...) }` block and root `requirements.txt`:

```
httpx==0.27.2            # already pinned in requirements.txt, MISSING from every pip block
exchange_calendars       # resolves to 4.11.1 on py3.13; see §2.6
```

`numpy` / `pandas` come in as `exchange_calendars` dependencies and are already Chaquopy
prebuilts (1.26.2 / 2.1.3). This **reverses `requirements.txt`'s stated no-pandas policy** — that
comment block should be updated to say so and why, rather than left contradicting the build.
`android-probe/app/build.gradle.kts` already installs numpy and pandas, so the two files are
currently out of sync regardless.

`yfinance` stays **desktop-only** — research, backtests, the forward-test harness, seeding the
initial `corporate_actions` history, and the CI calendar check. Never a runtime dependency of a
trade decision. Its fragility is irrelevant where a human can retry.

`android-probe-deps/` is retained as the dependency-regression harness until cleanup.

---

## 10. Open questions

### 10.1 Are eToro's candles split-adjusted? — ANSWERED

Probed on the z10, 2026-09-04 (§2.9). SCHG daily closes are continuous across the 2024-10-11 4:1
split: 2024-10-10 = 26.2905, next row 2024-10-14 = 26.5901 — ratio 0.989, not ≈4 (2024-10-11
itself returns no row). Absolute levels (~26 in October 2024 against ~35.5 today) are consistent
with post-split-adjusted values throughout the series. **eToro's candles are back-adjusted.**

Depth: `count=1000` was honoured for all three symbols — SPY oldest 2022-08-08, UPRO oldest
2022-07-26, SCHG oldest 2022-07-27, all newest 2026-09-04. ~1000 sessions ≈ 4 years per request.

**Consequence:** eToro candles are usable as an independent cross-check on Tiingo's `adjClose`
*history* (~4 years, one request, already adjusted) — but they still cannot validate *today's*
tick, which remains open (§10.2). **Unverified and not to be assumed: whether eToro also adjusts
for dividends.** The split test does not exercise that, and it is a separate question.

### 10.2 What validates today's live tick? — ANSWERED/CLOSED

Tiingo EOD cannot see today at T-30 (§5.1). **Tiingo IEX is confirmed as the live-tick
cross-check, verified 2026-09-04 against all three symbols with the market open (§2.9, §5.1):**

- Free tier, HTTP 200 for SPY, UPRO, and SCHG.
- `tngoLast` is the usable field for a current-price comparison, and it is what exposed UPRO's
  19-hour staleness against eToro's frozen quote — a **value** comparison, not a timestamp one.
- **Caveat, recorded rather than glossed over:** `timestamp` on the free-tier IEX response is
  response-generation time, not a trade or quote time, and both `lastSaleTimestamp` and
  `quoteTimestamp` are `null`. There is no independent freshness signal on this tier — only the
  price value itself. The resolution protocol (§5.1) must compare prices, not ages, against
  Tiingo.

Superseded candidates from the original list: eToro's own candles remain useful for series
consistency (§10.1) but were never a today's-tick check; "accept single-sourcing" is no longer
the default now that IEX is verified reachable and useful.

### 10.3 Runtime verification on the z10 — HIGH

Everything in §2.5 is build-time resolution. Unverified: that `numpy` 1.26.2, `pandas` 2.1.3,
`exchange_calendars` 4.11.1, and `httpx` 0.27.2 **import and run** on arm64 under Chaquopy.

### 10.4 Tolerance values

Layer-2 bands must be derived from the forward test's observed per-symbol daily-return
distribution, not carried over from the current 20%/60% guesses.

**Rule this section exists to state:** set limits from observation, don't guess. Two concrete
applications of it, recorded 2026-09-05:

**(a) `provisional_close_deviation_pct` — calibrated.** See `config.MarketDataConfig`'s own
docstring and `HANDOFF_2026-09-05_evening.md` §4 / `FINDINGS_window_live_2026-09-05.md` §3: 272
common Tiingo-raw-vs-eToro dates, mean |dev| 0.128%, max 0.640%; set to 1.0% (0.010).
`max_rate_age_minutes` and `rate_vs_candle_deviation_pct` remain provisional, pending the
still-unbuilt forward test (§12 step 7).

**(b) Tiingo's free-tier ceiling — corrected 2026-09-05, from the operator's own dashboard.**
§2.7 recorded the request-count limits (50/hour, 1000/day) from Tiingo's public documentation but
never recorded the account's bandwidth ceiling, and this project had implicitly been sizing its
fetch window against the request quota — the wrong axis. The real, binding limits, confirmed
2026-09-05:

| limit | value |
|---|---|
| bandwidth | **1 GB/month** |
| requests | 1000/day, 50/hour |
| unique symbols | 500/month |

This project fetches 3 symbols once/day: nowhere close to the request or symbol quotas. The
binding constraint, if there is one, is bandwidth — so this is the number that matters for sizing
the fetch window, and it had never been written down before this correction.

**Measured payload sizes** (`HANDOFF_2026-09-05_evening.md` §3):

| bars | span | payload |
|---|---|---|
| 276 | ~13 months | 94 KB |
| 819 | ~39 months | 278 KB |
| 4,184 | 2010-2026 (full SPY history) | 1.42 MB |

Full history daily, one fetch/day, 3 symbols: **~43 MB/month at SPY's own size** (the other two
managed symbols, UPRO from 2009-06-25 and SCHG from 2009-12-11, are shorter series and cost
proportionally less) — **~4.2% of the 1 GB ceiling**, growing only ~2.5 MB/month per additional
year of history (one more year ≈ 252 more bars ≈ 252 × ~340 bytes/bar). **Bandwidth is not a
binding constraint today.** This is why `market/window.py` fetches full history from
`FULL_HISTORY_START_DATE` ("2010-01-01") rather than a rolling window (operator decision,
2026-09-05, superseding the same day's earlier 819-bar/39-month decision) — see that module's own
docstring for the full reasoning, and `doc/DESIGN_ath_staircase.md` for the designed-but-not-built
contingency if this ceiling ever does become binding.

### 10.5 Is eToro's trade history windowed? — ANSWERED 2026-09-05: no. CLOSED

`TradeHistoryEntry` (§4.2) is the only broker source for realized P&L on closed positions. The
question was whether `GET /trading/info/trade/history` retains indefinitely — if not, a closed-lot
summary would have to be persisted locally as a **second sidecar table** alongside
`lot_annotations`.

**Answered against the live account, 2026-09-05, read-only probe from the z10.** With
`pageSize=500` and `MinDate=1970-01-01`, a **single call** returned the account's complete
**120-row** closed-trade history — earliest close **2020-12-21**, latest **2026-08-31**. Nearly six
years, no retention cutoff found, one request. **Long-run realized P&L is reconstructable from the
broker alone; the second sidecar table is not needed.** §4.6c is unblocked.

Two API facts found in the same probe, both of which contradict what the client was coded to do:

- **The query parameters are `MinDate`/`MaxDate`, not `startDate`/`endDate`.** The client sent the
  latter and the live API answers HTTP 400 (`"Missing required query parameter [MinDate]"`). The
  method would have failed outright in production.
- **`MaxDate` is silently ignored server-side.** A `MinDate=2024-08-01, MaxDate=2025-01-01` query
  still returned trades closed through 2026-08-31. Only `MinDate` bounds the result, so **any upper
  bound must be enforced locally** — never assume the server applied it.

The design's self-imposed `MAX_TRADE_HISTORY_DAYS = 364` page-by-year loop was written against a
documented <1yr lookback the live API does not enforce.

### 10.6 UPRO's feed is broken while the instrument is tradable — NEW

§2.9 established both halves of this cleanly: UPRO is fully eligible to trade (§2.9, eligibility
probe) and its quote is simultaneously ~19 hours stale (§2.9, staleness probe). This is not a
transient blip explained by ineligibility — it is a live feed defect on an instrument the account
can actually buy. Two follow-ups, neither resolvable from this design alone:

- **(a) Monitoring** — re-poll UPRO's quote at a later session to see whether the feed
  self-resolves (a stuck cache entry, say) or persists. A single day's evidence cannot distinguish
  "permanent" from "transient," and gate 10 (§6) must be built to catch this regardless of which
  it turns out to be.
- **(b) Whether to report it to eToro** — a judgment call for the user, not an engineering
  decision. Recorded here so it is not lost, not resolved here.

**A workaround now exists, and it strengthens the case for (b).** §2.11's probe found the
candles endpoint returns correct, current data for UPRO at every granularity down to one minute,
at the same instant the rates endpoint served a 19-hour-old quote. This does two things: it gives
the app a working alternative for the provisional close (§3.1), so (a)'s monitoring question no
longer blocks anything the app needs to do; and it sharpens the vendor bug report in (b) — it can
now cite the candles endpoint returning correct data for the same instrument, the same account,
at the same instant the rates endpoint did not, which rules out "the instrument's price is
unknown to eToro" as an explanation and narrows the report to the rates endpoint specifically.

Cross-reference §2.9 and §2.11 for the full evidence.

### 10.7 `_parse_position` reads fields that do not exist — FOUND 2026-09-04, FIXED 2026-09-05

`_parse_position()` (now `etoro/client.py:504-554`, moved from the `:441` line number this
subsection originally cited) mapped `currentRate`, `invested`, `unrealizedPL`, and `symbol` off
each broker position object. A live, non-empty portfolio (227 open positions, probed 2026-09-04)
confirmed **none of these four keys exist** on a live position (§4.2). Every `Position` parsed
from a real portfolio previously carried `current_price=None`, `invested=None`,
`unrealized_pnl=None`, and `symbol=None` — `position_id`, `units`, `entry_price`, `leverage`,
`is_buy`, and `open_date` populated correctly, since those map to fields that do exist.

**Fixed 2026-09-05** (HANDOFF_2026-09-05.md §1, §4 — found the day before, fixed the next
session). The four nonexistent-field reads are dropped; symbol resolution is keyed through
`instrumentID` → `instruments` instead (§4.2). `_parse_position` now raises
`EtoroValidationError` on a missing `positionID`/`instrumentID`/`units`/`openRate`/`amount` rather
than silently defaulting. Confirmed against the current source, not assumed from this note.

**Why this stayed invisible until now:** a demo account holding zero positions exercises
`_parse_position()` zero times — there is nothing to parse, so a wrong field mapping cannot
produce a wrong value, and no test comparing "expected vs. actual" ever ran. This is the same
"check that passes because nothing happened" pattern DESIGN.md §12 and HANDOFF.md §7 warn about:
the bug was not fixed by any amount of demo-account testing, only by reading a genuinely populated
live payload.

### 10.8 DESIGN.md §12's `clientPortfolio` envelope risk — CLOSED

DESIGN.md §12 tracked the `clientPortfolio` envelope-unwrap as unverified against a genuinely
non-empty portfolio. A live, 227-position account closes it: the top-level shape is
`clientPortfolio: {positions: [227], mirrors: [], credit, orders: [1], stockOrders, entryOrders,
exitOrders, ordersForOpen, ordersForClose, ordersForCloseMultiple, bonusCredit}`, and
`get_portfolio` (`client.py:412-430`) checks for `clientPortfolio`, unwraps it, and reads
`positions` — all correct, verified against real data rather than an empty-account stub. This
risk is closed; §10.7 above is the new, unrelated risk the same probe surfaced underneath it.

### 10.9 What the migration (§4.7) does not settle — ANSWERED 2026-09-05

The operator's decision to adopt all 209 pre-existing managed-symbol positions (§4.7) resolved
whether they get sidecar rows at all. The three sub-decisions it left open were put to the
operator explicitly on 2026-09-05 and are now answered. **All three answers are operator
decisions, not engineering findings** — none was derived or verified from data.

- **Where the 202 SCHG positions came from — ANSWERED: a recurring DCA the operator set up
  manually on the eToro side.** *(The "mostly ~$10 each" characterisation this subsection
  originally carried is **contradicted** by the operator's $44,000 SCHG figure and is retracted —
  see §10.11. The position count is measured; the per-position amount is not.)* Not `etoro_trader`, not any other program. **The load-bearing
  half of this answer is that nothing other than this app writes positions to the account
  programmatically**, so gate 11 invariant 1 (§4.5) has no second automated writer to contend
  with. The live consequence is that the manual recurring buy is still active unless the operator
  stops it: each new DCA fill lands as an open SCHG position with no sidecar row, which invariant
  1 halts on. **The migration is therefore not a one-off backfill** — either the recurring buy is
  turned off before the app goes live, or adoption must become a repeatable step. Tracked as
  §10.10; §4.7 as written does not handle it.
- **Whether `catapult` is the right default book for all 202+7 — ANSWERED: yes, confirmed.**
  §4.7's default stands unchanged; all 209 adopted positions enter the catapult ladder. Recorded
  as a decision, not a verification — nothing on the broker or in history distinguishes a ladder
  (§4.3), so there was nothing to check it against.
- **Whether the sleeve-size change is intended — ANSWERED: yes, count adopted positions in
  full.** The stack is being handed to the strategy as its core book, which is the app's whole
  purpose, so the sleeve growing to include it is the intended end state rather than a side
  effect to be capped. An earlier answer the same day said the opposite and specified a cap;
  §4.7's "Sleeve accounting" subsection records why that was wrong and supersedes it.

Cross-reference §4.7 for the migration this bears on, and §10.10 for the recurring-DCA
consequence.

### 10.10 The manual recurring SCHG buy — RESOLVED 2026-09-05: it stops. CONFIRMED STOPPED, 2026-09-05.

**Update, same day: the operator confirmed the recurring buy has actually been switched off on
the eToro side** — not merely decided, but done. This satisfies this subsection's own ordering
constraint below (the DCA must stop before the backfill runs) and **unblocks the §4.7 adoption
migration**. The migration itself has still **never been run** against the live account — that
remains open, tracked in HANDOFF_2026-09-05.md §6/§7 item 1.

Raised from §10.9's first answer: the operator's manual recurring DCA on SCHG is an eToro-side
standing instruction independent of this app, and every fill it produces would be an open position
in a managed symbol with no `lot_annotations` row — which gate 11 invariant 1 (§4.5) halts on. At
roughly two fills a week (202 positions over ~2 years) that is roughly two halted sessions a week.

Three resolutions were put to the operator: **(a)** stop the recurring buy; **(b)** make adoption a
recurring session step, which keeps the DCA running but permanently disarms invariant 1; **(c)**
halt and adopt only on explicit operator confirmation over Telegram.

**Resolved as (a).** The operator is handing the SCHG stack to the strategy and will not be
managing it by hand any more (§4.7). The recurring buy therefore ends, and §4.7's migration is a
genuine **one-shot backfill** — not a recurring adopt loop, and not a per-fill confirmation path.

**Gate 11 invariant 1 stays fully armed, and that is the point.** After the DCA stops, an
unannotated open position in a managed symbol is once again what the invariant was written to
mean: something opened that this app did not open. Halting on it is correct.

**Two build consequences, both easy to get wrong:**

- **Ordering matters.** The recurring buy must be switched off **before** the migration runs, not
  after. A fill landing between the backfill and go-live produces exactly the unannotated position
  the now-armed invariant halts on. The fix if it happens is not a code change — re-run the
  backfill, or adopt the single straggler by hand.
- **Do not build (b) or (c) speculatively.** Neither is wanted. Adding a recurring adopt path
  "just in case" would disarm the invariant that this resolution exists to keep armed.

### 10.11 Sleeve arithmetic — MEASURED 2026-09-05, supersedes two earlier estimates

This subsection has now been wrong twice from estimates and is finally settled by measurement. A
read-only probe of the live account from the z10 (2026-09-05) replaces every figure below.

**History, kept deliberately so the failure mode stays visible.** The first version claimed the
portfolio starts *below* its core floor and that no SCHG could ever be sold to fund a ladder buy —
built on an estimated ~$2,600 of SCHG, itself the product of multiplying a measured position count
by an unmeasured "~$10 each" (§10.12). The second version corrected the SCHG value to the
operator's stated $44,000 but still carried an estimated $1,000 of cash. Both conclusions moved
substantially. **Neither estimate was ever labelled as load-bearing, and both were.**

**Measured figures, live account, 2026-09-05:**

| Quantity | Value | Source |
|---|---|---|
| `credit` | $1,514.59 | measured |
| pending `orders` | $250.00 (1 row) | measured |
| `ordersForOpen` (mirrorID 0) | none | measured |
| **`broker_available_cash`** | **$1,264.59** | computed, eToro's formula |
| `cash_reserve_usd` | 0.0 | shipped config, operator-confirmed |
| `sleeve_cash` | $1,264.59 | derived |
| SCHG — 1,263.502255 units × $35.51 | **$44,867** | measured × candle close |
| UPRO — 26.672246 units × $151.73 | **$4,047** | measured × candle close |
| **`sleeve_equity`** | **~$50,179** | derived |
| Core floor (`0.5 × sleeve_equity`) | ~$25,089 | derived |
| **SCHG sellable above the floor** | **~$19,778** | derived |
| First-year budget (`0.4 × sleeve_equity`) | ~$20,071 | derived |
| `order_size` (`budget / 60`) | ~**$335** | derived |
| `cash_target` (`8 × order_size`) | ~$2,676 | derived |

**What this means:**

- The sleeve is far above the **$7,500** clamp, so `order_size` is unclamped at ~$335 and clears
  SCHG's and UPRO's real `$10.00` `minPositionExposure` comfortably (§10.14 corrects the earlier
  "$50 for SCHG" figure this line used to cite; DESIGN.md §4.3) — and clears the operator-mandated
  `$50` `min_order_size` config floor too, which is a deliberate choice independent of either
  broker minimum (§0.1, §10.14). The minimum-viable-allocation warning in DESIGN.md §4.5 does not
  bite.
- SCHG sits **above** the core floor with ~$19.8k sellable, so **the sell-to-fund path is live**.
- `sleeve_cash` ($1,264.59) is below `cash_target` (~$2,676). Funding that gap by selling SCHG down
  toward the floor is exactly what the sell-to-fund path exists for.
- The 18 unmanaged positions (~$6,790 cost basis across IAU, VONG, OEF, FISV, STZ, MINT — including
  a $5,000 MINT holding) stay excluded by instrument filter and enter none of the above.

**Invariants re-verified in the same probe:** `amount ≈ units × openRate` holds **227/227**, and
`leverage == 1` / `isBuy == true` holds **227/227** — checked across *all* positions, not only
managed symbols. Position counts confirmed exactly: 202 SCHG + 7 UPRO + 18 across 6 others (corrected 2026-09-05, §0.1) = 227.

### 10.12 The per-position SCHG amount — RESOLVED 2026-09-05 by measurement

The contradiction was: 202 measured SCHG positions, characterised in §10.9 as "mostly ~$10 each"
(with three real `amount=10.0` rows quoted in §4.6), against an operator-stated $44,000 holding.
202 × $10 = $2,020 of cost basis cannot become $44,000 in two years.

**Resolved: the position count was right, the per-position amount was wrong.** Measured live
2026-09-05 across all 202 SCHG positions:

| Year opened | n | sum(`amount`) | mean | range |
|---|---|---|---|---|
| 2024 | 28 | $1,330.00 | $47.50 | $10 – $300 |
| 2025 | 68 | $6,289.97 | $92.50 | $10 – $200 |
| 2026 | 106 | $33,247.20 | $313.65 | $100 – $3,497.95 |
| **total** | **202** | **$40,867.17** | **$202.31** | **$10 – $3,497.95** |

Median `amount` is $100, and the distribution is **multi-modal**, not one cluster: 25 rows at $10,
107 near $100, 21 at $250, 31 near $500, 4 at $1,000, and a single $3,497.95. Against $44,867 of
market value, that is a ~10% return on cost basis — entirely ordinary, and nothing like the 21× the
retracted estimate would have required.

**The "~$10 each" figure was extrapolated from the three earliest (2024-09) fills.** The operator's
DCA amount grew roughly 6.6× from 2024 to 2026, so the sample was not merely small — it was drawn
from the least representative end of the series.

**The lesson, which is the reusable part.** This is the same shape as the demo-account probe that
hid the `_parse_position` bug for months (§10.7): in both cases nothing errored, no test failed,
and a number nobody had measured quietly stood in for a fact. It survived because a *measured*
quantity (the 202 count) sat next to an *unmeasured* one (the per-position amount) in the same
sentence, and the sentence read as one finding. **Record provenance per quantity, not per
sentence** — and treat "we probed this" as a claim about specific fields, never about a paragraph.

### 10.13 `_parse_trade_history_entry` reads fields that do not exist — FIXED 2026-09-05

Third instance of the same defect class in two days (§10.7 for `_parse_position`, `get_balance`
earlier the same day, now this). `_parse_trade_history_entry()` (`etoro/client.py`) mapped
`positionID`, `symbol`, `action`, `profit`, `openDateTime`, `closeDateTime` off every trade-history
row. Confirmed against **120 real closed-trade rows** (the same 2026-09-05 probe that closed §10.5):
**none of those six key names exist** on a trade-history row. Every closed trade parsed under the
old code silently carried `position_id=None`, `symbol=None`, `action=""`, `profit=None`,
`open_date=None`, `close_date=None` — `units`, `open_rate`, `close_rate` populated correctly (direct
name matches), and `instrument_id` happened to work only because the old fallback chain
(`instrumentID` or `instrumentId`) caught the lowercase form by accident.

**Casing trap, confirmed across both endpoints:** the portfolio endpoint uses `positionID`/
`instrumentID` (capital `D`); trade history uses `positionId`/`instrumentId` (lowercase `d`). eToro
is inconsistent *across* its own endpoints, not merely internally wrong in one place.

**Two things this silently broke, beyond wrong fields:**

1. §10.5 was closed on the strength of this endpoint being the broker-side source of long-run
   realized P&L — true of the endpoint, but `profit` came back `None` on every row, so the parser
   did not actually deliver it. The conclusion in §10.5 stands; the code did not yet implement it.
2. The `MaxDate` local-filter workaround (§10.5's own fix, same day) was inert: `_closed_on_or_before`
   compares `close_date`, parsed from the nonexistent `closeDateTime` and therefore always `None`,
   and the helper keeps a row when `close_date is None` — so every row was kept and the workaround
   did nothing. Fixed by the same field-name correction; a dedicated test
   (`test_trade_history_filters_maxdate_locally_since_server_ignores_it`) proves the filter now
   genuinely excludes a too-late row.

**`profit` (`netProfit`) is not net of `fees`, despite the name.** Checked across all 120 rows, not
one sample. Adding `fees` to the rate-based P&L makes the fit markedly worse (mean absolute error
3.20 versus 0.0032; 56/120 rows matching within a cent versus 117/120). `fees` is a real, separate
field, and **a caller wanting all-in realized P&L must subtract it explicitly.**

**The aggregate is worth seeing, because the sign flips.** Across these 120 closed trades:
`Σ netProfit = +$327.25`, `Σ fees = −$380.40`. Reading `netProfit` alone reports a **profit** on a
book of trades that actually **lost $53.15** all-in. Any P&L surface — the dashboard (§9), a Telegram
summary, `budget_history` — that reports `netProfit` without fees will be wrong in the flattering
direction.

**The correct closed-trade P&L formula — and a second `units` finding underneath the first.** The
first pass at this called the single mismatching row "a data anomaly unrelated to fees". That was
true but incomplete, and the real finding is more useful. Verified independently across all 120
rows:

| Formula | Matches `netProfit` within $0.02 | Max error |
|---|---|---|
| `investment × (closeRate / openRate − 1)` | **120 / 120** | **$0.0091** |
| `units × (closeRate − openRate)` | 117 / 120 | $398.51 |

**`investment` is exact; `units` is not.** On the worst row (`positionId=1303167355`, closed
2021-09-17), `units × openRate` = $3,633.19 against an `investment` of **$50.00** — a 72×
disagreement. `netProfit` of $5.57 is exactly `$50 × (1149/1034 − 1)`. **4 of 120 rows** have
`units × openRate ≠ investment`.

**This is the mirror image of §4.5's open-position invariant, and the asymmetry is the point.**
`amount ≈ units × openRate` holds **227/227** on open positions but its closed-trade analogue holds
only **116/120**. So the invariant cannot be carried across to closed trades, and gate 11's
reasoning must not be reused there.

**The unifying rule, which §4.4 already reached for a different reason:** across both endpoints, the
**money field** (`amount` on an open position, `investment` on a closed one) is the trustworthy
quantity, and `units`/`openRate` are not. §4.6 established eToro retroactively rewrites `units` and
`openRate` on a split while preserving `amount` exactly — the closed-trade data here is the same
phenomenon seen from the other side, with the rewrite evidently applied inconsistently once a
position is closed. §4.4 chose `entry_amount` as the re-link key on the open-position evidence
alone; this is independent confirmation from a second endpoint that the choice was right.

**Consequence for §10.5's realized-P&L reconstruction:** compute it as
`investment × (closeRate / openRate − 1) − fees`, per closed trade. Do **not** compute it from
`units`, and do **not** take `netProfit` at face value as an all-in figure.

**Fixed:** `_parse_trade_history_entry` now reads the real keys, drops `symbol`/`action` (mirroring
`Position`'s §4.2/§10.7 precedent — resolve `symbol` from `instrument_id` through the `instruments`
table; `is_buy`/`leverage` are the real equivalents of `action`), adds `investment`/`fees`/`is_buy`/
`leverage`, and raises `EtoroValidationError` on a missing `positionId`/`units`/`openRate` rather
than silently defaulting — matching `_parse_position`'s corrected precedent. See
`test_trade_history_entry_matches_real_account_row_shape` and the two
`test_trade_history_entry_raises_on_missing_*` tests in `tests/test_etoro_client.py`.

### 10.14 SCHG's real `minPositionExposure` is $10.00, not $50.00 — and `min_order_size=50` is a deliberate operator choice, not a bug — CORRECTED 2026-09-05

**The factual claim, corrected.** DESIGN.md §1.1/§4.3 and `config/config.toml`'s comment on
`min_order_size` record "live-verified minimums: SCHG $50, UPRO $10 — hence `min_order_size` was
raised 10 → 50 on 2026-09-04." **The $50 figure for SCHG is contradicted by today's live payload.**
The 2026-09-05 on-device eligibility probe (`POST /api/v2/trading/info/eligibility` for SPY, UPRO,
SCHG in one call) reads `minPositionExposure: 10.0` for **both** SCHG and UPRO,
`allowOpenPosition: true` for both, and `maxUnitsPerOrder` 28,321 for SCHG / 13,421 for UPRO. This
is added to the §0.1 superseded-figures table; do not read the $50-for-SCHG claim anywhere else in
this document family as current.

**The deliberate choice, which stands regardless.** The operator has, independently of the above,
**demanded `min_order_size = 50`** in `config/config.toml`. This is not this document (or
`config.toml`'s comment) tracking a stale broker floor — it is a deliberate sizing decision the
operator wants enforced, and it must **not** be "corrected" down to 10 just because the $50 broker
figure that once justified it turns out to be wrong. Both facts have to be read together:

1. The real broker floor is $10.00 for both managed instruments (this subsection).
2. `min_order_size` stays 50 anyway, by explicit operator demand (config value unchanged by this
   correction — only the comment explaining it needs to stop citing a wrong number).

Getting only one of these two facts into a future reader's head is worse than getting neither:
reading only (1) invites "helpfully" lowering `min_order_size` to 10, undoing a decision the
operator made on purpose; reading only (2) invites treating the $50 SCHG figure as still true and
re-deriving other sizing logic (e.g. §10.11's sell-headroom arithmetic, DESIGN.md §4.3's sizing
note) against a number that was never real.

### 10.15 The fee model — SETTLED, 2026-09-05

Full detail in `doc/FINDINGS_fees_2026-09-05.md`; summarised here because it belongs in the design
record, not only in a findings memo.

**Fees are a carrying charge proportional to size × time, not a commission, not dividend-related,
and not a leverage charge.** Correlation of `|fees|` against `investment × holding_days` is
**Pearson r = 0.9918** (vs. 0.9157 for `investment` alone, −0.0611 for `holding_days` alone) across
all 120 closed trades. A scan of all 120 payloads found no dividend- or account-charge-shaped
field anywhere on this API surface. Every fee-bearing row is `leverage = 1`; the 2
leveraged/short trades in the account carry $0.00 of fees, while the other 118 plain 1× long
trades carry the entire `Σfees = -$380.40`.

**`get_costs`' quoted `overnightFee: $0.00` is contradicted by realised `totalFees` for the same
instrument, and must never be treated as evidence.** SCHG's `get_costs` quote reads `$0.00` while
the account's SCHG position has accrued **−$59.17** in realised `totalFees`. This is a quoted rate,
not a measured one — no gate, surface, or future parser audit may treat `get_costs`' fee fields as
ground truth for whether a fee is actually being charged.

**SCHG is not fee-free: 0.320%/yr**, −$59.17 accrued on a $40,867.17 cost basis (§10.12). It lands
on the long-hold core book — the position the strategy is least likely to close — and is currently
**modelled nowhere** in this app. This is flagged as an open item, not something this pass invents
a model for.

**The app's accounting principle, now confirmed rather than merely proposed:** a $100 order
produces a clean, unreduced $100 position — `amount` carries no baked-in fee at entry, ever — and
on exit the realised cash balance must be **read**, never predicted from `openRate`/`closeRate`/
`units`, because the fee accrues with holding time and settles on close, and no formula recovers
it after the fact. This is the specification the executor and any P&L surface must follow: compute
entry from `amount`, and always read realised proceeds from the broker's own post-close cash
figure rather than deriving them.

Instrument **4375** — one of the 6 unmanaged instruments (§0.1, §4.5, §4.7) — carries **81% of all
historical fees** (−$309.61 of −$380.40, across 8 closed trades, a strikingly consistent
2.9–3.8%/yr) and still has a $5,000 position open. Real money, unrelated to Nightwatch's managed
set; identifying it needs a metadata fetch the offline probe could not do. Carried forward as an
open follow-up, not resolved here.

### 10.16 The probe's own parser-audit caveat — high false-positive rate on `missing_from_payload`

The 2026-09-05 device probe's Section C parser audit (RUNBOOK_DEVICE_PROBE.md) produces two
outputs per parser: `missing_from_payload` and `unused_in_payload`. **`missing_from_payload` has a
high false-positive rate** — it flags every key name a parser *references*, including fallback
alternatives inside an `or`-chain, as if each were independently required. `_parse_candle` was
flagged for six "missing" keys this way and is in fact correct; the six names are alternatives
tried in sequence, not six separate required fields. **`unused_in_payload` is the trustworthy
half** — a field the live payload actually carries that no parser reads is a real, actionable
finding. Treat any `missing_from_payload` line as a hypothesis to check against the parser's
source, not as a bug report on its own; do not let a future reader chase these as ghosts.

The genuinely unread fields the same probe surfaced on the eligibility payload — real findings,
not false positives — are `requiresW8Ben`, `leverageConfigs`, `settlementType` (`real` vs. `cfd`),
and `maxUnitsPerOrder`. None of these is consumed by `get_eligibility` today; whoever next touches
gate 8 (instrument minimums, still a stub) should check whether any of them belong in that gate.

### 10.17 Live payload shapes confirmed today, 2026-09-05

Recorded here alongside the other confirmed field shapes (§4.2, §10.7, §10.8, §10.13) so a future
reader has one place to check a payload shape against rather than trusting a parser's assumptions:

- **Candles** nest as `{"interval": ..., "candles": [{"instrumentId": N, "candles": [rows]}]}`,
  with each row keyed `instrumentID`/`fromDate`/`open`/`high`/`low`/`close`/`volume`.
- **The portfolio endpoint returns a bare list** of 227 positions (not wrapped further beyond the
  `clientPortfolio` envelope already documented, §10.8), with keys including `settlementTypeID`,
  `isSettled`, `totalFees`, `totalExternalFees`, `amount`, `openDateTime`.
- **Trade history is likewise a bare list** — see §10.18 below for the parser bug this shape
  caused.

### 10.18 A fifth parser bug — `get_trade_history`'s envelope assumption, not a field inside it

Distinct from every prior instance of this defect class (§10.7 `_parse_position`, `get_balance`,
§10.13 `_parse_trade_history_entry`'s field names, and the truthiness bug in
HANDOFF_2026-09-05.md §4) — this one is about the **envelope**, not a field name inside it.
`get_trade_history` assumed the response body was `{"items": [...]}"` and read
`data.get("items", data if isinstance(data, list) else [])`. **The live body is a bare list**
(§10.17), and the fallback in that expression was dead code: `data.get` is evaluated before its own
`isinstance` fallback is ever considered, so calling `.get` on a bare list raised
`AttributeError: 'list' object has no attribute 'get'` before the fallback could run. Fixed
2026-09-05 by branching explicitly on `isinstance(data, list)` vs. `isinstance(data, dict)` and
raising `EtoroValidationError` on a shape that is neither — see `etoro/client.py`'s
`get_trade_history`, the comment at the `isinstance` branch.

**The pattern is now five instances in this family, not four** — see the updated tally in
HANDOFF_2026-09-05.md §4. **The meta-lesson, specific to this fifth instance:** every pre-existing
test wrapped fixture rows in `{"items": [...]}"` before this fix, so the test suite encoded the
same wrong envelope assumption the production code made, rather than independently checking it —
a fixture that agrees with a bug is not a check on the bug.

### 10.19 Section D re-measure, and the ATH-inclusivity judgment call — RESOLVED 2026-09-05

**Section D of the 2026-09-05 device probe re-confirms §10.11 exactly**: SCHG $44,866.97, UPRO
$4,046.98, `sleeve_equity` $50,178.53, 202 + 7 managed positions. HANDOFF_2026-09-05.md §11's "the
migration should be re-measured immediately before the write" ask is satisfied by this re-measure,
with the caveat carried forward that it should be run **once more** immediately before the
migration's write, since a live account can move between any two reads.

**The ATH-inclusivity judgment call (HANDOFF_2026-09-05.md §8) is resolved in favour of
inclusive.** §4.7 never specified whether SPY's all-time-high "as of" a date includes that date's
own close; `market/adopt.py`'s `build_spy_context_index` chose inclusive (its own docstring:
"the ATH as of a given date is the maximum `adjClose` over every bar up to and INCLUDING that
date, so a new-ATH day itself has `drawdown == 0.0`, not the day after") and flagged it as an
unverified judgment call.

**Verified against the code before asserting this, per this task's own instruction.** The live
session sequence (§4.1 of DESIGN.md, step [5]) computes `cone_5_z` "over 265+ bars ending on
today's provisional close" — i.e. the trailing window used for that day's live decision
**includes today's own (provisional, not-yet-official) close**, not just history through
yesterday. Since §1.4 sources `cone_5_z`, the running all-time high, and the drawdown tier from
the same SPY closes series, the live strategy's own ATH-and-drawdown computation is, by the same
construction, inclusive of today's bar at decision time. `market/adopt.py`'s inclusive convention
therefore makes reconstructed history consistent with what the live strategy actually computes —
the backfill and the live path agree.

**One correction to the rationale as originally proposed, stated so it is not silently smoothed
over:** the proposed rationale characterised the live decision point as "~10 minutes before the
close." That figure is stale — it is the **T-10** timing DESIGN.md §1.1 explicitly says the
design *originally* carried and has since moved away from; the live session fires at **T-30
minutes** (DESIGN.md §4.1, step [3]/[4]/[5]). The correction does not change the conclusion: at
either T-10 or T-30, the decision still necessarily runs before the official close exists, against
a provisional value for the current bar, so the inclusive-vs-exclusive argument is unaffected by
which T-offset is current — only the "~10 minutes" figure in the rationale was wrong, not the
reasoning built on it.

`strategy/params.py`'s `tier_for_drawdown` was also checked: it only maps an already-computed
signed drawdown fraction to a tier index and takes no position on how `ath`/`drawdown` themselves
are computed, so it neither confirms nor contradicts the inclusive convention — it is consistent
with either choice, and does not need to change.

This question moves from open to **closed, resolved as inclusive**, in HANDOFF_2026-09-05.md §8.

---

## 11. z10 probe plan — one session, six questions

**First, second, and third pass run 2026-09-04** (§2.9, §2.10, §2.11, §10.1, §10.2, and the
live-account pass below). Items below are marked with what closed and what is still open;
re-plug the z10 for the remainder.

1. **Runtime imports** (§10.3) — **NOT verified, and the attempt failed.** The 2026-09-04 probes
   ran entirely over device `curl`, not through Chaquopy, so Python was never exercised. A
   subsequent attempt to run the Chaquopy runtime probe **hung on a network call**, with output
   buffered so nothing surfaced before the hang. §10.3 remains open and must be **re-run with
   explicit stdout/stderr flushing and a per-call timeout on every network operation** — the
   probe script itself was the failure this time, not the finding. Once it runs cleanly: install
   `android-probe-deps` and import `numpy`, `pandas`, `exchange_calendars`, `httpx`; confirm
   `.sessions` membership works under pandas 2.1.3 and that `2018-12-05` / `2025-01-09` are
   absent from XNYS sessions **on the device**.
2. **eToro reachability and rate freshness** — **DONE, 2026-09-04.** Private DNS resolves eToro
   on-device (§2.8). Polled `get_rates` for SPY/UPRO/SCHG at market open, across three timestamps
   (10:58/11:11/11:15 ET), and reproduced the staleness finding on UPRO, not SCHG, with a
   quantified error against Tiingo (§2.9). Read-only; no orders.
3. **eToro candle adjustment** (§10.1) — **DONE, 2026-09-04.** SCHG's 2024-10-11 4:1 split is
   continuous in eToro's candles (back-adjusted); `count=1000` returns ~4 years of history for all
   three symbols. See §10.1.
4. **Tiingo from the device** (§10.2) — **DONE, 2026-09-04.** EOD history confirmed for all three
   symbols with correct per-instrument inception dates (§2.7); IEX intraday confirmed free-tier
   and used as the live cross-check that caught UPRO's staleness by value comparison (§5.1,
   §10.2). No rate-limit headers observed (§2.7) — self-throttling is required.
5. **Instrument metadata / eligibility for UPRO** (§2.9) — **DONE, 2026-09-04.** `POST
   /api/v2/trading/info/demo/eligibility` confirms `allowOpenPosition: true` for SPY, UPRO, and
   SCHG alike, resolving the ineligibility hypothesis in full (§2.9). `GET
   /api/v1/market-data/instruments` was also checked and confirmed to carry no tradability
   fields at all (§2.10).
6. **NEW — streaming feed reconnaissance** (§2.10) — **DONE, 2026-09-04.** `ws.etoro.com` is the
   marketing CDN (403 Cloudflare on every path tried); `streaming.etoro.com`, `push.etoro.com`,
   and `api.etoro.com` all reset TLS post-ClientHello. No push alternative to polling exists, or
   none reachable from this network — the SNI-vs-WAF cause is not fully separated (§2.10).
7. **NEW — live-account probe** (§2.9, §4.2, §4.6, §10.7, §10.8) — **DONE, 2026-09-04.** Read-only
   against the real account, market open. Settled four things at once: the rate freeze reproduces
   bit-for-bit on live, not just demo; eToro's own app was then confirmed to show the CORRECT
   live price, localising the defect to the public rates endpoint alone (§2.9); both open questions in §4.6 on split
   behaviour are answered against 15 pre-split SCHG positions (§4.6a, §4.6b); the `clientPortfolio`
   envelope unwrap is verified correct against 227 real positions, closing DESIGN.md §12's tracked
   risk (§10.8); and `_parse_position` is found to read four fields (`currentRate`, `invested`,
   `unrealizedPL`, `symbol`) that do not exist anywhere in the live payload, a bug invisible to any
   amount of demo-account testing (§10.7). Chaquopy runtime imports (§11 item 1 / §10.3) remain the
   only major open probe after this pass.
8. **NEW — candles-as-workaround for the rates freeze** (§2.11) — **DONE, 2026-09-04.** Probed
   whether the rates-endpoint freeze (§2.9) extends to the candles endpoint. It does not: UPRO's
   `OneDay` candle for today and every intraday granularity down to `OneMinute` returned current,
   correct data (151.19, agreeing with eToro's own app and Tiingo) in the same run that the rates
   endpoint still served UPRO's frozen 153.67. Also confirmed eToro's daily closes are not
   official NYSE closes — a variable ~0.1% venue/timing difference against Tiingo, not an
   adjustment artifact. Closes the question of whether a same-API alternative to the rates
   endpoint exists for the provisional close (§3.1) and for gate 10's cross-check (§6).

Read-only throughout. No order placement, no account mutation.

---

## 12. Build sequencing

| # | Step | Why here |
|---|---|---|
| 1 | z10 probe session (§11) | **Partially done, 2026-09-04.** Closed: eToro reachability/rate freshness, eToro candle adjustment, Tiingo device reachability + IEX cross-check, UPRO eligibility, streaming feed reconnaissance, candles-as-workaround for the rates freeze (§11 items 2–6, 8). Still open: Chaquopy runtime imports (§11 item 1) — the probe hung on a network call and needs explicit flushing and per-call timeouts before it can be re-run |
| 2 | Tiingo client (`market/source.py`) + dependency block | Foundation |
| 3 | Drop `bars`, migration, repurpose the store (§7) | Removes the code the rest would otherwise have to be compatible with |
| 4 | Fetch-derive-discard window assembly + fingerprints (§3) | The core |
| 5 | Gate 9 + gate 10 (§6) | Both High risks from §12 close here. **Gate 10 items 1 and 3 were built 2026-09-05, ahead of this plan's ordering** (`engine/preflight.py`) — a pure function over already-fetched data, so it did not need steps 2–4 to land first. Gate 9 and gate 10 items 2/4/5 remain unbuilt and still depend on steps 2–4 as originally planned. |
| 6 | `lot_annotations` sidecar + gate 11 membership/invariant inversion (§4) | Broker becomes authoritative for positions |
| 7 | Simplify the guard; delete layer 3 (§5) | Safe only after 4–6 land |
| 8 | Set tolerances from forward-test data (§10.4) | Needs the forward test |

Steps 3–5 remove both tracked High risks. Step 6 closes the split gap. **Step 7 must come after
4–6** — deleting layer 3 before the history is trustworthy would remove the only thing currently
catching a split.

**Step 5's gate 10 freshness check is now the highest-value item in this entire sequence.** §2.9's
live reproduction — UPRO stale ~19 hours with the market open, on the exact symbol the strategy
trades — means every session run before that check lands is exposed to the failure this design
exists to close. It does not change the ordering above; steps 2–4 remain prerequisites.

---

## 13. What we deliberately did not take from `activity_server`

| Capability | Verdict | Reason |
|---|---|---|
| `db/market_time.py` ET anchoring | **LIFT** | Pure stdlib `datetime`/`zoneinfo`, zero coupling. Strip the `db.config` indirection |
| `split_rebuilder.py` ratio arithmetic | **LEAVE** | Superseded by §4: prices are adjusted by the source, positions by the broker, so no ratio math is performed anywhere |
| New-action detection via pre-fetch cache-max | **PORT** | Generalize to dividends (§2.2); now a tripwire for sidecar re-linking (§4.6b), not an adjustment trigger |
| `fetcher.py` retry/backoff + two-tier error classification | **PORT the shape** | Drop the yfinance-specific exception classes and message strings |
| `stock_price/extractor.py` gap-fill | **LEAVE** | Edge-only, no interior-hole concept. Would import the wrong abstraction — and re-derivation makes it moot |
| `dual.py` `_UPSERT_KEYS` + DELETE-then-INSERT | **LEAVE** | A Snowflake dialect workaround. `repo.py`'s `ON CONFLICT` upserts are already better |
| `db/schema.py` SchemaManager diffing | **LEAVE** | 2000+ lines for a dual-DB drift problem we do not have |
| Turso reentrant lock | **LEAVE** | Different locking model entirely |
| **Its dividend-rebuild blind spot** | **LEAVE — actively** | §2.2. The single most important thing not to inherit |

Honest summary: **one file lifts, two patterns port, six stay behind.** It is as valuable as a
catalogue of what to avoid as of what to copy.

**One lesson banked for step 8 (dashboard):** `repo.py:71` opens SQLite without
`check_same_thread=False`, relying on WAL and `busy_timeout=30000`. Moot today —
`web/app.py` is a stub. When the FastAPI dashboard lands, use **one connection per thread** via
the existing factory. Do not reach for `check_same_thread=False` to share a connection; it
disables sqlite3's own safety check and turns a loud exception into silent corruption. Different
mechanism from activity_server's Turso panics, same lesson.
