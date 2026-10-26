"""Durable sale and tranche applications against migrated, real SQLite."""

from dataclasses import asdict, replace
import sqlite3

import pytest

from nightwatch.db import migrations
from nightwatch.db.repo import Bucket, Decision, Intent, connect
from nightwatch.engine.session import reconcile_effects
from .test_session_decide import no_network


def sale(repo, sid, pid, cents=12345, bucket=None):
    repo.insert_sale_effects(sid, [dict(position_id=pid, gross_proceeds_cents=cents,
                                     classification="bucket" if bucket else "recyclable",
                                     bucket=bucket)])
    return repo.insert_intent(Intent(sid, f"close-{pid}", repo.next_intent_sequence(sid),
                                     "UPRO", "close", position_id=pid, status="sent"))


def confirm(repo, iid, pid):
    repo.record_close_observation(intent_id=iid, order_row_id=None, order_id=None,
                                  position_id=pid, observed_status="confirmed_full_close")
    repo.set_intent_status(iid, "filled", resolved=True)


def reentry(repo, sid, *, status="filled", bucket_id="b"):
    seq = repo.next_intent_sequence(sid)
    return repo.insert_intent(Intent(sid, f"buy-{sid}-{seq}", seq, "UPRO", "buy",
                                     book="reentry", bucket_id=bucket_id,
                                     amount_usd=100, status=status))


def decision(repo, sid, day="2026-10-01", spy=95):
    repo.insert_decision(Decision(session_id=sid, trading_date=day, intents=[], spy_close=spy))


@pytest.fixture
def sid(repo):
    return repo.create_session("2026-10-01", "live", "demo", True).id


def test_confirmed_prefix_then_late_confirmation_applies_each_once(repo, sid):
    repo.save_strategy_state(replace(repo.get_strategy_state(), bars=7, ath=99,
                                    recyclable_cash=10.01, below_streak=2))
    before = repo.get_strategy_state()
    a = sale(repo, sid, "a", 12345)
    b = sale(repo, sid, "b", 23456)
    confirm(repo, a, "a")
    repo.record_close_observation(intent_id=b, order_row_id=None, order_id=None,
                                  position_id="b", observed_status="unresolved")
    reconcile_effects(repo)
    after = repo.get_strategy_state()
    assert after.recyclable_cash == 133.46
    assert (after.bars, after.ath, after.below_streak) == (before.bars, before.ath, before.below_streak)
    assert repo.unapplied_sale_effects() == []
    assert not repo.apply_sale_effect("b")
    # Recovery's eventual confirmation makes the second effect eligible.
    confirm(repo, b, "b")
    reconcile_effects(repo)
    reconcile_effects(repo)
    assert repo.get_strategy_state().recyclable_cash == 368.02
    assert not repo.apply_sale_effect("a") and not repo.apply_sale_effect("b")


def test_crash_after_confirmation_reconcile_applies_once_after_reopen(repo, sid):
    iid = sale(repo, sid, "a")
    confirm(repo, iid, "a")
    # A fresh connection sees the committed effect/evidence, but no application.
    with connect(repo.conn.execute("PRAGMA database_list").fetchone()[2]) as conn:
        from nightwatch.db.repo import Repo
        restarted = Repo(conn)
        assert restarted.get_strategy_state().recyclable_cash == 0
        reconcile_effects(restarted)
        reconcile_effects(restarted)
    assert repo.get_strategy_state().recyclable_cash == 123.45


def test_bucket_batch_uses_sum_of_confirmed_per_lot_cents_and_one_payload(repo, sid):
    """Chosen rule: sum confirmed per-lot cents, even when all lots confirm.

    Only the first row stores the batch payload. A late confirmation enlarges
    that bucket, never creates another or overwrites an already-applied fill.
    """
    bucket = Bucket("batch", "2026-10-01", 100, 600, 200, 8, last_reference_spy=100)
    payload = {"bucket": asdict(bucket), "tranches": 3}
    a = sale(repo, sid, "a", 20001, payload)
    repo.insert_sale_effects(sid, [dict(position_id="b", gross_proceeds_cents=40001,
                                     classification="bucket", bucket=None)])
    b = repo.insert_intent(Intent(sid, "close-b", 2, "UPRO", "close", position_id="b"))
    confirm(repo, a, "a")
    reconcile_effects(repo)
    assert repo.get_bucket("batch").proceeds == 200.01
    assert repo.get_bucket("batch").tranche_amount == 66.67
    confirm(repo, b, "b")
    reconcile_effects(repo)
    reconcile_effects(repo)
    assert len(repo.active_buckets()) == 1
    assert repo.get_bucket("batch").proceeds == 600.02
    assert repo.get_bucket("batch").tranche_amount == 200
    assert repo.get_strategy_state().recyclable_cash == 0
    assert repo.conn.execute("SELECT COUNT(bucket_json) FROM sale_effects").fetchone()[0] == 1


