"""T080: offline one-shot sequencing with stub runners and portfolio reads."""

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nightwatch.host import get_host

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = "T080_SYNTHETIC_PRIVATE_BODY"


@pytest.fixture
def seed(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "android-probe/app/src/main/python"))
    module = importlib.import_module("probe.demo_seed")
    home = tmp_path / "nightwatch_demo"
    home.mkdir()
    (home / "SEED_DEMO").write_text("5 11000", encoding="ascii")
    state = SimpleNamespace(module=module, home=home, now=0, clients=0, closed=0,
                            reads=0, buys=[], plans=[], applies=[], accepted=5,
                            complete=True, visible=True, baseline=2, fail=None,
                            refusals=0, plan_refusals=0, buy_elapsed=0, plan_elapsed=0)

    def sleep(seconds):
        state.now += seconds

    def load(*, host):
        assert host.root == home
        assert get_host() is host
        return SimpleNamespace(etoro=object())

    class Client:
        def __init__(self, config, *, role, repo):
            assert role == "read" and repo is None
            assert not (home / "SEED_DEMO").exists()
            assert (home / "SEED_DEMO.running").exists()
            state.clients += 1

        def resolve_instrument_id(self, symbol):
            assert symbol == "SCHG"
            return 1084

        def get_portfolio_strict(self):
            state.reads += 1
            if state.fail == "baseline" and state.reads == 1:
                raise RuntimeError(PRIVATE)
            if state.fail == "positions" and state.reads > 1:
                raise RuntimeError(PRIVATE)
            count = state.baseline + (state.accepted if state.buys and state.visible else 0)
            return [SimpleNamespace(instrument_id=1084) for _ in range(count)] + [
                SimpleNamespace(instrument_id=999)]

        def close(self):
            state.closed += 1

    def buy(home_arg, count, usd_each):
        assert state.reads == 1
        assert (home / "SEED_DEMO.running").exists()
        state.buys.append((home_arg, count, usd_each))
        state.now += state.buy_elapsed
        if state.fail == "buy":
            raise RuntimeError(PRIVATE)
        return "\n".join([f"Order {i}/5: accepted, order ID 123" for i in range(1, state.accepted + 1)]
                         + (["Demo buy complete: stub"] if state.complete else ["Demo buy stopped: stub"]))

    def plan(home_arg):
        state.plans.append(home_arg)
        state.now += state.plan_elapsed
        if state.fail == "plan":
            raise RuntimeError(PRIVATE)
        return json.dumps(dict(status="refused" if len(state.plans) <= state.plan_refusals else "planned",
                               plan_id=f"plan-{len(state.plans)}", report="stub plan report"))

    def apply(home_arg, plan_id):
        state.applies.append((home_arg, plan_id))
        if state.fail == "apply":
            raise RuntimeError(PRIVATE)
        return json.dumps(dict(status="refused" if len(state.applies) <= state.refusals else "applied",
                               report="portfolio changed; plan again" if len(state.applies) <= state.refusals
                               else "stub applied"))

    monkeypatch.setattr(module, "_monotonic", lambda: state.now)
    monkeypatch.setattr(module, "_sleep", sleep)
    monkeypatch.setattr(module, "load_config", load)
    monkeypatch.setattr(module, "EtoroClient", Client)
    monkeypatch.setattr(module.demo_manual_buy, "run", buy)
    monkeypatch.setattr(module.adopt_apply, "plan", plan)
    monkeypatch.setattr(module.adopt_apply, "apply", apply)
    state.run = lambda: json.loads(module.run(str(home)))
    return state


def persisted(seed, result):
    assert json.loads((seed.home / "SEED_DEMO.done").read_text(encoding="utf-8")) == result
    assert not (seed.home / "SEED_DEMO").exists()
    assert not (seed.home / "SEED_DEMO.running").exists()


def test_success_buys_once_adopts_and_persists_one_shot(seed):
    previous = get_host()
    result = seed.run()
    assert result["status"] == "seeded"
    assert seed.buys == [(str(seed.home), 5, 11000)]
    assert seed.plans == [str(seed.home)]
    assert seed.applies == [(str(seed.home), "plan-1")]
    assert result["accepted"] == 5 and result["buy_complete"] and result["positions_visible"]
    assert result["adoption_attempts"] == 1
    assert result["started_utc"] and result["finished_utc"]
    assert seed.clients == seed.closed == 1
    assert get_host() is previous
    persisted(seed, result)


