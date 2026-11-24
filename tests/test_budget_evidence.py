"""T073: durable annual-budget evidence and daily SCHG sampling, through the real dry-run engine."""

from dataclasses import replace
import math

import pytest

from nightwatch.engine import session
from nightwatch.strategy.ladder import LadderInputs
from .test_session_decide import config, prepare_session  # noqa: F401  (fixtures)
from .test_session_market import DAY, FakeTiingo

ISO = DAY.isoformat()


def run(repo, host, config, client):
    return session.run_session(host=host, config=config, repo=repo, client=client,
                               tiingo=FakeTiingo(), trading_date=DAY, wait=False)


def evaluation(repo, result):
    return repo.get_budget_evaluation(result.session_id)


def seed_state(repo, *, budget_year, budget=2000.0):
    repo.save_strategy_state(replace(
        repo.get_strategy_state(), ath=100.0, budget_year=budget_year, budget=budget,
        order_size=budget / 20, cash_target=budget / 7))


def seed_history(repo, year, values):
    for i, value in enumerate(values, start=1):
        repo.upsert_schg_value(f"{year}-03-{i:02d}", value)


def test_bootstrap_records_its_path_and_a_verified_empty_book_as_a_real_zero(repo, host, config, monkeypatch):
    result = run(repo, host, config, prepare_session(host, monkeypatch))
    ev = evaluation(repo, result)
    assert ev["source_path"] == "bootstrap" and ev["state_budget_year"] is None
    assert ev["target_year"] == DAY.year and ev["history_year"] == DAY.year - 1
    assert ev["sample_count"] == 0 and ev["sample_mean"] is None
    assert ev["sleeve_equity"] == 1000.0 and ev["rounding_step"] == 20.0
    assert ev["core_fraction"] == config.strategy.core_fraction
    assert ev["budget"] == math.floor(ev["core_fraction"] * 1000.0 / 20.0) * 20.0
    log = repo.get_schg_sampling(ISO)
    assert log["status"] == "recorded" and log["value"] == 0.0 and log["reason"] is None
    assert repo.conn.execute("SELECT value FROM schg_daily_values WHERE date = ?", (ISO,)).fetchone()[0] == 0.0
    assert "schg sample recorded" in result.reason


def test_rollover_with_prior_year_history_uses_the_observed_mean(repo, host, config, monkeypatch):
    seed_state(repo, budget_year=DAY.year - 1)
    seed_history(repo, DAY.year - 1, [10_000.0, 20_000.0, 30_000.0])
    ev = evaluation(repo, run(repo, host, config, prepare_session(host, monkeypatch)))
    assert ev["source_path"] == "prior_year_mean" and ev["state_budget_year"] == DAY.year - 1
    assert ev["sample_count"] == 3 and ev["sample_mean"] == 20_000.0
    assert ev["budget"] == math.floor(ev["core_fraction"] * 20_000.0 / 20.0) * 20.0


def test_rollover_without_history_says_it_fell_back_and_why(repo, host, config, monkeypatch):
    seed_state(repo, budget_year=DAY.year - 1)
    result = run(repo, host, config, prepare_session(host, monkeypatch))
    ev = evaluation(repo, result)
    assert ev["source_path"] == "missing_history_fallback"
    assert ev["sample_count"] == 0 and ev["sample_mean"] is None
    assert ev["budget"] == math.floor(ev["core_fraction"] * ev["sleeve_equity"] / 20.0) * 20.0
    assert "no prior-year SCHG history recorded" in result.reason


def test_available_history_is_not_claimed_when_the_budget_was_retained(repo, host, config, monkeypatch):
    seed_state(repo, budget_year=DAY.year, budget=3000.0)
    seed_history(repo, DAY.year - 1, [10_000.0, 20_000.0])
    ev = evaluation(repo, run(repo, host, config, prepare_session(host, monkeypatch)))
    assert ev["source_path"] == "retained"
    assert ev["sample_count"] == 2 and ev["sample_mean"] == 15_000.0  # present, but did not drive it
    assert ev["budget"] == 3000.0


