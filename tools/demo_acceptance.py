"""One demo-only armed acceptance session; live execution belongs to the PM."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tomllib
from urllib.parse import unquote
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app" / "src" / "main" / "python"))
from nightwatch.config import load_config  # noqa: E402
from nightwatch.db.repo import Notification, Repo, default_db_path  # noqa: E402
from nightwatch.engine.session import arming_refusals, run_session  # noqa: E402
from nightwatch.etoro.client import EtoroClient  # noqa: E402
from nightwatch.host import DesktopHost, get_host, set_host  # noqa: E402
from nightwatch.market.source import TiingoClient  # noqa: E402

ORIGIN = "https://public-api.etoro.com"
WARNING = ("WARNING: a fresh-home rerun is not recovery after an uncertain send; "
           "the existing-session check is DB-local. Inspect broker state before rerunning.")
STATE_TABLES = ("buckets", "strategy_state", "lot_annotations")
REQUIRED_TABLES = (
    "sessions", "preflight_checks", "decisions", "intents", "orders", "lots",
    "buckets", "strategy_state", "portfolio_snapshots", "portfolio_snapshot_positions",
    "notifications", "close_observations", "sale_effects", "bucket_fill_applications",
    "lot_annotations", "tp_clears",
)
REDACTED = "[REDACTED]"
SENSITIVE = re.compile(r"token|secret|password|auth|key|cookie|chatid", re.I)
TEXT_ID = re.compile(
    r"(?<![\w-])(?:instrument_?account|account|acct|user|customer|gcid|cid|client|"
    r"order|position|request|reference|ref|chat|trade)"
    r"(?:[ _.-]*(?:id|number|num|no))?\.?[:=#\s]+([A-Za-z0-9_-]+)", re.I,
)
URL = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s\"'<>]+", re.I)
BOT_URL = re.compile(r"https?://(?:api\.)?telegram\.org/bot[^\s\"'<>]+", re.I)
BOT_TOKEN = re.compile(r"(?<=/)bot\d+:[A-Za-z0-9_-]+|\d{6,}:[A-Za-z0-9_-]{20,}", re.I)


class Refusal(Exception):
    pass


class Sanitizer:
    """Discover across the entire bundle before replacing any identifier or secret."""

    def __init__(self):
        self.replacements = {}
        self.ids = {kind: {} for kind in ("ORDER", "POSITION", "REFERENCE")}

    def decode(self, value, key=""):
        if is_dataclass(value):
            value = asdict(value)
        if isinstance(value, dict):
            return {str(k): self.decode(v, str(k)) for k, v in value.items()}
        if isinstance(value, (tuple, list)):
            return [self.decode(v) for v in value]
        if isinstance(value, str):
            text = value.strip()
            if text and (key.endswith("_json") or text[0] in '[{\"'):
                try:
                    decoded = json.loads(value)
                except ValueError:
                    if key.endswith("_json"):
                        raise ValueError("invalid JSON evidence column") from None
                else:
                    if decoded != value:
                        return self.decode(decoded)
        return value

    @staticmethod
    def normalize_text(text):
        def decode(part):
            for _ in range(3):
                decoded = unquote(part)
                if decoded == part:
                    break
                part = decoded
            return part

        def strip_query(url):
            parts = re.split(r"[?#]", url, maxsplit=1)
            return parts[0] + ("?[REDACTED]" if len(parts) == 2 else "")

        # Keep each URL intact while decoding: escaped whitespace must not expose a query tail.
        parts = []
        start = 0
        for match in URL.finditer(text):
            parts.extend((decode(text[start:match.start()]), strip_query(decode(match[0]))))
            start = match.end()
        parts.append(decode(text[start:]))
        return URL.sub(lambda m: strip_query(m[0]), "".join(parts))

    def discover(self, value, sensitive=False):
        if isinstance(value, dict):
            for key, item in value.items():
                name = re.sub(r"[-_]", "", key).lower()
                private = sensitive or bool(SENSITIVE.search(name))
                account = name in ("accountid", "cid", "gcid", "clientid", "userid")
                kind = {"orderid": "ORDER", "positionid": "POSITION",
                        "referenceid": "REFERENCE", "requestid": "REFERENCE",
                        "xrequestid": "REFERENCE"}.get(name)
                if (private or account or kind) and item is not None and not isinstance(item, (dict, list)):
                    original = self.normalize_text(str(item))
                    if original:
                        if private or account:
                            self.replacements[original] = REDACTED
                        else:
                            ids = self.ids[kind]
                            if original not in ids:
                                ids[original] = f"{kind}_{len(ids) + 1}"
                            self.replacements.setdefault(original, ids[original])
                self.discover(item, private or account)
        elif isinstance(value, list):
            for item in value:
                self.discover(item, sensitive)
        elif isinstance(value, str):
            value = self.normalize_text(value)
            if sensitive and value:
                self.replacements[value] = REDACTED
            for match in TEXT_ID.finditer(value):
                self.replacements[match[1]] = REDACTED
            for pattern in (BOT_URL, BOT_TOKEN):
                for match in pattern.finditer(value):
                    self.replacements[match[0]] = REDACTED

    def sanitize(self, value, key=""):
        if isinstance(value, dict):
            return {self.text(k): (REDACTED if SENSITIVE.search(re.sub(r"[-_]", "", k))
                                  or re.sub(r"[-_]", "", k).lower() in
                                  ("accountid", "cid", "gcid", "clientid", "userid")
                                  else self.sanitize(v, k)) for k, v in value.items()}
        if isinstance(value, list):
            return [self.sanitize(v, key) for v in value]
        if isinstance(value, str):
            return self.text(value)
        if re.sub(r"[-_]", "", key).lower() in (
            "orderid", "positionid", "referenceid", "requestid", "xrequestid"
        ) and str(value) in self.replacements:
            return self.replacements[str(value)]
        return value

    def text(self, text):
        text = self.normalize_text(text)
        text = BOT_TOKEN.sub(REDACTED, BOT_URL.sub(REDACTED, text))
        # Match once against the original text: replacements must not redact their own aliases.
        originals = sorted(self.replacements, key=len, reverse=True)
        if originals:
            pattern = r"(?<![\w])(?:" + "|".join(re.escape(v) for v in originals) + r")(?![\w])"
            text = re.sub(pattern, lambda m: self.replacements[m[0]], text)
        # Header/token values in unstructured errors need not have appeared in a JSON field.
        return re.sub(r"(?i)\b(authorization|x-api-key|x-user-key|bot_token|access_token)"
                      r"\s*[:=]\s*(?:Bearer\s+)?[^\s,;\"']+", r"\1=[REDACTED]", text)


class CaptureNotifier:
    """No Telegram client, no inbound updates, and no offset acknowledgement."""

    def __init__(self, repo):
        self.repo = repo
        self.messages = []

    def send(self, text, *, parse_mode=None, event="message", session_id=None, trading_date=None):
        notification = Notification(event=event, body=text, session_id=session_id,
                                    trading_date=trading_date, status="skipped")
        self.messages.append(asdict(notification))
        self.repo.insert_notification(notification)
        return notification

    def get_updates(self, offset=None, timeout=0):
        if offset is not None:
            raise RuntimeError("acceptance capture refuses offset acknowledgement")
        return []

    def close(self):
        pass


def planned_run_dir():
    return ROOT / "data" / ("acceptance_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))


def fresh_home_for(run_dir):
    return run_dir / "home"


def check_home(home, production_homes, db=None):
    home = home.resolve()
    target = (db if db is not None else home / "data" / "nightwatch.db").resolve()
    if any(home == production.resolve() for production in production_homes):
        raise Refusal("acceptance refused: home is the real one")
    if not target.is_relative_to(home):
        raise Refusal("acceptance refused: DB is outside fresh home")
    for production in production_homes:
        prod_db = production / "data" / "nightwatch.db"
        if target in {Path(str(prod_db) + suffix).resolve() for suffix in ("", "-wal", "-shm")}:
            raise Refusal("acceptance refused: DB is the production DB")


def export_tables(repo):
    names = [row[0] for row in repo.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    if not set(REQUIRED_TABLES).issubset(names):
        raise ValueError("acceptance export missing required tables")
    return {name: [dict(row) for row in repo.conn.execute(
        'SELECT * FROM "' + name.replace('"', '""') + '"'
    )] for name in names}


def acceptance_checks(tables):
    intents = tables["intents"]
    orders = {row["intent_id"]: row for row in tables["orders"]}
    closes = [i for i in intents if i["side"] == "close"]
    buys = [i for i in intents if i["side"] == "buy"]
    confirmed = {o["intent_id"] for o in tables["close_observations"]
                 if o["observed_status"] in ("confirmed_full_close", "confirmed_closed") and o["applied_at"]}
    applied = {e["position_id"] for e in tables["sale_effects"] if e["applied_at"]}
    snapshots = [json.loads(row["raw_json"] or "{}") for row in tables["portfolio_snapshots"]]
    return {
        "closes_confirmed_and_sale_effects_applied": all(
            i["status"] in ("filled", "recovered") and i["id"] in confirmed
            and i["position_id"] in applied for i in closes),
        "buys_filled": all(i["status"] in ("filled", "recovered")
                           and orders.get(i["id"], {}).get("terminal_status") == 3 for i in buys),
        "post_execution_snapshot": any(s.get("phase") == "post_execution" for s in snapshots),
        "no_unresolved_intents": not any(i["status"] in ("pending", "sent") for i in intents),
    }


def report(bundle):
    lines = ["# Demo acceptance", "", WARNING, "",
             "Working home/ DB is not evidence and must never be attached.",
             "Inbound-command acceptance is excluded; outbound messages are captured only.", "",
             "## Result", json.dumps(bundle["result"], indent=2)]
    sections = (("Preflight gates", "preflight_checks"), ("Intents", "intents"),
                ("Orders and fills", "orders"), ("Close observations", "close_observations"),
                ("Sale effects applied", "sale_effects"), ("TP clears", "tp_clears"),
                ("Snapshot", "portfolio_snapshots"))
    for title, table in sections:
        lines.extend(["", "## " + title, json.dumps(bundle["tables"][table], indent=2)])
    lines.extend(["", "## Bucket, strategy state and annotation changes",
                  json.dumps(bundle["state_changes"], indent=2), "", "## Captured notifications",
                  json.dumps(bundle["captured_notifications"], indent=2)])
    return "\n".join(lines) + "\n"


def write_bundle(run_dir, bundle, config):
    sanitizer = Sanitizer()
    # Config is discovery-only, never rendered or included in the evidence.
    sanitizer.discover(sanitizer.decode(config))
    decoded = sanitizer.decode(bundle)
    sanitizer.discover(decoded)
    clean = sanitizer.sanitize(decoded)
    files = {"result.json": clean["result"], "manifest.json": clean["manifest"],
             "state_changes.json": clean["state_changes"],
             "captured_notifications.json": clean["captured_notifications"]}
    files.update({f"tables/{name}.json": rows for name, rows in clean["tables"].items()})
    # Render every byte before creating even a staging file.
    rendered = {name: json.dumps(value, indent=2, allow_nan=False) + "\n"
                for name, value in files.items()}
    rendered["report.md"] = report(clean)
    staging = run_dir / ".evidence_pending"
    staging.mkdir(exist_ok=False)
    try:
        for name, text in rendered.items():
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        staging.rename(run_dir / "evidence")
    except Exception:
        shutil.rmtree(staging)
        raise


def orders_may_have_been_sent(run_dir, *, started):
    if not started:
        return False
    try:
        db = fresh_home_for(run_dir) / "data" / "nightwatch.db"
        with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            counts = [conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                      for table in ("intents", "orders", "tp_clears")]
        return any(count > 0 for count in counts)
    except Exception:
        return True


def minimal_failure(run_dir, *, code, status, started, reason, exc, config):
    record = {"status": status if status in ("executed", "would_have_done", "executed_partial") else None,
              "session_id": None, "failed_gate": None, "reason": reason, "exit_code": code,
              "orders_may_have_been_sent": orders_may_have_been_sent(run_dir, started=started),
              "exception_type": type(exc).__name__, "warning": WARNING}
    try:
        if config is not None:
            sanitizer = Sanitizer()
            sanitizer.discover(sanitizer.decode(config))
            message = str(exc)
            sanitizer.discover(message)
            record["exception_message"] = sanitizer.sanitize(message)
    except Exception:
        pass  # Never let arbitrary text escape when discovery or sanitization fails.
    text = json.dumps(record, indent=2) + "\n"
    print(text, end="")
    if run_dir is not None:
        evidence = run_dir / "evidence"
        evidence.mkdir(exist_ok=False)
        (evidence / "result.json").write_text(text, encoding="utf-8")


def dry_plan(args, run_dir):
    print("Demo acceptance dry plan (no secrets, clients, network or filesystem mutation)")
    print(f"Run directory: {run_dir}")
    print("Fresh DesktopHost -> write-role demo client -> capture-only notifier -> Tiingo")
    print("One run_session: preflight/recovery -> closes -> sale effects -> cash reread -> buys -> TP clears -> snapshot")
    print("Export all tables, before/after state, captured notifications, manifest and sanitized report atomically.")
    print("Gate 7 reads both demo and real portfolios; real-account trading is never permitted.")
    print(WARNING)
    data = None
    if args.config:
        try:
            with args.config.open("rb") as handle:
                data = tomllib.load(handle)
        except Exception:
            print("acceptance refused: config-only parse failed")
            return 1
    checks = {
        "mode=live": lambda d: d.get("trading", {}).get("mode", "dry_run") == "live",
        "NIGHTWATCH_ARM set": lambda d: bool(os.environ.get("NIGHTWATCH_ARM")),
        "risk limits: max_notional_per_session_usd, max_daily_loss_usd, max_open_lots":
            lambda d: all(d.get("risk", {}).get(k, 5000 if k == "max_notional_per_session_usd" else None)
                          is not None for k in ("max_notional_per_session_usd", "max_daily_loss_usd", "max_open_lots")),
        "risk.max_orders_per_session set": lambda d: d.get("risk", {}).get("max_orders_per_session") is not None,
        "strategy.exit_mode=fixed": lambda d: d.get("strategy", {}).get("exit_mode", "fixed") == "fixed",
        "etoro.environment=demo": lambda d: d.get("etoro", {}).get("environment", "demo") == "demo",
        "etoro.base_url=" + ORIGIN: lambda d: d.get("etoro", {}).get("base_url", ORIGIN) == ORIGIN,
    }
    try:
        for name, check in checks.items():
            print(f"{'REQUIRED' if data is None else 'PASS' if check(data) else 'FAIL'}: {name}")
    except (AttributeError, TypeError):
        print("acceptance refused: invalid config-only table shape")
        return 1
    print(f"{'FAIL' if run_dir.exists() else 'PASS'}: run directory must not exist")
    print("REQUIRED: fresh home and resolved DB isolated from default/inherited production DBs (including WAL/SHM)")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--secrets", type=Path)
    parser.add_argument("--date", type=date.fromisoformat)
    parser.add_argument("--no-wait", action="store_true")
    parser.add_argument("--dry-plan", action="store_true")
    args = parser.parse_args(argv)
    run_dir = planned_run_dir()
    if args.dry_plan:
        return dry_plan(args, run_dir)

    print(WARNING)
    previous_host = get_host()
    previous_home = os.environ.get("NIGHTWATCH_HOME")
    production_homes = [ROOT]
    if previous_home:
        production_homes.append(Path(previous_home).expanduser())
    if isinstance(previous_host, DesktopHost):
        production_homes.append(previous_host.root)
    created = None
    started = False
    status = None
    config = None
    try:
        if run_dir.exists():
            raise Refusal("acceptance refused: run dir exists")
        home = fresh_home_for(run_dir)
        check_home(home, production_homes)
        if home.exists() or home.resolve() != (run_dir / "home").resolve():
            raise Refusal("acceptance refused: home is not fresh")
        run_dir.mkdir(parents=True, exist_ok=False)
        created = run_dir
        home.mkdir(exist_ok=False)
        os.environ["NIGHTWATCH_HOME"] = str(home)
        host = DesktopHost(home)
        set_host(host)
        check_home(host.root, production_homes)
        if args.config is None or args.secrets is None:
            raise ValueError("live invocation requires --config and --secrets")
        config = load_config(config_path=args.config, secrets_path=args.secrets, host=host)
        if config.etoro.environment != "demo":
            raise Refusal("acceptance refused: etoro.environment must be demo")
        if config.etoro.base_url != ORIGIN:
            raise Refusal("acceptance refused: etoro.base_url must be the default HTTPS origin")
        if not config.is_armed:
            raise Refusal("acceptance refused: not armed (mode=live and NIGHTWATCH_ARM required)")
        prerequisites = arming_refusals(config)
        if config.risk.max_orders_per_session is None:
            prerequisites.append("risk.max_orders_per_session unset")
        if prerequisites:
            raise Refusal("acceptance refused: " + "; ".join(prerequisites))
        # Check the pure path before data_dir() can create directories; check its returned path again.
        check_home(host.root, production_homes)
        db = default_db_path(host)
        check_home(host.root, production_homes, db)
        with ExitStack() as stack:
            repo = Repo.open(db, host=host)
            stack.callback(repo.close)
            before_tables = export_tables(repo)
            before = {name: before_tables[name] for name in STATE_TABLES}
            client = EtoroClient(config.etoro, role="write", repo=repo)
            stack.callback(client.close)
            notifier = CaptureNotifier(repo)
            stack.callback(notifier.close)
            tiingo = TiingoClient(config.tiingo)
            stack.callback(tiingo.close)
            started = True
            result = run_session(host=host, config=config, repo=repo, client=client,
                                 trading_date=args.date or datetime.now(ZoneInfo("America/New_York")).date(),
                                 tiingo=tiingo, wait=not args.no_wait, notifier=notifier)
            status = result.status
            tables = export_tables(repo)
            checks = acceptance_checks(tables)
            code = 0 if status in ("executed", "would_have_done") and all(checks.values()) else 1
            bundle = {
                "result": {"status": status, "session_id": result.session_id, "failed_gate": result.failed_gate,
                           "reason": result.reason, "exit_code": code, "acceptance_checks": checks},
                "tables": tables,
                "state_changes": {"before": before, "after": {name: tables[name] for name in STATE_TABLES}},
                "captured_notifications": notifier.messages,
                "manifest": {"tables": {name: len(rows) for name, rows in tables.items()},
                             "phases_observed": {
                                 "closes": bool(tables["close_observations"]),
                                 "sale_effects": any(e["applied_at"] for e in tables["sale_effects"]),
                                 "buys": any(i["side"] == "buy" for i in tables["intents"]),
                                 "tp_clears": bool(tables["tp_clears"]),
                                 "snapshot": bool(tables["portfolio_snapshots"])},
                             "working_home_is_not_evidence": True, "inbound_commands_excluded": True},
            }
        if code:
            bundle["result"]["orders_may_have_been_sent"] = orders_may_have_been_sent(created, started=started)
        write_bundle(run_dir, bundle, config)
        print(f"Acceptance exit code: {code}; evidence: {run_dir / 'evidence'}")
        return code
    except Exception as exc:
        code = 2 if isinstance(exc, Refusal) else 1
        # Refusal messages are fixed local literals; arbitrary exception details never escape.
        reason = str(exc) if isinstance(exc, Refusal) else "acceptance failed; inspect working DB locally before any rerun"
        try:
            minimal_failure(created, code=code, status=status, started=started, reason=reason,
                            exc=exc, config=config)
        except Exception:
            print("acceptance failed: minimal evidence write failed", file=sys.stderr)
        return code
    finally:
        set_host(previous_host)
        if previous_home is None:
            os.environ.pop("NIGHTWATCH_HOME", None)
        else:
            os.environ["NIGHTWATCH_HOME"] = previous_home


if __name__ == "__main__":
    raise SystemExit(main())