@pytest.mark.parametrize("flag", [b"5", b"5 x", b"0 11000", b"11 11000", b"5 49", b"5 15001",
                                  b"", b"5 11000" + b" " * 26, b"5 1_000", b"5 11000 1", b"5 \xff"])
def test_malformed_flag_refuses_without_client_or_runners(seed, flag):
    (seed.home / "SEED_DEMO").write_bytes(flag)
    result = seed.run()
    assert result["status"] == "refused"
    assert seed.clients == 0
    assert not seed.buys and not seed.plans and not seed.applies
    persisted(seed, result)


@pytest.mark.parametrize("flag,count,usd", [("1 50", 1, 50), ("10 15000", 10, 15000),
                                           ("\t+5\n11000 \r\n", 5, 11000)])
def test_flag_accepts_integer_bounds_and_ascii_whitespace(seed, flag, count, usd):
    (seed.home / "SEED_DEMO").write_text(flag, encoding="ascii")
    seed.run()
    assert seed.buys == [(str(seed.home), count, usd)]


def test_buy_refusal_skips_adoption(seed):
    seed.accepted = 0
    seed.complete = False
    result = seed.run()
    assert result["status"] == "buy_refused_or_failed"
    assert len(seed.buys) == 1 and not seed.plans and not seed.applies
    assert not (seed.home / "SEED_DEMO.adopt").exists()
    persisted(seed, result)


def test_partial_buy_is_never_resent_and_still_adopts(seed):
    seed.accepted = 2
    seed.complete = False
    result = seed.run()
    assert result["status"] == "seeded_partial"
    assert len(seed.buys) == len(seed.plans) == len(seed.applies) == 1
    assert result["accepted"] == 2
    persisted(seed, result)


def test_visibility_timeout_still_adopts_after_180_seconds(seed):
    seed.visible = False
    result = seed.run()
    assert seed.now == 180
    assert result["positions_visible"] is False
    assert result["status"] == "seeded_partial"
    assert len(seed.plans) == len(seed.applies) == 1
    assert any(s["step"] == "positions" and s["status"] == "timeout" for s in result["steps"])
    persisted(seed, result)


@pytest.mark.parametrize("refusals,status", [(2, "seeded"), (3, "bought_adoption_pending")])
def test_portfolio_changed_replans_at_most_three_times(seed, refusals, status):
    seed.refusals = refusals
    result = seed.run()
    assert result["status"] == status
    assert len(seed.plans) == len(seed.applies) == result["adoption_attempts"] == 3
    assert [plan_id for _, plan_id in seed.applies] == ["plan-1", "plan-2", "plan-3"]
    assert seed.now == 20
    assert len(seed.buys) == 1
    persisted(seed, result)


def test_plan_refusals_never_apply_and_stop_after_three(seed):
    seed.plan_refusals = 3
    result = seed.run()
    assert result["status"] == "bought_adoption_pending"
    assert len(seed.plans) == 3 and not seed.applies
    assert len(seed.buys) == 1
    assert (seed.home / "SEED_DEMO.adopt").read_text(encoding="ascii") == "0"
    persisted(seed, result)


@pytest.mark.parametrize("boundary", ["buy", "positions", "plan", "apply"])
def test_budget_expiry_prevents_next_runner_or_poll_and_persists(seed, monkeypatch, boundary):
    if boundary == "buy":
        original = seed.module.load_config

        def load(**kwargs):
            seed.now = 600
            return original(**kwargs)

        monkeypatch.setattr(seed.module, "load_config", load)
    elif boundary == "positions":
        seed.buy_elapsed = 600
    elif boundary == "plan":
        seed.refusals = 3
        seed.plan_elapsed = 590
    else:
        seed.plan_elapsed = 600
    result = seed.run()
    assert result["status"] == ("budget_exceeded" if boundary == "buy" else "bought_adoption_pending")
    assert any(s["status"] == "budget_exceeded" for s in result["steps"])
    if boundary != "buy":
        assert (seed.home / "SEED_DEMO.adopt").read_text(encoding="ascii") == "0"
    assert len(seed.buys) == (0 if boundary == "buy" else 1)
    assert len(seed.plans) == (1 if boundary in ("plan", "apply") else 0)
    assert len(seed.applies) == (1 if boundary == "plan" else 0)
    if boundary == "positions":
        assert seed.reads == 1
    persisted(seed, result)


