"""T073: the dashboard export projects stored budget evidence only, as a fixed allowlist."""

import json

import pytest

from nightwatch.db.repo import Repo
from nightwatch.notify.dashboard_export import dashboard_snapshot, export_json

EVAL = dict(source_path="prior_year_mean", state_budget_year=2025, target_year=2026, history_year=2025,
            sample_count=3, sample_mean=20000.0, sleeve_equity=9000.0, core_fraction=0.4,
            rounding_step=20.0, sizing_floor=10.0, order_divisor=20.0, budget=8000.0,
            order_size=400.0, cash_target=2800.0)
EVAL_KEYS = {"evaluated", "source_path", "session_matches", "trading_date", "evaluated_at",
             "state_budget_year", "target_year", "history_year", "sample_count", "sample_mean_usd",
             "sleeve_equity_usd", "core_fraction", "rounding_step_usd", "sizing_floor_usd",
             "order_divisor", "budget_usd", "order_size_usd", "cash_target_usd", "sampling"}


@pytest.fixture
def repo(tmp_path):
    r = Repo.open(tmp_path / "nightwatch_demo" / "data" / "nightwatch.db")
    yield r
    r.close()


def session(repo, day, state="finished"):
    s = repo.create_session(day, "dry_run", "demo", False)
    repo.update_session(s.id, state=state, preflight_verdict="passed")
    return s


def budget(repo):
    return dashboard_snapshot(repo)["annual_budget"]


def test_sessions_without_any_record_are_not_evaluated_never_assumed(repo):
    session(repo, "2026-10-01")
    assert budget(repo) == {"evaluated": False, "sampling": {"status": "not_evaluated",
                                                              "trading_date": "2026-10-01"}}


def test_an_evaluated_session_projects_exactly_the_named_fields(repo):
    s = session(repo, "2026-10-01")
    repo.insert_budget_evaluation(s.id, "2026-10-01", EVAL)
    repo.record_schg_sample("2026-10-01", s.id, 1234.5)
    out = budget(repo)
    assert set(out) == EVAL_KEYS
    assert out["evaluated"] is True and out["session_matches"] is True
    assert out["source_path"] == "prior_year_mean" and out["sample_count"] == 3
    assert out["sample_mean_usd"] == 20000.0 and out["budget_usd"] == 8000.0
    assert out["order_size_usd"] == 400.0 and out["cash_target_usd"] == 2800.0
    assert out["sleeve_equity_usd"] == 9000.0 and out["rounding_step_usd"] == 20.0
    assert set(out["sampling"]) == {"status", "trading_date", "reason", "value_usd", "recorded_at"}
    assert out["sampling"]["status"] == "recorded" and out["sampling"]["value_usd"] == 1234.5


def test_a_later_session_that_never_evaluated_cannot_borrow_an_older_verdict(repo):
    old = session(repo, "2026-10-01")
    repo.insert_budget_evaluation(old.id, "2026-10-01", EVAL)
    session(repo, "2026-10-02", state="aborted")
    out = budget(repo)
    assert out["evaluated"] is True and out["session_matches"] is False
    assert out["trading_date"] == "2026-10-01"
    assert out["sampling"] == {"status": "not_evaluated", "trading_date": "2026-10-02"}


def test_a_skipped_sample_shows_its_reason_and_no_value(repo):
    s = session(repo, "2026-10-01")
    repo.skip_schg_sample("2026-10-01", s.id, "snapshot_failed")
    sampling = budget(repo)["sampling"]
    assert sampling["status"] == "skipped" and sampling["reason"] == "snapshot_failed"
    assert sampling["value_usd"] is None


def test_foreign_text_in_the_evidence_never_reaches_the_export(repo):
    s = session(repo, "2026-10-01")
    repo.insert_budget_evaluation(s.id, "2026-10-01", EVAL)
    repo.conn.execute("INSERT INTO schg_sampling_log VALUES ('2026-10-01', ?, 'skipped', "
                      "'token=SECRET_MARKER private', NULL, '2026-10-01T00:00:00Z')", (s.id,))
    repo.conn.commit()
    out = budget(repo)
    assert out["sampling"]["reason"] is None
    assert "SECRET_MARKER" not in json.dumps(out)


def test_an_unknown_source_path_is_treated_as_not_evaluated(repo):
    s = session(repo, "2026-10-01")
    repo.insert_budget_evaluation(s.id, "2026-10-01", EVAL)
    repo.conn.execute("PRAGMA ignore_check_constraints = ON")
    repo.conn.execute("UPDATE budget_evaluations SET source_path = 'magic'")
    repo.conn.commit()
    assert budget(repo)["evaluated"] is False


def test_the_section_is_present_even_when_there_is_no_snapshot(repo):
    session(repo, "2026-10-01")
    value = dashboard_snapshot(repo)
    assert "account" not in value and "annual_budget" in value


def test_export_json_carries_the_section_for_the_phone_merge(tmp_path):
    home = tmp_path / "nightwatch_demo"
    r = Repo.open(home / "data" / "nightwatch.db")
    s = session(r, "2026-10-01")
    r.insert_budget_evaluation(s.id, "2026-10-01", EVAL)
    r.close()
    assert json.loads(export_json(str(home)))["annual_budget"]["budget_usd"] == 8000.0


def test_a_database_without_the_evidence_tables_still_exports_everything_else(repo):
    s = session(repo, "2026-10-01")
    repo.insert_decision(__import__("nightwatch.db.repo", fromlist=["Decision"]).Decision(
        s.id, "2026-10-01", reason="Hold", budget=1200, order_size=50))
    repo.conn.execute("DROP TABLE budget_evaluations")
    repo.conn.execute("DROP TABLE schg_sampling_log")
    repo.conn.commit()
    value = dashboard_snapshot(repo)
    assert value["last_decision"]["budget_usd"] == 1200  # the rest of the payload survives
    assert value["annual_budget"] == {"evaluated": False, "sampling": {
        "status": "not_evaluated", "trading_date": "2026-10-01"}}
