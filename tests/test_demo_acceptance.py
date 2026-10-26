"""T039: real isolated SQLite wiring, fake session/brokers, and no network."""
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import socket
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest

from nightwatch.config import EtoroEnvironment, load_config
from nightwatch.db.repo import Repo
from nightwatch.host import DesktopHost, get_host, set_host
from nightwatch.notify.poll import process_pending

_SPEC = importlib.util.spec_from_file_location(
    "demo_acceptance", Path(__file__).resolve().parents[1] / "tools" / "demo_acceptance.py"
)
acceptance = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(acceptance)


def forbidden(*args, **kwargs):
    raise AssertionError("unexpected network, secret loading, or filesystem mutation")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(httpx, "Client", forbidden)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    root = tmp_path / "product"
    root.mkdir()
    production = root / "data"
    production.mkdir()
    sentinel = production / "nightwatch.db"
    sentinel.write_bytes(b"production sentinel: never open")
    config_path = tmp_path / "demo.toml"
    config_path.write_text('''[trading]
mode = "live"
[etoro]
environment = "demo"
[risk]
max_daily_loss_usd = 100.0
max_open_lots = 10
max_orders_per_session = 2
''', encoding="utf-8")
    config = load_config(config_path=config_path, secrets_path=tmp_path / "absent.env",
                         env={"NIGHTWATCH_ARM": "1"}, host=DesktopHost(root))
    previous = get_host()
    installed = DesktopHost(root)
    set_host(installed)
    monkeypatch.setenv("NIGHTWATCH_HOME", str(root))
    monkeypatch.setattr(acceptance, "ROOT", root)
    run_dir = root / "data" / "acceptance_test"
    monkeypatch.setattr(acceptance, "planned_run_dir", lambda: run_dir)
    monkeypatch.setattr(acceptance, "load_config", lambda **kw: config)
    events = []

    class Notifier(acceptance.CaptureNotifier):
        def __init__(self, repo):
            super().__init__(repo)
            events.append("notifier constructed")

        def close(self):
            events.append("notifier closed")

    class Client:
        def __init__(self, cfg, *, role, repo):
            assert cfg.environment == "demo"
            assert role == "write"
            assert get_host().root == run_dir / "home"
            events.append("client constructed")

        def close(self):
            events.append("client closed")

    class Tiingo:
        def __init__(self, cfg):
            events.append("tiingo constructed")

        def close(self):
            events.append("tiingo closed")

    monkeypatch.setattr(acceptance, "EtoroClient", Client)
    monkeypatch.setattr(acceptance, "TiingoClient", Tiingo)
    monkeypatch.setattr(acceptance, "CaptureNotifier", Notifier)
    opened = []
    real_open = Repo.open

    def open_repo(path, *, host):
        assert path.resolve().is_relative_to((run_dir / "home").resolve())
        assert path.resolve() != sentinel.resolve()
        opened.append(path)
        repo = real_open(path, host=host)
        close = repo.close

        def close_repo():
            events.append("repo closed")
            close()

        repo.close = close_repo
        return repo

    monkeypatch.setattr(acceptance.Repo, "open", open_repo)
    args = ["--config", str(config_path), "--secrets", str(tmp_path / "absent.env"),
            "--date", "2026-10-26", "--no-wait"]
    h = SimpleNamespace(root=root, sentinel=sentinel, config=config, config_path=config_path,
                        run_dir=run_dir, args=args, events=events, opened=opened, host=installed)
    try:
        yield h
    finally:
        assert sentinel.read_bytes() == b"production sentinel: never open"
        assert get_host() is installed
        assert acceptance.os.environ["NIGHTWATCH_HOME"] == str(root)
        set_host(previous)