def test_bootstrap_with_history_still_reports_bootstrap(repo, host, config, monkeypatch):
    seed_history(repo, DAY.year - 1, [50_000.0])
    ev = evaluation(repo, run(repo, host, config, prepare_session(host, monkeypatch)))
    assert ev["source_path"] == "bootstrap" and ev["sample_count"] == 1
    assert ev["budget"] == math.floor(ev["core_fraction"] * ev["sleeve_equity"] / 20.0) * 20.0


def test_a_failed_snapshot_logs_a_skip_and_never_writes_or_overwrites_a_value(repo, host, config, monkeypatch):
    repo.upsert_schg_value(ISO, 777.0)  # an earlier valid row for the same date
    client = prepare_session(host, monkeypatch)
    client.get_pnl = lambda: (_ for _ in ()).throw(RuntimeError("fake offline"))
    result = run(repo, host, config, client)
    log = repo.get_schg_sampling(ISO)
    assert log["status"] == "skipped" and log["reason"] == "snapshot_failed" and log["value"] is None
    assert repo.conn.execute("SELECT value FROM schg_daily_values WHERE date = ?", (ISO,)).fetchone()[0] == 777.0
    assert result.status == "would_have_done"  # sampling never decides a session


def test_a_skip_never_replaces_a_recorded_day(repo):
    sid = repo.conn.execute("SELECT 1").fetchone() and None
    repo.record_schg_sample(ISO, sid, 123.0)
    repo.skip_schg_sample(ISO, sid, "snapshot_failed")
    log = repo.get_schg_sampling(ISO)
    assert log["status"] == "recorded" and log["value"] == 123.0 and log["reason"] is None


def test_evidence_write_failure_is_reported_but_never_stops_the_session(repo, host, config, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("fake storage failure")

    monkeypatch.setattr(repo, "insert_budget_evaluation", broken)
    result = run(repo, host, config, prepare_session(host, monkeypatch))
    assert result.status == "would_have_done"
    assert "budget evidence not recorded (RuntimeError)" in result.reason
    assert repo.get_budget_evaluation(result.session_id) is None
    assert repo.get_schg_sampling(ISO)["status"] == "recorded"


def inputs(value, price=100.0):
    return LadderInputs(
        trading_date=ISO, year=DAY.year, spy=None, cone_z=None, upro_price=1.0, schg_price=price,
        cash=0.0, equity=0.0, schg_position_value=value, open_lots=[], buckets=[])


@pytest.mark.parametrize("value,price", [
    (float("nan"), 100.0), (float("inf"), 100.0), (-1.0, 100.0), (None, 100.0), (True, 100.0),
    (500.0, 0.0), (500.0, float("nan")), (500.0, -3.0),
])
def test_missing_or_invalid_inputs_never_become_a_zero(repo, value, price):
    outcome = session._record_schg_sample(repo, None, ISO, inputs(value, price))
    assert outcome.startswith("skipped")
    assert repo.get_schg_sampling(ISO)["status"] == "skipped"
    assert repo.conn.execute("SELECT COUNT(*) FROM schg_daily_values").fetchone()[0] == 0


def test_a_validated_zero_and_a_validated_value_are_recorded(repo):
    assert session._record_schg_sample(repo, None, ISO, inputs(0.0)) == "recorded"
    assert repo.get_schg_sampling(ISO)["value"] == 0.0
    assert session._record_schg_sample(repo, None, "2026-10-03", inputs(1234.5)) == "recorded"
    assert repo.schg_sample_count(2026) == 2


def test_evaluation_is_written_once_per_session(repo, host, config, monkeypatch):
    result = run(repo, host, config, prepare_session(host, monkeypatch))
    first = dict(evaluation(repo, result))
    repo.insert_budget_evaluation(result.session_id, ISO, {k: first[k] for k in (
        "source_path", "state_budget_year", "target_year", "history_year", "sample_count",
        "sample_mean", "sleeve_equity", "core_fraction", "rounding_step", "sizing_floor",
        "order_divisor", "budget", "order_size", "cash_target")} | {"budget": 999999.0})
    assert dict(evaluation(repo, result)) == first


def test_the_evidence_path_touches_no_broker_or_order_code():
    import inspect
    for fn in (session._record_schg_sample, session._skip_schg_sample):
        src = inspect.getsource(fn)
        for forbidden in ("client", "place_", "get_portfolio", "execute"):
            assert forbidden not in src
