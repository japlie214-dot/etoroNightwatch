"""The cone_5_z signal (DESIGN §1.4). Built in step 4.

# TODO(step 4): Port, on SPY daily closes:
#
#   cone_5_z[t] = (C[t]/C[t-5] - 1) / stdev_pop( C[i]/C[i-5] - 1 , i = t-259..t )
#
# First valid value needs 265 bars (260 lookback + 5 shift); returns None
# before that (DESIGN §1.4) — engine/preflight.py gate 10 is what actually
# blocks trading on insufficient history, this module just returns None
# faithfully rather than raising or fabricating a value.
# CONE_HORIZON (5) and CONE_CENTER_MODE ('close'|'mid') are config-driven
# (nightwatch.config.StrategyConfig) — do not hardcode 5 here.
"""
