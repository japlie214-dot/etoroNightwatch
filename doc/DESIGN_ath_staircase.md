# DESIGN: the ATH staircase — a designed, NOT built, bandwidth contingency

**Status: DESIGNED, NOT BUILT.**
**Date: 2026-09-05.**
**Why deferred:** Tiingo's confirmed free-tier bandwidth ceiling is **1 GB/month**
(`DESIGN_market_data.md` §10.4, from the operator's own dashboard, 2026-09-05). The live app's
current design — full SPY history fetched from `FULL_HISTORY_START_DATE` ("2010-01-01") every
session (`market/window.py`) — costs **~43 MB/month** (measured: 4,184 bars = 1.42 MB/fetch,
1 fetch/day), **~4.2% of the ceiling**. Bandwidth is not scarce. This document exists so that if
it ever becomes scarce, the fix is a design already reasoned through, not a scramble.

This is insurance, not a roadmap item. Nothing below should be built unless the trigger condition
in §6 is actually met.

---

## 1. The problem this solves, restated precisely

`market/window.py` fetches SPY's full history from 2010-01-01 every session so that the
session's ATH/drawdown/tier computation (`_spy_context_with_provisional`) is a **true** all-time
high, not a rolling-window approximation that can silently understate drawdown when an old peak
ages out of a shorter window (`FINDINGS_window_live_2026-09-05.md` §2 — the exact bug this
project already shipped and fixed once). Full history is the simplest way to guarantee that: there
is no window to age out of.

The cost of "simplest" is a payload that grows every year: ~1.42 MB today, ~+2.5 MB/year as more
history accrues. If Tiingo's free tier ever shrinks — a lower monthly cap, a lower per-request
cap, or the free tier disappearing in favour of a metered one — this project needs a fetch that
costs a small, roughly *constant* amount per day rather than a slowly *growing* one, while still
producing a true, not approximate, ATH. That is what the staircase is for.

## 2. The concept

Maintain a small, monotone, persisted list of SPY's **record-high raw closes**:

```
ath_staircase(date, raw_close)
  2010-01-04   113.33
  2010-04-15   121.24
  2010-04-23   122.15
  ...
  2020-02-19   339.08
  2021-08-31   453.19  (post-adjustment for splits, see §4)
  2022-01-03   477.71
  2024-...     ...
```

Each row is a date on which SPY's raw close reached a **new** all-time high — i.e. every row
already satisfies `close > every prior close`. The list is monotonically increasing in both date
and value by construction. It is bootstrapped once, in full, from a single full-history fetch
(exactly the fetch `market/window.py` already makes today), by scanning the whole raw-`close`
series for `bar.close > running_max` and keeping only those rows.

**Daily operation once bootstrapped:**

```
today_ath = max(staircase[-1].close, max(bar.close for bar in short_rolling_window))
```

The "short rolling window" only needs to cover what `cone_5_z` itself requires
(`strategy.cone.REQUIRED_HISTORY_BARS` = 265, ~280 calendar days with holiday margin — the exact
figure `market/window.py` fetched before the 39-month/819-bar and full-history revisions). The
staircase's own last entry already IS the true ATH as of its own date; folding in the short
window's own max (which necessarily includes every session between the staircase's last entry and
today) recovers the running max exactly, without ever re-fetching the full history.

**Daily fetch cost with the staircase:** ~280 bars ≈ 95 KB/day ≈ **~2.9 MB/month** — a **~15×**
reduction from full history's ~43 MB/month, and one that does not grow year over year (the short
window's size is fixed; only the staircase, a few hundred bytes at most, grows).

## 3. Why this is legitimate against §2.2's prohibition — the central argument

`DESIGN_market_data.md` §2.2 is explicit and is not being relitigated here: **accumulating price
history on the phone is forbidden**, because an accumulated store of `adjClose`-style values
becomes a patchwork of dividend-adjustment epochs — every dividend retroactively rescales all
prior prices, so an append-only store of adjusted values silently drifts, and no rebuild logic
fixes a category error at the root. That is real, and it is why `market/window.py`'s whole design
is fetch-derive-discard.

**The staircase does not violate this, and the reason is not a hand-wave — it rests on two
specific, checkable facts:**

