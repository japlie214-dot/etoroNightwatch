# Findings — first live run of the window assembly (2026-09-05)

Section F of the on-device probe, real account, read-only. First time `market/window.py` and
`strategy/cone.py` met a live payload; both were fakes-only until now.

## 1. What passed

- **SPY resolves on eToro.** Previously unknown — SPY is signal-only and had never been fetched
  from eToro. The eToro-candle fallback for the provisional close therefore exists for SPY.
- **Window completeness passes against the real calendar**: 276 sessions, 2025-08-01..2026-09-04,
  `missing_sessions: []`, `unexpected_dates: []`, exact XNYS match.
- **A real signal was produced end to end**: provisional close 770.19 (source `tiingo_iex`),
  `cone_5_z = 0.24263565302483447`, ATH 777.88, drawdown -0.9886%, tier 0.
- Fingerprint computed over 276 bars; raw payloads saved.

## 2. BUG — the "all-time high" is a 276-bar high

`market/window.py:417-431` computes `running_ath = max(bar.adj_close for bar in window)` over the
**fetched window only** (~13 months). The strategy this was ported from accumulates
`_ath = max(_ath, spy)` across its **entire** dataset. These are not the same quantity.

Consequence: whenever the true peak is older than the window, the window loses it, the measured
drawdown is **shallower than reality**, `tier_for_drawdown` returns a **lower** tier, and
`TIER_CAPS` — cumulative ceilings on deployed budget — permit **less** capital exactly in the
prolonged drawdowns the deep tiers exist for. The error is in the dangerous direction.

**Measured over 4,184 SPY bars (2010-2026), true running ATH vs a 276-bar rolling max:**

| | |
|---|---|
| days the windowed ATH understates the true drawdown | 248 (**5.9%**) |
| days the resulting **tier** differs | 67 (**1.6%**) |
| tier gap, when it differs | always exactly **1** |
| max drawdown understatement | **10.04 percentage points** |

Worst observed episode — Feb 2023, after the Jan-2022 peak aged out of the window:

```
2023-02-28  true dd = -17.05% (tier 2)   windowed dd = -14.15% (tier 1)
```

**This is a floor, not a ceiling.** The dataset starts in 2010, so it excludes 2008 (a ~17-month,
-55% decline) and 2000-2002 (~30 months). Both are longer than a 276-bar window and would produce
larger gaps than anything measured above.

**Fix direction (not yet implemented).** Do **not** persist a running ATH across sessions — that
reintroduces exactly the accumulated state §2.2 exists to avoid, and a stored ATH from an older
dividend-adjustment epoch would be in stale units. Instead **fetch a longer window in the same
single fetch** (one fetch is still one epoch, so the §2.2 guarantee is preserved) and take the ATH
from it, while `cone_5_z` continues to use its 265-bar tail. Cost is bandwidth: the 276-bar
payload was 94,009 bytes (~340 B/bar), so full history (~4,200 bars) is ~1.4 MB per fetch.

## 3. BUG — the Tiingo/eToro cross-check compares unlike quantities

Tiingo returns **both** `close` and `adjClose`. `window.py` uses **`adjClose`** (and the
fingerprint hashes `(date, adjClose)`), but section F's cross-check compared Tiingo's **raw
`close`** against eToro. On 2025-08-01:

```
Tiingo close    621.72
Tiingo adjClose 614.92
eToro  close    620.83
```

**eToro's candles track Tiingo's RAW close, not `adjClose`** — 0.14% apart versus 0.95%.

Measured across the 272 overlapping dates, Tiingo raw vs eToro: mean |dev| **0.128%**, median
0.124%, max **0.640%**. The signed deviation **decays monotonically toward the present** —
+0.153% (2025-Q3), +0.139% (Q4), +0.087% (2026-Q1), +0.007% (Q2), +0.021% (Q3) — which is the
dividend-adjustment signature: older bars diverge more because more dividends have accrued since.

**Why it matters.** `config.toml` sets `provisional_close_deviation_pct = 0.0075` (0.75%). A
cross-check of an `adjClose`-derived value against an eToro candle would diverge ~0.95% on a
13-month-old bar and **breach that tolerance**, while raw-vs-raw sits at 0.128%. Today's bar is
safe either way (adjustment accumulates backwards, so it is ~0 at the present edge), which is why
gate 10 item 1 has not tripped — but the gate must be explicit that it compares **like with like**,
and must never compare an adjusted historical value to an eToro candle.

The max raw-vs-eToro deviation of 0.640% also sits uncomfortably close to the 0.75% tolerance.
That tolerance is documented as provisional (§10.4: set it from the forward test's observed
distribution, do not guess) — this is the first real distribution to set it from.

## 4. Follow-ups

1. Fix the ATH window (§2). Highest priority — it silently under-deploys in crashes.
2. Make the adjusted-vs-raw distinction explicit wherever Tiingo and eToro are compared (§3), and
   decide which basis the strategy uses throughout. `cone_5_z` on `adjClose` and an ATH on raw
   close would be incoherent.
3. Revisit `provisional_close_deviation_pct` using §3's measured distribution rather than the
   placeholder.