def session_rows(repo, *, snapshot=True, intents=False):
    conn = repo.conn
    conn.execute("INSERT INTO sessions (id,trading_date,state,mode,environment,armed,started_at,created_at) "
                 "VALUES (1,'2026-10-26','finished','live','demo',1,'now','now')")
    conn.execute("INSERT INTO preflight_checks (session_id,gate,name,passed,detail,checked_at) "
                 "VALUES (1,7,'account',1,?, 'now')",
                 (json.dumps({"reason": "account id private-text-only approved"}),))
    if snapshot:
        conn.execute("INSERT INTO portfolio_snapshots (session_id,taken_at,raw_json) VALUES (1,'now',?)",
                     (json.dumps({"phase": "post_execution", "pnl": {"accountId": "private-account"}}),))
    if intents:
        for iid, side in ((1, "close"), (2, "buy")):
            conn.execute("INSERT INTO intents (id,session_id,request_id,sequence,symbol,side,position_id,status,created_at) "
                         "VALUES (?,1,?,?,'UPRO',?,?,'filled','now')",
                         (iid, f"private-request-{iid}", iid, side, f"private-position-{iid}"))
            conn.execute("INSERT INTO orders (intent_id,session_id,request_id,order_id,symbol,side,terminal_status,"
                         "fill_units,status_history_json,created_at,updated_at) VALUES (?,1,?,?,'UPRO',?,3,2,?,'now','now')",
                         (iid, f"private-request-{iid}", f"private-order-{iid}", side,
                          json.dumps({"nested": json.dumps({"accountId": "private-account", "Authorization": "Bearer private-auth"})})))
        conn.execute("INSERT INTO close_observations (intent_id,position_id,observed_status,observed_at,applied_at) "
                     "VALUES (1,'private-position-1','confirmed_full_close','now','now')")
        conn.execute("INSERT INTO sale_effects (session_id,position_id,gross_proceeds_cents,classification,created_at,applied_at) "
                     "VALUES (1,'private-position-1',100,'recyclable','now','now')")
        conn.execute("INSERT INTO tp_clears (order_id,position_id,request_id,attempt,status,created_at) "
                     "VALUES (2,'private-position-2','private-tp-request',1,'cleared','now')")
    conn.commit()


def fake_session(h, monkeypatch, *, status="executed", snapshot=True, intents=False):
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        assert kwargs["host"] is get_host()
        assert kwargs["trading_date"].isoformat() == "2026-10-26"
        assert kwargs["wait"] is False
        assert process_pending(kwargs["notifier"], kwargs["repo"], kwargs["host"], kwargs["config"]) == []
        session_rows(kwargs["repo"], snapshot=snapshot, intents=intents)
        kwargs["notifier"].send("account id private-text-only; private-account; "
                                "https://api.telegram.org/bot123:private-token/sendMessage",
                                event="session_report", session_id=1)
        return SimpleNamespace(status=status, session_id=1, failed_gate=None,
                               reason="private-account private-text-only private-position-1")

    monkeypatch.setattr(acceptance, "run_session", run)
    return calls