def test_rejected_close_never_applies(repo, sid):
    iid = sale(repo, sid, "a")
    repo.set_intent_status(iid, "rejected", resolved=True)
    reconcile_effects(repo)
    assert not repo.apply_sale_effect("a")
    assert repo.get_strategy_state().recyclable_cash == 0


def test_sale_insert_batch_failure_rolls_back_every_row(repo, sid):
    with pytest.raises(sqlite3.IntegrityError):
        repo.insert_sale_effects(sid, [
            dict(position_id="a", gross_proceeds_cents=1, classification="recyclable", bucket=None),
            dict(position_id="b", gross_proceeds_cents=-1, classification="recyclable", bucket=None),
        ])
    assert repo.conn.execute("SELECT COUNT(*) FROM sale_effects").fetchone()[0] == 0


@pytest.mark.parametrize("tranches", [0, 2])
def test_recovered_or_restarted_fill_advances_once_and_third_retires(repo, sid, tranches):
    repo.upsert_bucket(Bucket("b", "2026-09-01", 100, 300, 100, 1,
                              tranches_bought=tranches, last_reference_spy=100))
    decision(repo, sid)
    iid = reentry(repo, sid, status="sent")
    reconcile_effects(repo)
    assert repo.get_bucket("b").tranches_bought == tranches
    # Both recovery and an executor interrupted after a fill persist this status.
    repo.set_intent_status(iid, "filled", resolved=True)
    reconcile_effects(repo)
    reconcile_effects(repo)
    b = repo.get_bucket("b")
    assert b.tranches_bought == tranches + 1 and b.last_reference_spy == 95
    assert b.is_active == (tranches < 2)
    assert not repo.apply_bucket_fill(iid)
    assert repo.conn.execute("SELECT COUNT(*) FROM bucket_fill_applications").fetchone()[0] == 1


def test_bucket_fill_failure_rolls_back_marker_and_retries(repo, sid):
    decision(repo, sid)
    repo.upsert_bucket(Bucket("b", "2026-09-01", 100, 300, 100, 1, last_reference_spy=100))
    iid = reentry(repo, sid)
    repo.conn.execute("CREATE TRIGGER refuse_fill BEFORE INSERT ON bucket_fill_applications "
                      "BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
    repo.conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="disk failure"):
        repo.apply_bucket_fill(iid)
    assert repo.get_bucket("b").tranches_bought == 0
    assert repo.conn.execute("SELECT COUNT(*) FROM bucket_fill_applications").fetchone()[0] == 0
    repo.conn.execute("DROP TRIGGER refuse_fill")
    repo.conn.commit()
    assert repo.apply_bucket_fill(iid)


def test_application_and_sale_mutation_roll_back_together(repo, sid):
    iid = sale(repo, sid, "a")
    confirm(repo, iid, "a")
    repo.get_strategy_state()
    repo.conn.execute("CREATE TRIGGER refuse_apply BEFORE UPDATE ON sale_effects "
                      "BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
    repo.conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="disk failure"):
        repo.apply_sale_effect("a")
    assert repo.get_strategy_state().recyclable_cash == 0
    repo.conn.execute("DROP TRIGGER refuse_apply")
    repo.conn.commit()
    assert repo.apply_sale_effect("a")
    assert repo.get_strategy_state().recyclable_cash == 123.45