@pytest.mark.parametrize("stage", ["buy", "positions", "plan", "apply"])
def test_exception_is_redacted_and_done_is_written(seed, capsys, stage):
    previous = get_host()
    seed.fail = stage
    result = seed.run()
    assert result["status"] == ("error" if stage == "buy" else "bought_adoption_pending")
    assert any(s["status"] == "error" for s in result["steps"])
    if stage != "buy":
        assert (seed.home / "SEED_DEMO.adopt").read_text(encoding="ascii") == "0"
    output = json.dumps(result) + capsys.readouterr().out
    assert PRIVATE not in output
    assert "RuntimeError" in output
    assert get_host() is previous
    persisted(seed, result)


def test_baseline_failure_burns_flag_without_buy(seed, capsys):
    seed.fail = "baseline"
    result = seed.run()
    assert result["status"] == "refused"
    assert not seed.buys and not seed.plans
    assert PRIVATE not in json.dumps(result) + capsys.readouterr().out
    persisted(seed, result)


def test_second_call_without_flag_refuses_without_overwriting_done(seed):
    result = seed.run()
    before = (seed.home / "SEED_DEMO.done").read_bytes()
    calls = (len(seed.buys), len(seed.plans), len(seed.applies), seed.clients)
    assert seed.run()["status"] == "refused"
    assert (len(seed.buys), len(seed.plans), len(seed.applies), seed.clients) == calls
    assert (seed.home / "SEED_DEMO.done").read_bytes() == before
    persisted(seed, result)


def test_wrong_resolved_home_refuses_without_renaming(seed):
    wrong = seed.home.parent / "other"
    wrong.mkdir()
    flag = wrong / "SEED_DEMO"
    flag.write_text("5 11000", encoding="ascii")
    result = json.loads(seed.module.run(str(seed.home / ".." / "other")))
    assert result["status"] == "refused"
    assert flag.read_text() == "5 11000"
    assert not (wrong / "SEED_DEMO.done").exists()
    assert seed.clients == 0 and not seed.buys


def test_rename_failure_refuses_before_network(seed, monkeypatch):
    original = Path.replace

    def replace(path, target):
        if path == seed.home / "SEED_DEMO":
            raise OSError(PRIVATE)
        return original(path, target)

    monkeypatch.setattr(Path, "replace", replace)
    result = seed.run()
    assert result["status"] == "refused"
    assert seed.clients == 0 and not seed.buys
    assert PRIVATE not in json.dumps(result)
    assert (seed.home / "SEED_DEMO").exists()


def test_seed_has_only_read_role_and_no_order_call(seed):
    source = Path(seed.module.__file__).read_text(encoding="utf-8")
    assert 'role="read", repo=None' in source
    assert 'role="write"' not in source
    assert "place_buy_order" not in source


@pytest.fixture
def adopting(seed, monkeypatch):
    (seed.home / "SEED_DEMO").unlink()
    (seed.home / "SEED_DEMO.adopt").write_text("0", encoding="ascii")
    seed.buy_result = b'{"status":"bought_adoption_pending","original":"unchanged"}\n'
    (seed.home / "SEED_DEMO.done").write_bytes(seed.buy_result)

    def forbidden(*args, **kwargs):
        pytest.fail("adopt phase must not buy, load config or build its own client")

    monkeypatch.setattr(seed.module.demo_manual_buy, "run", forbidden)
    monkeypatch.setattr(seed.module, "EtoroClient", forbidden)
    monkeypatch.setattr(seed.module, "load_config", forbidden)
    original_plan = seed.module.adopt_apply.plan

    def plan(home):
        assert 1 <= int((seed.home / "SEED_DEMO.adopt").read_text(encoding="ascii")) <= 5
        assert not (seed.home / "SEED_DEMO.adopt.tmp").exists()
        return original_plan(home)

    monkeypatch.setattr(seed.module.adopt_apply, "plan", plan)
    return seed


def adoption_persisted(seed, result):
    assert json.loads((seed.home / "SEED_DEMO.adopt.done").read_text(encoding="utf-8")) == result
    assert (seed.home / "SEED_DEMO.done").read_bytes() == seed.buy_result
    assert not seed.buys and seed.clients == 0
    assert set(result) == {"status", "alarm_number", "adoption_attempts", "started_utc", "finished_utc", "steps"}


def test_adopt_phase_applies_without_buy_or_client_and_preserves_buy_result(adopting):
    result = adopting.run()
    assert result["status"] == "adopted"
    assert result["alarm_number"] == result["adoption_attempts"] == 1
    assert len(adopting.plans) == len(adopting.applies) == 1
    assert not (adopting.home / "SEED_DEMO.adopt").exists()
    adoption_persisted(adopting, result)


