from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "android-probe" / "app" / "src" / "main" / "python"))

from probe.payload_gaps import _rows, _subminimum_amount


def test_subminimum_amount_is_below_both_live_minima_and_capped() -> None:
    assert _subminimum_amount(10.0, 10.0) == 1.0
    amount = _subminimum_amount(0.4, 0.2)
    assert 0 < amount < 0.4
    assert amount < 0.2


def test_subminimum_amount_refuses_unknown_or_invalid_minimum() -> None:
    with pytest.raises(ValueError):
        _subminimum_amount(None, 10.0)
    with pytest.raises(ValueError):
        _subminimum_amount(10.0, 0.0)


def test_search_rows_keep_fuzzy_rows_and_require_object_rows() -> None:
    rows = [{"internalSymbolFull": "SPY"}, {"internalSymbolFull": "SPY.RTH"}]
    assert _rows({"items": rows}) == rows
    with pytest.raises(ValueError):
        _rows({"items": ["bad row"]})