def test_migration_010_on_009_and_idempotent(tmp_path, monkeypatch):
    all_migrations = migrations.discover()
    with connect(tmp_path / "upgrade.db") as conn:
        with monkeypatch.context() as patch:
            patch.setattr(migrations, "discover", lambda: [m for m in all_migrations if m.version <= 9])
            assert migrations.migrate(conn) == 9
        assert migrations.migrate(conn) == 10
        assert migrations.migrate(conn) == 10
        assert {r[1] for r in conn.execute("PRAGMA table_info(sale_effects)")} == {
            "id", "session_id", "position_id", "gross_proceeds_cents", "classification",
            "bucket_json", "created_at", "applied_at",
        }
        assert {r[1] for r in conn.execute("PRAGMA table_info(bucket_fill_applications)")} == {
            "intent_id", "bucket_id", "applied_at",
        }
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_retry_upsert_replaces_unconfirmed_sale_effect_fields(repo, sid):
    sale(repo, sid, "a", cents=12345)
    repo.conn.execute("UPDATE sale_effects SET created_at = 'old-time'")
    repo.conn.commit()
    next_sid = repo.create_session("2026-10-02", "live", "demo", True).id
    payload = {"bucket": asdict(Bucket("new-batch", "2026-10-02", 110, 300, 100, 1)),
               "tranches": 3}
    repo.insert_sale_effects(next_sid, [dict(position_id="a", gross_proceeds_cents=30001,
                                           classification="bucket", bucket=payload)])
    row = repo.conn.execute("SELECT * FROM sale_effects WHERE position_id = 'a'").fetchone()
    assert row["session_id"] == next_sid
    assert row["gross_proceeds_cents"] == 30001
    assert row["classification"] == "bucket"
    import json
    assert json.loads(row["bucket_json"]) == payload
    assert row["created_at"] != "old-time" and row["applied_at"] is None
    # Retrying as recyclable also removes a stale bucket payload.
    repo.insert_sale_effects(next_sid, [dict(position_id="a", gross_proceeds_cents=40002,
                                           classification="recyclable", bucket=None)])
    row = repo.conn.execute("SELECT * FROM sale_effects WHERE position_id = 'a'").fetchone()
    assert row["classification"] == "recyclable" and row["bucket_json"] is None
    assert row["gross_proceeds_cents"] == 40002
    assert repo.conn.execute("SELECT COUNT(*) FROM sale_effects").fetchone()[0] == 1


def test_confirmed_effect_cannot_be_overwritten_before_reconcile(repo, sid):
    iid = sale(repo, sid, "a", cents=12345)
    confirm(repo, iid, "a")
    before = dict(repo.conn.execute("SELECT * FROM sale_effects").fetchone())
    next_sid = repo.create_session("2026-10-02", "live", "demo", True).id
    retry = dict(position_id="a", gross_proceeds_cents=99999,
                 classification="recyclable", bucket=None)
    with pytest.raises(ValueError, match="confirmed sale effect must be applied"):
        repo.insert_sale_effects(next_sid, [retry])
    assert dict(repo.conn.execute("SELECT * FROM sale_effects").fetchone()) == before
    reconcile_effects(repo)
    assert repo.get_strategy_state().recyclable_cash == 123.45
    with pytest.raises(ValueError, match="sale effect already applied"):
        repo.insert_sale_effects(next_sid, [retry])
    reconcile_effects(repo)
    assert repo.get_strategy_state().recyclable_cash == 123.45
    row = repo.conn.execute("SELECT * FROM sale_effects").fetchone()
    assert row["session_id"] == sid and row["gross_proceeds_cents"] == 12345
    assert row["applied_at"] is not None


def test_retry_batch_rolls_back_if_any_position_was_already_applied(repo, sid):
    sale(repo, sid, "retry", cents=1)
    iid = sale(repo, sid, "closed", cents=2)
    confirm(repo, iid, "closed")
    reconcile_effects(repo)
    next_sid = repo.create_session("2026-10-02", "live", "demo", True).id
    with pytest.raises(ValueError, match="sale effect already applied"):
        repo.insert_sale_effects(next_sid, [
            dict(position_id="retry", gross_proceeds_cents=100,
                 classification="recyclable", bucket=None),
            dict(position_id="closed", gross_proceeds_cents=200,
                 classification="recyclable", bucket=None),
        ])
    row = repo.conn.execute("SELECT * FROM sale_effects WHERE position_id = 'retry'").fetchone()
    assert row["session_id"] == sid and row["gross_proceeds_cents"] == 1
    assert repo.get_strategy_state().recyclable_cash == 0.02