def read_result(h):
    return json.loads((h.run_dir / "evidence" / "result.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("explicit", [False, True])
def test_dry_plan_no_secrets_clients_network_or_mutation(harness, monkeypatch, capsys, explicit):
    h = harness
    monkeypatch.setattr(acceptance, "load_config", forbidden)
    monkeypatch.setattr(acceptance, "EtoroClient", forbidden)
    monkeypatch.setattr(acceptance, "TiingoClient", forbidden)
    monkeypatch.setattr(acceptance, "CaptureNotifier", forbidden)
    monkeypatch.setattr(acceptance.Repo, "open", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    real_open = Path.open

    def read_only(path, mode="r", *args, **kwargs):
        assert path == h.config_path and mode == "rb"
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", read_only)
    args = ["--dry-plan", "--secrets", "DO-NOT-READ"]
    if explicit:
        args += ["--config", str(h.config_path)]
    assert acceptance.main(args) == 0
    output = capsys.readouterr().out
    for term in ("mode=live", "NIGHTWATCH_ARM", "risk limits", "max_orders_per_session", "exit_mode=fixed",
                 "environment=demo", acceptance.ORIGIN, "not recovery", str(h.run_dir)):
        assert term in output
    assert not h.run_dir.exists()
    # Undo the open guard before fixture teardown reads its production sentinel.
    monkeypatch.setattr(Path, "open", real_open)


@pytest.mark.parametrize("kind,message", [
    ("real", "etoro.environment must be demo"),
    ("unarmed", "not armed"),
    ("exists", "run dir exists"),
    ("production", "home is the real one"),
    ("origin", "etoro.base_url"),
])
def test_refusals_before_clients_or_repo(harness, monkeypatch, capsys, kind, message):
    h = harness
    cfg = h.config
    if kind == "real":
        cfg = replace(cfg, etoro=replace(cfg.etoro, environment=EtoroEnvironment.REAL))
    elif kind == "unarmed":
        cfg = replace(cfg, arm_flag_present=False)
    elif kind == "exists":
        h.run_dir.mkdir()
        (h.run_dir / "keep").write_text("unchanged")
    elif kind == "production":
        monkeypatch.setattr(acceptance, "fresh_home_for", lambda run: h.root)
    elif kind == "origin":
        cfg = replace(cfg, etoro=replace(cfg.etoro, base_url="https://foreign.invalid"))
    monkeypatch.setattr(acceptance, "load_config", lambda **kw: cfg)
    monkeypatch.setattr(acceptance, "EtoroClient", forbidden)
    monkeypatch.setattr(acceptance, "TiingoClient", forbidden)
    monkeypatch.setattr(acceptance, "CaptureNotifier", forbidden)
    assert acceptance.main(h.args) == 2
    assert message in capsys.readouterr().out
    assert h.opened == []
    if kind == "exists":
        assert (h.run_dir / "keep").read_text() == "unchanged"
        assert not (h.run_dir / "evidence").exists()
    elif kind == "production":
        assert not h.run_dir.exists()


def test_real_host_repo_wiring_and_complete_sanitized_bundle(harness, monkeypatch):
    h = harness
    calls = fake_session(h, monkeypatch, intents=True)
    assert acceptance.main(h.args) == 0
    assert len(calls) == 1
    assert len(h.opened) == 1
    assert h.events == ["client constructed", "notifier constructed", "tiingo constructed",
                        "tiingo closed", "notifier closed", "client closed", "repo closed"]
    evidence = h.run_dir / "evidence"
    for table in acceptance.REQUIRED_TABLES:
        assert (evidence / "tables" / f"{table}.json").is_file()
    manifest = json.loads((evidence / "manifest.json").read_text())
    assert manifest["tables"]["orders"] == 2
    assert all(manifest["phases_observed"].values())
    assert read_result(h)["status"] == "executed"
    assert all(read_result(h)["acceptance_checks"].values())
    changes = json.loads((evidence / "state_changes.json").read_text())
    assert set(changes["before"]) == set(changes["after"]) == set(acceptance.STATE_TABLES)
    all_text = "\n".join(p.read_text(encoding="utf-8") for p in evidence.rglob("*") if p.is_file())
    for private in ("private-account", "private-text-only", "private-auth", "private-token", "bot123:",
                    "private-position-1", "private-order-1", "private-request-1"):
        assert private not in all_text
    orders = json.loads((evidence / "tables" / "orders.json").read_text())
    assert isinstance(orders[0]["status_history_json"]["nested"], dict)
    assert "ORDER_" in all_text and "POSITION_" in all_text and "[REDACTED]" in all_text
    assert not (evidence / "home").exists()


@pytest.mark.parametrize("status,expected", [
    ("executed", 0), ("would_have_done", 0), ("already_ran", 1),
    ("executed_partial", 1), ("aborted", 1), ("refused_armed", 1),
])
def test_raw_status_and_exit_mapping(harness, monkeypatch, status, expected):
    h = harness
    fake_session(h, monkeypatch, status=status)
    assert acceptance.main(h.args) == expected
    result = read_result(h)
    assert result["status"] == status
    assert result["exit_code"] == expected


def test_executed_without_snapshot_fails_acceptance(harness, monkeypatch):
    h = harness
    fake_session(h, monkeypatch, snapshot=False)
    assert acceptance.main(h.args) == 1
    result = read_result(h)
    assert result["status"] == "executed"
    assert not result["acceptance_checks"]["post_execution_snapshot"]


def check_tables():
    tables = {name: [] for name in acceptance.REQUIRED_TABLES}
    tables["intents"] = [
        {"id": 1, "side": "close", "position_id": "p", "status": "filled"},
        {"id": 2, "side": "buy", "position_id": "q", "status": "filled"},
    ]
    tables["orders"] = [{"intent_id": 2, "terminal_status": 3}]
    tables["close_observations"] = [{"intent_id": 1, "observed_status": "confirmed_closed", "applied_at": "now"}]
    tables["sale_effects"] = [{"position_id": "p", "applied_at": "now"}]
    tables["portfolio_snapshots"] = [{"raw_json": '{"phase":"post_execution"}'}]
    return tables


@pytest.mark.parametrize("defect,check", [
    ("close_confirmation", "closes_confirmed_and_sale_effects_applied"),
    ("sale_effect", "closes_confirmed_and_sale_effects_applied"),
    ("buy_unfilled", "buys_filled"),
    ("buy_order", "buys_filled"),
    ("unresolved", "no_unresolved_intents"),
    ("snapshot", "post_execution_snapshot"),
])
def test_acceptance_failure_cases(defect, check):
    tables = check_tables()
    assert all(acceptance.acceptance_checks(tables).values())
    if defect == "close_confirmation":
        tables["close_observations"][0]["observed_status"] = "unresolved"
    elif defect == "sale_effect":
        tables["sale_effects"][0]["applied_at"] = None
    elif defect == "buy_unfilled":
        tables["intents"][1]["status"] = "rejected"
    elif defect == "buy_order":
        tables["orders"][0]["terminal_status"] = 5
    elif defect == "unresolved":
        tables["intents"][1]["status"] = "sent"
    else:
        tables["portfolio_snapshots"][0]["raw_json"] = '{"phase":"post_decision"}'
    assert not acceptance.acceptance_checks(tables)[check]


def test_sanitizer_discovers_across_files_and_json_text_before_rendering():
    value = {"first": {"reason": "later-account later-order text-account"},
             "last": {"raw_json": json.dumps({"accountId": "later-account", "orderId": "later-order",
                                              "nested": json.dumps({"chat_id": "private-chat"})}),
                      "body": "account id text-account; private-chat",
                      "error": "https://api.telegram.org/bot123:private-token/getUpdates"}}
    sanitizer = acceptance.Sanitizer()
    decoded = sanitizer.decode(value)
    sanitizer.discover(decoded)
    clean = sanitizer.sanitize(decoded)
    rendered = json.dumps(clean)
    for private in ("later-account", "later-order", "text-account", "private-chat", "private-token"):
        assert private not in rendered
    assert "ORDER_1" in clean["first"]["reason"]


def test_config_secrets_and_free_text_auth_headers_are_redacted():
    sanitizer = acceptance.Sanitizer()
    sanitizer.discover({"secrets": {"api_key": "runtime-secret"}, "telegram": {"chat_id": "chat-only"}})
    value = {"reason": "runtime-secret chat-only Authorization: Bearer unknown-secret",
             "raw_json": '{"auth":"header-secret","cookie":"session-secret"}'}
    decoded = sanitizer.decode(value)
    sanitizer.discover(decoded)
    rendered = json.dumps(sanitizer.sanitize(decoded))
    for secret in ("runtime-secret", "chat-only", "unknown-secret", "header-secret", "session-secret"):
        assert secret not in rendered


def test_export_has_all_sessions_and_before_after_values(repo):
    session_rows(repo)
    repo.conn.execute("INSERT INTO strategy_state (id,updated_at) VALUES (1,'now')")
    repo.conn.commit()
    before = acceptance.export_tables(repo)
    repo.conn.execute("INSERT INTO sessions (trading_date,state,mode,environment,started_at,created_at) "
                      "VALUES ('2026-10-27','finished','live','demo','now','now')")
    repo.conn.execute("UPDATE strategy_state SET recyclable_cash=123")
    repo.conn.commit()
    after = acceptance.export_tables(repo)
    assert len(after["sessions"]) == 2
    assert before["strategy_state"] != after["strategy_state"]


@pytest.mark.parametrize("failure", ["config", "repo", "client", "notifier", "tiingo", "session", "export", "sanitizer", "write", "cleanup"])
def test_failures_are_nonzero_cleaned_and_minimal_after_possible_sends(harness, monkeypatch, failure):
    h = harness
    calls = fake_session(h, monkeypatch, intents=True)

    def fail(*args, **kwargs):
        raise RuntimeError("account id private-error; https://api.telegram.org/bot123:secret/sendMessage")

    if failure == "config":
        monkeypatch.setattr(acceptance, "load_config", fail)
    elif failure == "repo":
        monkeypatch.setattr(acceptance.Repo, "open", fail)
    elif failure == "client":
        monkeypatch.setattr(acceptance, "EtoroClient", fail)
    elif failure == "notifier":
        monkeypatch.setattr(acceptance, "CaptureNotifier", fail)
    elif failure == "tiingo":
        monkeypatch.setattr(acceptance, "TiingoClient", fail)
    elif failure == "session":
        monkeypatch.setattr(acceptance, "run_session", fail)
    elif failure == "export":
        original = acceptance.export_tables
        exports = []

        def export(repo):
            exports.append(True)
            if len(exports) == 2:
                fail()
            return original(repo)

        monkeypatch.setattr(acceptance, "export_tables", export)
    elif failure == "sanitizer":
        monkeypatch.setattr(acceptance.Sanitizer, "sanitize", fail)
    elif failure == "write":
        original = Path.write_text
        writes = []

        def write(path, *args, **kwargs):
            if ".evidence_pending" in path.parts:
                writes.append(path)
                if len(writes) == 2:
                    fail()
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", write)
    elif failure == "cleanup":
        class Tiingo:
            def __init__(self, cfg):
                pass

            close = fail

        monkeypatch.setattr(acceptance, "TiingoClient", Tiingo)
    assert acceptance.main(h.args) == 1
    evidence = h.run_dir / "evidence"
    assert sorted(p.name for p in evidence.iterdir()) == ["result.json"]
    text = (evidence / "result.json").read_text()
    assert "private-error" not in text and "secret" not in text and "bot123" not in text
    result = json.loads(text)
    assert result["orders_may_have_been_sent"] == (failure in ("session", "export", "sanitizer", "write", "cleanup"))
    if failure in ("export", "sanitizer", "write", "cleanup"):
        assert len(calls) == 1  # Never retry a possibly completed session.
        assert result["status"] == "executed"
    assert not (h.run_dir / ".evidence_pending").exists()
    if failure not in ("config", "repo"):
        assert "repo closed" in h.events
    if failure not in ("config", "repo", "client"):
        assert "client closed" in h.events
    if failure in ("tiingo", "session", "export", "sanitizer", "write", "cleanup"):
        assert "notifier closed" in h.events
    if failure in ("session", "export", "sanitizer", "write"):
        assert "tiingo closed" in h.events


def test_no_seed_option(harness):
    with pytest.raises(SystemExit) as exc:
        acceptance.main(["--seed-db", "never-read"])
    assert exc.value.code == 2
    assert harness.opened == []


def test_capture_never_fetches_or_acknowledges_telegram(repo, host):
    notifier = acceptance.CaptureNotifier(repo)
    assert process_pending(notifier, repo, host, None) == []
    with pytest.raises(RuntimeError, match="offset acknowledgement"):
        notifier.get_updates(offset=42)
    notifier.close()


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm"])
def test_forced_production_db_and_sidecars_refused_before_repo(harness, monkeypatch, suffix):
    h = harness
    monkeypatch.setattr(acceptance, "default_db_path", lambda host: Path(str(h.sentinel) + suffix))
    monkeypatch.setattr(acceptance, "EtoroClient", forbidden)
    assert acceptance.main(h.args) == 2
    assert h.opened == []


def test_db_outside_home_refused():
    with pytest.raises(acceptance.Refusal, match="outside fresh home"):
        acceptance.check_home(Path("C:/fresh"), [], Path("C:/foreign/nightwatch.db"))


@pytest.mark.parametrize("url", [
    "https://api.telegram.org/bot123456:synthetic_bot_token_abcdefghijklmnopqrstuvwxyz/sendMessage",
    "https://api.telegram.org/file/bot123456:synthetic_bot_token_abcdefghijklmnopqrstuvwxyz/photos/file.jpg",
    "https://example.invalid/nested/bot123:short-synthetic-token/file",
    "error contains 123456:synthetic_bot_token_abcdefghijklmnopqrstuvwxyz without a URL",
])
def test_qa_token_path_and_generic_fallback_redacted(url):
    sanitizer = acceptance.Sanitizer()
    bundle = {"first": {"reason": "synthetic_bot_token_abcdefghijklmnopqrstuvwxyz short-synthetic-token"},
              "last": {"error": url}}
    decoded = sanitizer.decode(bundle)
    sanitizer.discover(decoded)
    rendered = json.dumps(sanitizer.sanitize(decoded))
    assert "123456:synthetic_bot_token_abcdefghijklmnopqrstuvwxyz" not in rendered
    assert "bot123:short-synthetic-token" not in rendered
    assert "[REDACTED]" in rendered


@pytest.mark.parametrize("label,value", [
    ("account number:", "987654321"), ("ACCOUNT NO #", "synthetic-account-no"),
    ("account id=", "synthetic-account-id"), ("order id:", "synthetic-order-42"),
    ("position id:", "synthetic-position-42"), ("request id ", "synthetic-request-42"),
    ("cid:", "synthetic-cid-42"), ("customer id=", "synthetic-customer-42"),
    ("chat id#", "synthetic-chat-42"),
    ("Account No.", "987654322"), ("user_id:", "synthetic-user-5678"),
    ("reference_id:", "synthetic-reference-5678"), ("Ref#", "abc-123"), ("acct=", "55"),
    ("trade-num:", "synthetic-trade"), ("Instrument_Account.Number =", "synthetic-instrument-account"),
    ("client.num.", "synthetic-client"), ("position-id.", "synthetic-position"),
])
def test_qa_free_text_identifier_labels_discovered_across_files(label, value):
    sanitizer = acceptance.Sanitizer()
    bundle = {"first": {"reason": value}, "last": {"body": label + " " + value}}
    sanitizer.discover(bundle)
    rendered = json.dumps(sanitizer.sanitize(bundle))
    assert value not in rendered
    assert "[REDACTED]" in rendered


def test_qa_order_and_position_labels_in_one_message():
    value = {"body": "order id: synthetic-order-42; position id: synthetic-position-42"}
    sanitizer = acceptance.Sanitizer()
    sanitizer.discover(value)
    clean = sanitizer.sanitize(value)["body"]
    assert "synthetic-order-42" not in clean
    assert "synthetic-position-42" not in clean
    assert clean.count("[REDACTED]") == 2


@pytest.mark.parametrize("missing", ["exit_mode", "max_notional_per_session_usd", "max_daily_loss_usd",
                                     "max_open_lots", "max_orders_per_session"])
def test_qa_live_prerequisites_refused_before_repo_clients_or_session(harness, monkeypatch, missing):
    h = harness
    cfg = h.config
    if missing == "exit_mode":
        cfg = replace(cfg, strategy=replace(cfg.strategy, exit_mode="trailing"))
    else:
        risk = SimpleNamespace(**{name: getattr(cfg.risk, name) for name in (
            "max_notional_per_session_usd", "max_daily_loss_usd", "max_open_lots", "max_orders_per_session")})
        setattr(risk, missing, None)
        cfg = replace(cfg, risk=risk)
    monkeypatch.setattr(acceptance, "load_config", lambda **kw: cfg)
    session_calls = []
    monkeypatch.setattr(acceptance, "run_session", lambda **kw: session_calls.append(kw))
    assert acceptance.main(h.args) == 2
    assert h.opened == []
    assert h.events == []
    assert session_calls == []
    assert read_result(h)["exit_code"] == 2


@pytest.mark.parametrize("depth", [1, 2, 3])
def test_qa_encoded_query_and_token_path(depth):
    token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_synthetic"
    encoded = token
    for _ in range(depth):
        encoded = quote(encoded, safe="")
    bundle = {"error": "https://api.telegram.org/getFile?token=" + encoded + "#private-fragment",
              "body": "https://example.invalid/file/bot" + encoded + "/image.jpg"}
    sanitizer = acceptance.Sanitizer()
    sanitizer.discover(bundle)
    clean = sanitizer.sanitize(bundle)
    assert clean["error"] == "https://api.telegram.org/getFile?[REDACTED]"
    assert clean["body"] == "https://example.invalid/file/[REDACTED]/image.jpg"
    assert token not in json.dumps(clean)
    assert encoded not in json.dumps(clean)


@pytest.mark.parametrize("url,expected", [
    ("https://example.invalid/path?arbitrary=private-value&other=opaque#fragment",
     "https://example.invalid/path?[REDACTED]"),
    ("https://example.invalid/path#private-fragment", "https://example.invalid/path?[REDACTED]"),
    ("ftp://example.invalid/path?unknown=private-value", "ftp://example.invalid/path?[REDACTED]"),
    ("https://example.invalid/path%3Funknown%3Dprivate-value%23fragment",
     "https://example.invalid/path?[REDACTED]"),
    ("https://example.invalid/path?unknown=prefix%20private-suffix%0Aprivate-tail",
     "https://example.invalid/path?[REDACTED]"),
    ("https://example.invalid/path%3Funknown%3Dprefix%20private-suffix",
     "https://example.invalid/path?[REDACTED]"),
])
def test_qa_entire_url_query_and_fragment_removed(url, expected):
    sanitizer = acceptance.Sanitizer()
    bundle = {"reason": "failed " + url + " after request"}
    sanitizer.discover(bundle)
    assert sanitizer.sanitize(bundle)["reason"] == "failed " + expected + " after request"


@pytest.mark.parametrize("depth", [1, 2, 3])
def test_qa_encoded_identifier_label_discovers_unlabelled_earlier_value(depth):
    labelled = "user_id: synthetic-user-5678"
    for _ in range(depth):
        labelled = quote(labelled, safe="")
    bundle = {"first": {"reason": "synthetic-user-5678"}, "last": {"body": labelled}}
    sanitizer = acceptance.Sanitizer()
    sanitizer.discover(bundle)
    clean = sanitizer.sanitize(bundle)
    assert clean["first"]["reason"] == "[REDACTED]"
    assert "synthetic-user-5678" not in json.dumps(clean)
