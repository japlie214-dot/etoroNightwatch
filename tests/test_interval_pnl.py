"""Synthetic interval arithmetic and a sanitized broker-shape contract."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from nightwatch.db.repo import PortfolioSnapshot, SnapshotPosition
from nightwatch.engine.interval_pnl import ClosedLot, PartialCloseEvidence, compute_interval, observe


managed = frozenset({3244, 1084})


def pos(id="1", *, instrument=3244, units=10, rate=150, pnl=0, fees=-1,
        amount=1500, is_buy=True, leverage=1):
    return {"positionID": id, "instrumentID": instrument, "isBuy": is_buy,
            "leverage": leverage, "units": units, "amount": amount,
            "totalFees": fees, "totalExternalFees": 0,
            "unrealizedPnL": {"closeRate": rate, "pnL": pnl}}


def snap(positions, *, complete=True, taken_at="2026-10-08T19:00:00+00:00"):
    positions = deepcopy(positions)
    rows = [SnapshotPosition(
        position_id=str(p["positionID"]), symbol=None, instrument_id=p["instrumentID"],
        units=p["units"], entry_price=p.get("openRate"),
        current_price=(p.get("unrealizedPnL") or {}).get("closeRate"),
        amount=p.get("amount"), leverage=p.get("leverage", 1),
        unrealized_pnl=(p.get("unrealizedPnL") or {}).get("pnL"), open_date=None,
    ) for p in positions]
    for p in positions:
        p.setdefault("isBuy", True)
        p.setdefault("leverage", 1)
        p.setdefault("totalFees", 0)
        p.setdefault("totalExternalFees", 0)
    return PortfolioSnapshot(
        session_id=None, taken_at=taken_at, equity=None, cash=None,
        available_cash=None, unrealized_pnl=None, realized_pnl=None,
        positions=rows, raw={"complete": complete,
                            "pnl": {"clientPortfolio": {"positions": positions}}}, id=None,
    )


def compute(prev, cur, **kwargs):
    args = dict(managed_ids=managed, closed=(), entry_marks={}, exit_marks={})
    args.update(kwargs)
    return compute_interval(prev, cur, **args)


def numbers(result, pnl, market, residual):
    assert result.state == "complete"
    assert result.pnl_usd == pytest.approx(pnl, abs=1e-9)
    assert result.explained_usd == pytest.approx(market, abs=1e-9)
    assert result.residual_usd == pytest.approx(residual, abs=1e-9)
    assert result.modelled_residual_usd == pytest.approx(residual, abs=1e-9)
    assert result.reasons == ()


def pending(result, reason, *, state="incomplete"):
    assert result.state == state
    assert result.explained_usd is result.residual_usd is None
    assert not result.alert_a and not result.alert_c
    assert any(reason in r for r in result.reasons)


def test_no_baseline():
    result = compute(None, snap([]))
    assert result.state == "no_baseline"
    assert result.pnl_usd is result.pnl_pct is result.explained_usd is result.residual_usd is None
    assert not result.alert_a and not result.alert_c
    assert result.reasons == ("no previous snapshot",)
    assert result.lines == ()


@pytest.mark.parametrize("fees,pnl,residual", [(-1, -100, 0), (-3, -102, -2)])
def test_held_market_and_fee_delta(fees, pnl, residual):
    result = compute(snap([pos(pnl=-100)]), snap([pos(rate=140, pnl=-200, fees=fees)]))
    numbers(result, pnl, -100, residual)
    assert result.pnl_pct == pytest.approx(100 * pnl / 1400, abs=1e-9)
    assert not result.alert_a and not result.alert_c
    assert result.lines == (f"1 held: pnl {pnl:+.2f}, market -100.00",)


def test_alert_a():
    result = compute(snap([pos()]), snap([pos(rate=90, pnl=-600)]))
    numbers(result, -600, -600, 0)
    assert result.alert_a and not result.alert_c


@pytest.mark.parametrize("pnl,residual,alert", [(-70, -60, True), (-59.99, -49.99, False),
                                                 (-60, -50, True)])
def test_alert_c_and_boundary(pnl, residual, alert):
    result = compute(snap([pos()]), snap([pos(rate=149, pnl=pnl)]))
    numbers(result, pnl, -10, residual)
    assert result.alert_c is alert and not result.alert_a


def test_opened_since():
    cur = snap([pos("2", instrument=1084, units=2, rate=37, pnl=1, fees=-0.5)])
    result = compute(snap([]), cur, entry_marks={"2": 36.6})
    numbers(result, 0.5, 0.8, -0.3)
    assert result.pnl_pct is None
    assert result.lines == ("2 opened since: pnl +0.50, market +0.80",)
    result = compute(snap([]), cur)
    pending(result, "opened since, no independent entry mark")
    assert result.pnl_usd == pytest.approx(0.5, abs=1e-9)
    assert result.lines == ("2 opened since: pnl +0.50, market n/a",)


def test_closed_since():
    prev = snap([pos("3", pnl=-100)])
    lot = ClosedLot("3", 3244, 10, -120, -2, "2026-10-01T15:00:00Z", "2026-10-09T15:00:00Z")
    result = compute(prev, snap([]), closed=(lot,), exit_marks={"3": 148})
    numbers(result, -21, -20, -1)
    assert result.lines == ("3 closed since: pnl -21.00, market -20.00",)
    pending(compute(prev, snap([]), closed=None), "not supplied")
    pending(compute(prev, snap([])), "pending in history")
    result = compute(prev, snap([]), closed=(lot,))
    pending(result, "no independent exit mark")
    assert result.pnl_usd == pytest.approx(-21, abs=1e-9)


def test_round_trip():
    lot = ClosedLot("4", 3244, 5, 10, -1, "2026-10-09T14:00:00Z", "2026-10-09T15:00:00Z")
    result = compute(snap([]), snap([]), closed=(lot,), entry_marks={"4": 100}, exit_marks={"4": 102})
    numbers(result, 9, 10, -1)
    assert result.lines == ("4 round trip: pnl +9.00, market +10.00",)
    pending(compute(snap([]), snap([]), closed=(lot,), entry_marks={"4": 100}),
            "round trip, missing independent mark")


def test_units_integrity_overrides_incomplete():
    result = compute(snap([pos()]), snap([pos(units=12)], complete=False), closed=None)
    pending(result, "units changed 10->12", state="integrity")
    assert result.pnl_usd == 0.0 and result.lines == ()
    pending(compute(snap([pos()]), snap([pos(units=12)])), "units changed", state="integrity")


def test_non_managed():
    result = compute(snap([pos("9", instrument=9999)]),
                     snap([pos("9", instrument=9999, pnl=-9999)]))
    numbers(result, 0, 0, 0)
    assert result.lines == () and result.pnl_pct is None


@pytest.mark.parametrize("kwargs", [{"is_buy": False}, {"leverage": 2}])
@pytest.mark.parametrize("side", ["prev", "cur"])
def test_short_or_leveraged(kwargs, side):
    prev = snap([pos(**kwargs)]) if side == "prev" else snap([pos()])
    cur = snap([pos(**kwargs)]) if side == "cur" else snap([pos()])
    result = compute(prev, cur)
    pending(result, "short or leveraged, not modelled")
    assert result.pnl_usd == 0.0 and result.pnl_pct is None and result.lines == ()


@pytest.mark.parametrize("side", ["prev", "cur"])
def test_incomplete_snapshot_no_partial_threshold(side):
    result = compute(snap([pos()], complete=side != "prev"),
                     snap([pos(rate=149, pnl=-600)], complete=side != "cur"))
    pending(result, "snapshot marks incomplete")
    assert result.pnl_usd == pytest.approx(-600, abs=1e-9)
    assert result.modelled_residual_usd is None


@pytest.mark.parametrize("field", ["closeRate", "pnL"])
@pytest.mark.parametrize("kind", ["held", "opened", "closed"])
def test_missing_mark(field, kind):
    row = pos()
    row["unrealizedPnL"][field] = None
    lot = ClosedLot("1", 3244, 10, -120, -2, None, None)
    prev = snap([]) if kind == "opened" else snap([row])
    cur = snap([]) if kind == "closed" else snap([row])
    pending(compute(prev, cur, closed=(lot,)), "missing mark")


def test_observe_missing_raw_pnl():
    snapshot = snap([])
    del snapshot.raw["pnl"]
    with pytest.raises(ValueError, match="^snapshot has no raw pnl$"):
        observe(snapshot, managed)


def test_real_shape_and_input_immutability():
    pnl = json.loads((Path(__file__).parent / "fixtures/etoro_contracts/pnl_real.json").read_text())
    snapshot = snap(pnl["clientPortfolio"]["positions"])
    snapshot.raw["pnl"] = pnl
    before = deepcopy(snapshot)
    ids = frozenset(p["instrumentID"] for p in pnl["clientPortfolio"]["positions"])
    numbers(compute(snapshot, snapshot, managed_ids=ids), 0, 0, 0)
    assert snapshot == before


def test_observe_signed_external_fees_and_absent_marks():
    snapshot = snap([pos()])
    row = snapshot.raw["pnl"]["clientPortfolio"]["positions"][0]
    row["totalExternalFees"] = -2
    assert observe(snapshot, managed)["1"].fees == -3
    del row["totalFees"], row["totalExternalFees"], row["unrealizedPnL"]
    observation = observe(snapshot, managed)["1"]
    assert observation.fees == 0 and observation.pnl is observation.close_rate is None


@pytest.mark.parametrize("amount,pnl", [(None, 0), (1500, None), (0, 0), (1, -2)])
def test_percentage_unavailable(amount, pnl):
    result = compute(snap([pos(amount=amount, pnl=pnl)]), snap([pos(rate=140, pnl=-100)]))
    assert result.pnl_pct is None


@pytest.mark.parametrize("open_date,instrument", [(None, 3244), ("2026-10-08T19:00:00Z", 3244),
                                                  ("2026-10-01T12:00:00Z", 3244),
                                                  ("2026-10-09T12:00:00Z", 9999)])
def test_other_history_ignored(open_date, instrument):
    lot = ClosedLot("4", instrument, 5, -9999, -1, open_date, None)
    numbers(compute(snap([]), snap([]), closed=(lot,)), 0, 0, 0)


def test_naive_dates_are_utc_and_custom_thresholds():
    lot = ClosedLot("4", 3244, 5, -500, 0, "2026-10-09T14:00:00", None)
    result = compute(snap([], taken_at="2026-10-08T19:00:00"), snap([]), closed=(lot,),
                     entry_marks={"4": 100}, exit_marks={"4": 10},
                     loss_alert_usd=500, residual_halt_usd=50)
    numbers(result, -500, -450, -50)
    assert result.alert_a and result.alert_c
    result = compute(snap([]), snap([]), closed=(lot,), entry_marks={"4": 100},
                     exit_marks={"4": 10}, loss_alert_usd=501, residual_halt_usd=51)
    assert not result.alert_a and not result.alert_c


@pytest.mark.parametrize("residual", [-10, -60])
@pytest.mark.parametrize("slice_id", [None, "slice"])
def test_app_partial_close_and_modelled_loss(residual, slice_id):
    prev = snap([pos("schg", instrument=1084, units=3.621089), pos("upro")])
    cur = snap([pos("schg", instrument=1084, units=1.810544, pnl=999),
                pos("upro", pnl=residual)])
    lot = ClosedLot("slice", 1084, 1.810545, 100, 0,
                    "2026-10-09T14:00:00Z", "2026-10-09T15:00:00Z")
    result = compute(prev, cur, closed=(lot,), partial_closes=(
        PartialCloseEvidence("schg", 3.621089, 1.810544, slice_id),))
    assert result.excluded_positions == ("schg",)
    assert result.state == ("complete" if slice_id else "incomplete")
    assert result.modelled_residual_usd == residual
    assert result.alert_c is (residual == -60)
    assert result.pnl_pct == pytest.approx(100 * result.pnl_usd / 1500)
    assert "schg app partial close 3.621089->1.810544: excluded from residual" in result.lines
    assert "position schg: app partial close excluded" in result.reasons
    if slice_id:
        assert result.pnl_usd == result.residual_usd == residual
        assert not any("round trip" in line for line in result.lines)
    else:
        assert result.pnl_usd == residual + 100
        assert result.residual_usd is result.explained_usd is None
        assert "position slice: round trip, missing independent mark" in result.reasons


def test_all_partial_positions_excluded_and_endpoint_tolerance():
    result = compute(snap([pos(units=8)]), snap([pos(units=6)]), partial_closes=(
        PartialCloseEvidence("1", 8.0000019, 5.9999981, None),))
    assert result.state == "complete"
    assert result.pnl_usd == result.explained_usd == result.residual_usd == result.modelled_residual_usd == 0
    assert result.pnl_pct is None
    assert result.excluded_positions == ("1",)
    assert not result.alert_a and not result.alert_c


@pytest.mark.parametrize("before,after,evidence", [
    (6, 4, (PartialCloseEvidence("1", 8, 6, None),)),  # stale
    (8, 4, (PartialCloseEvidence("1", 8, 6, None), PartialCloseEvidence("1", 6, 4, None))),
    (3.62, 1.81, (PartialCloseEvidence("1", 3.62, 1.80, None),)),
    (8, 6, (PartialCloseEvidence("other", 8, 6, None),)),
    (8, 6, (PartialCloseEvidence("1", 8, 6, None),) * 2),
    (6, 8, (PartialCloseEvidence("1", 6, 8, None),)),
    (8, 6, ()),
    (8, 6, (PartialCloseEvidence("1", 8.0000021, 6, None),)),
    (8, 6, (PartialCloseEvidence("1", 8, 6.0000021, None),)),
])
def test_partial_close_nonmatching_evidence_is_integrity(before, after, evidence):
    result = compute(snap([pos(units=before), pos("upro")]),
                     snap([pos(units=after), pos("upro", pnl=-60)]), partial_closes=evidence)
    pending(result, "units changed", state="integrity")
    assert result.excluded_positions == ()
    assert result.modelled_residual_usd is None


def test_already_reflected_partial_close_is_not_used():
    evidence = (PartialCloseEvidence("1", 3.62, 1.81, "slice"),)
    result = compute(snap([pos(units=1.81)]), snap([pos(units=1.81)]), partial_closes=evidence)
    numbers(result, 0, 0, 0)
    assert result.excluded_positions == ()


@pytest.mark.parametrize("residual", [-60, -10])
@pytest.mark.parametrize("unmodelled", ["held", "opened", "closed", "round_trip", "short", "leveraged"])
def test_row_incompleteness_keeps_only_paired_modelled_terms(residual, unmodelled):
    prev, cur = [pos("upro")], [pos("upro", pnl=residual)]
    lots = ()
    row = pos("other", instrument=1084)
    if unmodelled == "held":
        prev.append(row)
        cur.append(pos("other", instrument=1084, pnl=None))
    elif unmodelled == "opened":
        cur.append(pos("other", instrument=1084, pnl=100, fees=0))
    elif unmodelled in ("closed", "round_trip"):
        if unmodelled == "closed":
            prev.append(row)
        lots = (ClosedLot("other", 1084, 10, 100, 0,
                          "2026-10-09T14:00:00Z", "2026-10-09T15:00:00Z"),)
    else:
        row["isBuy"] = unmodelled != "short"
        row["leverage"] = 2 if unmodelled == "leveraged" else 1
        prev.append(row)
        cur.append(row)
    result = compute(snap(prev), snap(cur), closed=lots)
    assert result.state == "incomplete"
    assert result.explained_usd is result.residual_usd is None
    assert result.modelled_residual_usd == residual
    assert result.alert_c is (residual == -60)
    if unmodelled == "round_trip":
        assert result.pnl_usd == residual + 100


@pytest.mark.parametrize("kind", ["opened", "closed"])
def test_modelled_opened_and_closed_rows_remain_eligible(kind):
    prev, cur = [pos("missing", pnl=None)], [pos("missing", pnl=None)]
    lots, entry, exit = (), {}, {}
    if kind == "opened":
        cur.append(pos("modelled", rate=150, pnl=-60, fees=0))
        entry = {"modelled": 150}
    else:
        prev.append(pos("modelled", fees=0))
        lots = (ClosedLot("modelled", 3244, 10, -60, 0, None, None),)
        exit = {"modelled": 150}
    result = compute(snap(prev), snap(cur), closed=lots, entry_marks=entry, exit_marks=exit)
    assert result.state == "incomplete"
    assert result.modelled_residual_usd == -60
    assert result.alert_c


def test_history_unavailable_disables_modelled_subset():
    result = compute(snap([pos()]), snap([pos(pnl=-60)]), closed=None)
    pending(result, "trade history not supplied")
    assert result.modelled_residual_usd is None