**(a) The staircase is built from raw `close`, which is never retroactively revised.**
`HANDOFF_2026-09-05_evening.md` §4 (implemented in this same round of fixes) split the codebase's
price basis: `cone_5_z` uses `adjClose` (a *return*, where the dividend adjustment is exactly the
correction you want), while ATH/drawdown/tier — the staircase's entire domain — use raw `close` (a
*level*, compared against traded prices, matching what eToro's own candles track). §2.2's failure
mode is specifically that an *adjusted* value changes retroactively as new corporate actions
accrue after the bar's own date. A raw `close` has no such property: Tiingo's raw close for
2015-03-02 is the same number today as it will be in ten years, because it is (subject to §4
below) simply what SPY traded at that day — a historical fact, not a function of everything that
has happened since. The "stale dividend-adjustment epoch" objection that makes an accumulated
*adjusted* series unsafe **does not apply to a raw-close series at all.**

**(b) A max over a closed, past interval is a pure function of history — a CACHE, not accumulated
state.** This is the distinction that actually matters, stated precisely: *accumulated state*
(what §2.2 forbids) is data that **drifts** — its correct value today depends on information that
did not exist when it was written, so a value written yesterday can silently become wrong today
without anyone touching it, and there is no way to tell a stale row from a fresh one by looking at
it. A *cache* is data that is **re-derivable and verifiable** — it has one correct value, fixed
forever once its input interval is closed, and that value can always be recomputed from scratch
and compared byte-for-byte against what is stored. `max(SPY raw close, 2010-01-01..2022-01-02)`
is a fixed number the day 2022-01-03 begins; it is never going to be a different number next year,
next decade, or ever (again, subject to §4's split guard) — because "the maximum raw close over an
interval that has already ended" does not depend on anything that has not happened yet. Storing
the *staircase* (the sequence of running-max updates) rather than the *raw series itself* also
means this is not a disguised re-implementation of "store the price history": the staircase is
strictly smaller than the series it summarizes (its own length is the count of NEW highs, not the
count of trading days — a few hundred rows over 16 years, not several thousand), and every row it
contains is independently checkable against a fresh fetch (§5).

Put together: §2.2 forbids storing something that can go stale silently and unrecoverably. A raw
running-max over closed history cannot go stale (fact a) and can always be re-verified against a
fresh fetch (fact b, operationalized in §5). It is the same category as `price_window_fingerprints`
(§3.4, migration 004) — already-accepted cheap derived state, not the forbidden price series
itself — not the same category as `activity_server`'s `auto_adjust`-poisoned cache (§2.2's own
counter-example).

## 4. The split guard

Raw closes are stable **except** across a stock split, which rebases every price before the split
date by the split ratio. This is not hypothetical for this project: a real SCHG split (2024-10-11,
4:1) has already been observed and verified against a live account
(`HANDOFF_2026-09-04_market_data.md` §2.1, §4.6 — eToro retroactively rescaled both `units` and
`openRate` on 15 pre-split positions). SPY itself has had a 2:1 split before (2007) but none since
Tiingo's usable history begins (2010); UPRO and SCHG (the other two symbols this project fetches,
though the staircase as specified here is SPY-only) split on a documented ~2-3 year cadence
(`HANDOFF_2026-09-04_market_data.md` §2.1).

Tiingo's EOD payload carries `splitFactor` on every bar (`market/source.py`'s `EodBar.split_factor`,
already parsed). The guard:

1. Every daily fetch reads `splitFactor` for the bars it retrieves.
2. If any bar in the short rolling window has `splitFactor != 1.0`, the staircase built before that
   date is now expressed in a different scale than the live raw-close series — **invalidate it and
   re-bootstrap in full** (one full-history fetch, exactly like the initial bootstrap).