@pytest.mark.parametrize("used", [0, 2, 4])
def test_adopt_phase_refusal_advances_counter_before_network_and_keeps_flag(adopting, used):
    (adopting.home / "SEED_DEMO.adopt").write_text(str(used), encoding="ascii")
    adopting.plan_refusals = 3
    result = adopting.run()
    assert result["status"] == "adoption_pending"
    assert result["alarm_number"] == used + 1
    assert result["adoption_attempts"] == len(adopting.plans) == 3
    assert not adopting.applies
    assert adopting.now == 20
    assert (adopting.home / "SEED_DEMO.adopt").read_text(encoding="ascii") == str(used + 1)
    adoption_persisted(adopting, result)


@pytest.mark.parametrize("flag", [b"5", b"-1", b"x", b"", b"6", b"0 1", b"\xff", b"9" * 33])
def test_adopt_phase_invalid_or_exhausted_flag_is_abandoned_without_network(adopting, flag):
    (adopting.home / "SEED_DEMO.adopt").write_bytes(flag)
    result = adopting.run()
    assert result["status"] == "adoption_abandoned"
    assert result["adoption_attempts"] == 0
    assert not adopting.plans and not adopting.applies
    assert not (adopting.home / "SEED_DEMO.adopt").exists()
    assert (adopting.home / "SEED_DEMO.adopt.failed").read_bytes() == flag
    adoption_persisted(adopting, result)


def test_adopt_phase_takes_precedence_and_leaves_buy_flag_untouched(adopting):
    flag = adopting.home / "SEED_DEMO"
    original = b"5 11000\r\n"
    flag.write_bytes(original)
    result = adopting.run()
    assert result["status"] == "adopted"
    assert flag.read_bytes() == original
    adoption_persisted(adopting, result)


def test_adopt_phase_replans_changed_portfolio_at_most_three_times(adopting):
    adopting.refusals = 2
    result = adopting.run()
    assert result["status"] == "adopted"
    assert len(adopting.plans) == len(adopting.applies) == result["adoption_attempts"] == 3
    assert adopting.now == 20
    adoption_persisted(adopting, result)


def test_adopt_phase_budget_expiry_keeps_flag_and_records_cause(adopting):
    adopting.plan_elapsed = 600
    result = adopting.run()
    assert result["status"] == "adoption_pending"
    assert len(adopting.plans) == 1 and not adopting.applies
    assert any(s["status"] == "budget_exceeded" for s in result["steps"])
    assert (adopting.home / "SEED_DEMO.adopt").read_text(encoding="ascii") == "1"
    adoption_persisted(adopting, result)


@pytest.mark.parametrize("stage", ["plan", "apply"])
def test_adopt_phase_exception_keeps_flag_and_redacts_cause(adopting, capsys, stage):
    adopting.fail = stage
    result = adopting.run()
    assert result["status"] == "adoption_pending"
    assert any(s["status"] == "error" and "RuntimeError" in s["detail"] for s in result["steps"])
    assert PRIVATE not in json.dumps(result) + capsys.readouterr().out
    assert (adopting.home / "SEED_DEMO.adopt").read_text(encoding="ascii") == "1"
    adoption_persisted(adopting, result)


def test_adopt_counter_replace_failure_never_calls_network(adopting, monkeypatch):
    original = Path.replace

    def replace(path, target):
        if path == adopting.home / "SEED_DEMO.adopt.tmp":
            raise OSError(PRIVATE)
        return original(path, target)

    monkeypatch.setattr(Path, "replace", replace)
    result = adopting.run()
    assert result["status"] == "adoption_pending"
    assert not adopting.plans and not adopting.applies
    assert (adopting.home / "SEED_DEMO.adopt").read_text(encoding="ascii") == "0"
    assert PRIVATE not in json.dumps(result)
    adoption_persisted(adopting, result)


