"""Operator-supervised adoption; only demo_seed may call unattended from an alarm.

Only the broker read role is used; apply writes local lot annotations, not orders.
The single saved plan belongs to this home and expires after thirty minutes.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

from nightwatch.android_entry import _session_lock
from nightwatch.config import load_config
from nightwatch.db import migrations
from nightwatch.db.repo import Instrument, Repo, default_db_path
from nightwatch.etoro.client import EtoroClient
from nightwatch.host import DesktopHost, get_host, set_host
from nightwatch.market.adopt import AdoptionError, run_adoption_backfill
from nightwatch.market.source import TiingoClient
from nightwatch.market.window import FULL_HISTORY_START_DATE

PLAN_FILE = "adoption_plan.json"
MAX_PLAN_AGE_SECONDS = 30 * 60


class AdoptionRefused(Exception):
    """A local refusal whose text is safe to show to the operator."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fingerprint(positions) -> str:
    # Raw payload can contain changing marks; only the parsed position state is bound.
    rows = [{k: v for k, v in asdict(p).items() if k != "raw"} for p in positions]
    rows.sort(key=lambda row: row["position_id"])
    return hashlib.sha256(json.dumps(rows, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _account_marker(positions) -> str:
    if not positions:
        raise AdoptionRefused("nothing to adopt: empty portfolio has no account marker")
    markers = []
    for position in positions:
        cid = position.raw.get("CID")
        if isinstance(cid, bool) or not isinstance(cid, (str, int)) or not str(cid).strip():
            raise AdoptionRefused("portfolio position is missing its CID account marker")
        markers.append(str(cid))
    if len(set(markers)) != 1:
        raise AdoptionRefused("portfolio positions disagree on the CID account marker")
    return markers[0]


def _fresh(saved, plan_id):
    try:
        if not isinstance(saved, dict) or saved["plan_id"] != plan_id:
            raise AdoptionRefused("plan ID mismatch; plan again")
        created = datetime.fromisoformat(saved["created_at"])
        age = (_now() - created).total_seconds()
        if not 0 <= age <= MAX_PLAN_AGE_SECONDS:
            raise AdoptionRefused("stale plan (or clock moved backwards); plan again")
    except (KeyError, TypeError, ValueError) as exc:
        raise AdoptionRefused("invalid saved plan; plan again") from exc


def _database_fingerprint(repo):
    state = dict(
        instruments=[asdict(i) for i in sorted(repo.list_instruments(), key=lambda i: i.symbol)],
        annotations=[asdict(a) for a in sorted(repo.list_lot_annotations(), key=lambda a: a.position_id)],
    )
    return hashlib.sha256(json.dumps(state, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _open_for_apply(path):
    if not path.is_file():
        raise AdoptionRefused("database is missing; initialize it and plan again")
    # No create, migrations or persistent PRAGMA changes on an apply/refusal path.
    conn = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=30.0)
    try:
        try:
            row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
            if row[0] != migrations.discover()[-1].version:
                raise AdoptionRefused("database is not at the current schema; migrate separately and plan again")
        except sqlite3.DatabaseError as exc:
            raise AdoptionRefused("database is not at the current schema; migrate separately and plan again") from exc
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return Repo(conn)
    except BaseException:
        conn.close()
        raise


def _save(path, saved):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(saved, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def plan(home: str) -> str:
    """Fetch, dry-run, persist and print the plan; return JSON for the UI."""
    return _run(home, None)


def apply(home: str, plan_id: str) -> str:
    """Apply only the exact fresh plan the operator reviewed."""
    if not plan_id:
        return _result("refused", "missing plan ID; plan again")
    return _run(home, plan_id)


def _result(status, report, plan_id=None):
    print(report, flush=True)
    return json.dumps(dict(status=status, plan_id=plan_id, report=report))


def _run(home, plan_id):
    previous_host = get_host()
    stage = "setup"
    try:
        host = DesktopHost(Path(home))
        set_host(host)
        db_path = default_db_path(host).resolve()
        with _session_lock(host.data_dir() / "session.lock") as acquired:
            if not acquired:
                raise AdoptionRefused("session or adoption already running; try again when idle")
            path = host.root / PLAN_FILE
            saved = None
            if plan_id is not None:
                try:
                    saved = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise AdoptionRefused("missing or unreadable saved plan; plan again") from exc
                _fresh(saved, plan_id)
                if saved.get("home_path") != str(host.root) or saved.get("db_path") != str(db_path):
                    raise AdoptionRefused("home or database path mismatch; plan again in this home")
            stage = "configuration load"
            config = load_config(host=host)
            environment = config.etoro.environment.value
            # The UI's dedicated demo home must never silently select the real account.
            if host.root.name == "nightwatch_demo" and environment != "demo":
                raise AdoptionRefused("demo home requires demo environment")
            with ExitStack() as resources:
                stage = "database open"
                repo = Repo.open(db_path, host=host) if saved is None else _open_for_apply(db_path)
                resources.callback(repo.close)
                baseline = _database_fingerprint(repo)
                stage = "portfolio read"
                client = EtoroClient(config.etoro, role="read", repo=None)
                resources.callback(client.close)
                positions = sorted(client.get_portfolio_strict(), key=lambda p: p.position_id)
                marker = _account_marker(positions)
                if len({p.position_id for p in positions}) != len(positions):
                    raise AdoptionRefused("portfolio has duplicate position IDs")
                fingerprint = _fingerprint(positions)
                if saved is not None:
                    if saved.get("environment") != environment or saved.get("account_marker") != marker:
                        raise AdoptionRefused("account or environment mismatch; plan again")
                    if saved.get("positions_fingerprint") != fingerprint:
                        raise AdoptionRefused("portfolio changed; plan again")
                stage = "instrument resolution"
                instruments = list(repo.list_instruments())
                pending = []
                symbols = {i.symbol for i in instruments}
                ids = {i.instrument_id for i in instruments}
                for symbol in config.symbols.traded:
                    if symbol not in symbols:
                        iid = client.resolve_instrument_id(symbol)
                        if iid in ids:
                            raise AdoptionRefused("instrument metadata conflicts with this home's cache")
                        pending.append(Instrument(symbol=symbol, instrument_id=iid))
                        symbols.add(symbol)
                        ids.add(iid)
                unknown = sorted({p.instrument_id for p in positions} - ids)
                if unknown:
                    for row in client.get_instruments(unknown):
                        symbol, iid = row["symbolFull"], int(row["instrumentID"])
                        if (not isinstance(symbol, str) or not symbol or iid not in unknown
                                or symbol in symbols or iid in ids):
                            raise AdoptionRefused("instrument metadata conflicts with this home's cache")
                        pending.append(Instrument(symbol=symbol, instrument_id=iid))
                        symbols.add(symbol)
                        ids.add(iid)
                instruments.extend(pending)
                stage = "SPY history read"
                tiingo = TiingoClient(config.tiingo)
                resources.callback(tiingo.close)
                bars = tiingo.get_eod_history("SPY", start_date=FULL_HISTORY_START_DATE)
                stage = "adoption resolution"
                annotations = repo.list_lot_annotations()
                snapshot = SimpleNamespace(list_instruments=lambda: instruments,
                                           list_lot_annotations=lambda: annotations)
                adoption = run_adoption_backfill(
                    snapshot, positions, bars, managed_symbols=config.symbols.traded, dry_run=True,
                )
                resolved = asdict(adoption)
                if saved is None:
                    saved = dict(
                        plan_id=uuid4().hex, created_at=_now().isoformat(),
                        home_path=str(host.root), db_path=str(db_path),
                        environment=environment, account_marker=marker,
                        positions_fingerprint=fingerprint, database_fingerprint=baseline,
                        adoption=resolved,
                        report=adoption.report(),
                    )
                    stage = "plan persistence"
                    _save(path, saved)
                    return _result("planned", saved["report"], saved["plan_id"])
                _fresh(saved, plan_id)
                if (saved.get("adoption") != json.loads(json.dumps(resolved))
                        or saved.get("database_fingerprint") != baseline):
                    raise AdoptionRefused("adoption inputs or annotations changed; plan again")
                stage = "annotation write"
                with repo.conn:
                    repo.conn.execute("BEGIN IMMEDIATE")
                    if _database_fingerprint(repo) != baseline:
                        raise AdoptionRefused("database changed during validation; plan again")
                    _fresh(saved, plan_id)
                    # upsert_instrument commits per call; new cache rows must join the bulk insert's commit.
                    repo.conn.executemany(
                        "INSERT INTO instruments (symbol, instrument_id, metadata_json, updated_at) "
                        "VALUES (?, ?, ?, ?)",
                        [(i.symbol, i.instrument_id, "{}", _now().isoformat()) for i in pending],
                    )
                    adoption = run_adoption_backfill(
                        repo, positions, bars, managed_symbols=config.symbols.traded, dry_run=False,
                    )
                return _result("applied", adoption.report(), plan_id)
    except AdoptionRefused as exc:
        return _result("refused", f"Adoption refused: {exc}")
    except AdoptionError as exc:
        return _result("refused", f"Adoption refused during resolution: {exc}")
    except Exception as exc:
        # Foreign exception bodies can contain credentials; expose the boundary, not the body.
        return _result("error", f"Adoption failed during {stage} ({type(exc).__name__}); nothing further attempted")
    finally:
        set_host(previous_host)