3. Never patch the staircase in place by multiplying old rows by the split ratio. That is exactly
   the "silent rebuild that fixes a category error" §2.2's `activity_server` post-mortem warns
   against — a fresh full fetch is cheap (one fetch, same cost as today's daily fetch) and
   verifiably correct; an in-place rescale is one more thing that can be subtly wrong and would
   defeat the entire "cache, re-derivable and verifiable" argument in §3.

## 5. Periodic re-verification

Even without a detected split, re-fetch full history **monthly** and assert the staircase still
matches exactly (every stored `(date, close)` pair reproduced, no missing new-high row, no
extra row). A mismatch with no split detected is itself a finding worth halting on — either a
Tiingo data revision nobody expected, or a bug in the staircase maintenance logic — and should be
treated with the same "never fail silently" posture as every other guard in this codebase. This
re-verification is the direct, cheap way facts (a) and (b) in §3 stay checked rather than merely
argued: the whole legitimacy case rests on the staircase being reproducible from a fresh fetch, so
periodically actually reproducing it and comparing is not optional hygiene, it is the proof.

Cost: one full-history fetch per month (~1.42 MB today, growing slowly) on top of the ~2.9 MB/month
daily cost — call it **~4.3 MB/month all-in**, still a large reduction from full-history-daily's
~43 MB/month.

## 6. The bonus capability

Because the staircase is keyed by date and monotone, it directly answers **"what was the ATH as
of any past date D?"** — the value is simply the last staircase row with `date <= D` (or, if none
exists, SPY had not yet traded / had no recorded high before D). This is exactly the query
`market/adopt.py`'s adoption migration needs (`build_spy_context_index`, which today rebuilds this
same running-max from a full fetch every time it runs) and exactly what a sandbox/backtest replay
needs when it wants "the ATH the live app would have seen on trading day D" without re-fetching
and re-scanning full history for every date it wants to check. Today, with the full-history-fetch
design, this capability already exists implicitly (any full fetch can answer it) — the staircase's
contribution is making that query *cheap* (a lookup, not a fetch-and-scan) once the daily fetch no
longer carries the full series with it.

## 7. Trigger condition — when to actually build this

**Build the staircase if Tiingo's free-tier bandwidth cap drops below roughly 215 MB/month.**

Derivation, shown rather than asserted: full-history-daily costs ~43 MB/month today
(`DESIGN_market_data.md` §10.4). A reasonable operating margin — the same kind of headroom
reasoning this project already applies to its other tolerances (§10.4's own calibration of
`provisional_close_deviation_pct` left ~56% headroom above an observed maximum) — is to never let
ordinary usage exceed roughly a fifth of the quota, so a transient spike (a re-run, a second
symbol's history, a bug that double-fetches) still can't blow the budget. `43 MB / 0.20 ≈ 215 MB`.
If the free tier's bandwidth cap is ever reduced to somewhere near or below that figure, full
history's ~43 MB/month stops being comfortably small and the staircase's ~4.3 MB/month (a further
~10× margin under the same 20%-of-quota heuristic, i.e. safe down to caps as low as ~22 MB/month)
becomes worth its added complexity.

Two things to note about this threshold:

- **It is far below today's 1 GB cap** (215 MB is ~21% of 1 GB) — this is not a "we are already
  close" warning, it is a number to watch if Tiingo's *policy* changes, not a number this project
  is approaching through its own growth. At ~2.5 MB/month of organic growth per year, it would
  take roughly **70 years** of accumulating SPY history for full-history-daily's own growth alone
  to reach 215 MB/month — growth is not the risk; a tier policy change is.
- **Re-derive this number if any input changes**: a change to how many symbols are fetched daily,
  a change to the rolling-window size `cone_5_z` needs, or a revised operating-margin heuristic
  should all flow through to a revised trigger figure — this is meant to be recomputed, not quoted
  forever.

## 8. What this document is not

This is a design, not an implementation plan with file paths and function signatures — building it
prematurely (before the trigger in §7 is actually met) would itself be exactly the kind of
speculative complexity this project's own decisions elsewhere have repeatedly declined (e.g.
`DESIGN_market_data.md` §4.7's explicit "Not an open question; do not re-propose it" on a related
speculative-cap idea). If the trigger is met, the next step is a proper implementation design
(storage location — likely a small new SQLite table alongside `price_window_fingerprints`,
migration numbering, the exact re-verification schedule/mechanism, and how `market/adopt.py` and a
future sandbox/backtest would consume the staircase's date-keyed lookup) — not yet written,
because it is not yet needed.