def test_buy_phase_pending_is_adopted_on_later_alarm_without_rebuy(seed, monkeypatch):
    seed.plan_refusals = 3
    first = seed.run()
    assert first["status"] == "bought_adoption_pending"
    buy_result = (seed.home / "SEED_DEMO.done").read_bytes()
    assert (seed.home / "SEED_DEMO.adopt").read_text(encoding="ascii") == "0"
    persisted(seed, first)

    def forbidden(*args, **kwargs):
        pytest.fail("later alarm must never repeat the buy or create a seed client")

    monkeypatch.setattr(seed.module.demo_manual_buy, "run", forbidden)
    monkeypatch.setattr(seed.module, "EtoroClient", forbidden)
    second = seed.run()
    assert second["status"] == "adopted"
    assert len(seed.buys) == 1 and seed.clients == 1
    assert (seed.home / "SEED_DEMO.done").read_bytes() == buy_result
    assert not (seed.home / "SEED_DEMO.adopt").exists()
    assert json.loads((seed.home / "SEED_DEMO.adopt.done").read_text())["status"] == "adopted"


def test_adopt_phase_stops_after_five_alarms_and_overwrites_own_result(adopting):
    adopting.plan_refusals = 100
    for alarm in range(1, 6):
        result = adopting.run()
        assert result["status"] == "adoption_pending"
        assert result["alarm_number"] == alarm
        assert (adopting.home / "SEED_DEMO.adopt").read_text(encoding="ascii") == str(alarm)
        adoption_persisted(adopting, result)
    assert len(adopting.plans) == 15
    result = adopting.run()
    assert result["status"] == "adoption_abandoned"
    assert len(adopting.plans) == 15
    adoption_persisted(adopting, result)


# --- QA finding 2026-11-13: accepted orders must never be left without an adoption retry ---

def test_recovery_flag_exists_before_the_buy_runner_is_called(seed, monkeypatch):
    seen = []
    original = seed.module.demo_manual_buy.run

    def buy(home_arg, count, usd_each):
        seen.append((seed.home / "SEED_DEMO.adopt").read_text(encoding="ascii"))
        return original(home_arg, count, usd_each)

    monkeypatch.setattr(seed.module.demo_manual_buy, "run", buy)
    assert seed.run()["status"] == "seeded"
    assert seen == ["0"]
    assert not (seed.home / "SEED_DEMO.adopt").exists()  # adopted: flag removed


def test_buy_runner_exception_leaves_recovery_flag_and_later_alarm_adopts_without_rebuy(seed):
    seed.fail = "buy"
    result = seed.run()
    assert result["status"] == "error" and PRIVATE not in json.dumps(result)
    assert (seed.home / "SEED_DEMO.adopt").read_text(encoding="ascii") == "0"
    assert not (seed.home / "SEED_DEMO").exists()
    assert len(seed.buys) == 1 and seed.plans == []
    seed.fail = None
    later = seed.run()
    assert later["status"] == "adopted"
    assert len(seed.buys) == 1  # never bought again
    assert not (seed.home / "SEED_DEMO.adopt").exists()


def test_kill_during_buy_leaves_recovery_flag_for_next_alarm(seed, monkeypatch):
    class Kill(BaseException):
        pass

    def buy(home_arg, count, usd_each):
        seed.buys.append((home_arg, count, usd_each))
        raise Kill()

    monkeypatch.setattr(seed.module.demo_manual_buy, "run", buy)
    with pytest.raises(Kill):
        seed.module.run(str(seed.home))
    assert (seed.home / "SEED_DEMO.adopt").read_text(encoding="ascii") == "0"
    assert not (seed.home / "SEED_DEMO").exists()
    monkeypatch.setattr(seed.module.demo_manual_buy, "run",
                        lambda *a: pytest.fail("buy runner called again after a kill"))
    assert seed.run()["status"] == "adopted"
    assert len(seed.buys) == 1


def test_position_poll_exception_after_acceptance_keeps_recovery_flag(seed):
    seed.fail = "positions"
    result = seed.run()
    assert result["status"] == "bought_adoption_pending" and result["accepted"] == 5
    assert (seed.home / "SEED_DEMO.adopt").read_text(encoding="ascii") == "0"


def test_clean_refusal_with_zero_accepted_removes_recovery_flag(seed):
    seed.accepted, seed.complete = 0, False
    result = seed.run()
    assert result["status"] == "buy_refused_or_failed"
    assert not (seed.home / "SEED_DEMO.adopt").exists()
    assert seed.plans == []


def test_recovery_flag_write_failure_prevents_the_buy(seed, monkeypatch):
    original = Path.write_text

    def write_text(path, *args, **kwargs):
        if path.name == "SEED_DEMO.adopt":
            raise OSError(PRIVATE)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write_text)
    result = seed.run()
    assert result["status"] == "error" and seed.buys == []
    assert PRIVATE not in json.dumps(result)
