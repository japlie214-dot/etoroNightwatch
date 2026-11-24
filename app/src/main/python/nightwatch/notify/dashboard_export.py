"""Read-only, bounded DEMO dashboard export; never forward broker payloads."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from pathlib import Path

from ..db.repo import Repo
from ..logging_setup import RedactionFilter

MANAGED = ("UPRO", "SCHG")


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def _reason(value):
    return RedactionFilter.redact(value)[:300] if isinstance(value, str) else None


def dashboard_snapshot(repo: Repo, *, history: int = 30) -> dict:
    """Page-shaped allowlist in one read transaction; no session yields {}."""
    conn = repo.conn
    with conn:
        conn.execute("BEGIN")
        session = repo.latest_session()
        if session is None or session.environment != "demo":
            return {}
        decision = repo.get_decision(session.id)
        last = {
            "trading_date": session.trading_date, "state": session.state,
            "armed": session.armed, "preflight": session.preflight_verdict,
            "failed_gate": session.preflight_failed_gate, "next_run_at": session.next_run_at,
            "snapshot_taken_at": None, "snapshot_complete": None,
            "omitted_managed_positions": 0,
        }
        result = {"environment": "demo", "trading_date": session.trading_date,
                  "last_session": last, "recent_sessions": []}
        if decision is not None:
            result["last_decision"] = {
                "reason": _reason(decision.reason), "intents": len(decision.intents),
                "cone_z": _number(decision.cone_z), "spy_drawdown": _number(decision.drawdown),
                "tier": _number(decision.tier), "budget_usd": _number(decision.budget),
                "order_size_usd": _number(decision.order_size),
            }
        rows = conn.execute(
            "SELECT id, trading_date, armed, state FROM sessions WHERE environment = 'demo' "
            "ORDER BY trading_date DESC, id DESC LIMIT ?", (max(0, min(history, 30)),),
        ).fetchall()
        for row in rows:
            d = repo.get_decision(row["id"])
            orders = conn.execute("SELECT COUNT(*) FROM orders WHERE session_id = ?",
                                  (row["id"],)).fetchone()[0]
            result["recent_sessions"].append({
                "trading_date": row["trading_date"], "armed": bool(row["armed"]),
                "state": row["state"], "reason": _reason(d.reason) if d else None,
                "orders": orders,
            })
        snap = conn.execute(
            "SELECT id, taken_at, equity, cash, available_cash, raw_json "
            "FROM portfolio_snapshots WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (session.id,),
        ).fetchone()
        if snap is None:
            return result
        metadata = json.loads(snap["raw_json"] or "{}")
        last["snapshot_taken_at"] = snap["taken_at"] or None
        complete = metadata.get("complete")
        last["snapshot_complete"] = complete if type(complete) is bool else None
        result["account"] = {
            "total_equity_usd": _number(snap["equity"]),
            "total_cash_usd": _number(snap["cash"]),
            "broker_available_cash_usd": _number(snap["available_cash"]),
        }
        stored_sleeve = metadata.get("sleeve", {})
        sleeve = {target: _number(stored_sleeve.get(source)) for source, target in (
            ("cash_reserve_usd", "cash_reserve_usd"),
            ("sleeve_cash", "sleeve_cash_usd"), ("sleeve_equity", "sleeve_equity_usd"),
        )}
        if decision is not None:
            for name in ("cone_z", "spy_drawdown", "tier", "budget_usd", "order_size_usd"):
                sleeve[name] = result["last_decision"][name]
        totals = conn.execute(
            "SELECT COUNT(*), COUNT(unrealized_pnl), SUM(unrealized_pnl) "
            "FROM portfolio_snapshot_positions WHERE snapshot_id = ? AND symbol IN (?, ?)",
            (snap["id"], *MANAGED),
        ).fetchone()
        sleeve["sleeve_unrealized_pnl_usd"] = (
            _number(totals[2]) if complete is True and totals[0] == totals[1] else None
        )
        result["sleeve"] = sleeve
        positions = conn.execute(
            "SELECT symbol, units, entry_price, amount, unrealized_pnl, open_date "
            "FROM portfolio_snapshot_positions WHERE snapshot_id = ? AND symbol IN (?, ?) "
            "ORDER BY symbol, open_date, id LIMIT 230", (snap["id"], *MANAGED),
        ).fetchall()
        last["omitted_managed_positions"] = max(0, totals[0] - len(positions))
        result["positions"] = []
        for p in positions:
            amount, pnl = _number(p["amount"]), _number(p["unrealized_pnl"])
            result["positions"].append({
                "symbol": p["symbol"], "is_managed": True, "units": _number(p["units"]),
                "entry_price_usd": _number(p["entry_price"]),
                "market_value_usd": amount + pnl if amount is not None and pnl is not None else None,
                "unrealized_pnl_usd": pnl, "open_date": p["open_date"],
            })
        return result


def export_json(home: str) -> str:
    """Read an existing demo DB with mode=ro; SQLite may manage WAL/SHM sidecars."""
    try:
        root = Path(home)
        if root.name != "nightwatch_demo":
            return "{}"
        uri = (root / "data" / "nightwatch.db").resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=1)
        try:
            conn.row_factory = sqlite3.Row
            result = dashboard_snapshot(Repo(conn))
        finally:
            conn.close()
        if result:
            for name in ("SEED_DEMO.adopt.done", "SEED_DEMO.done"):
                path = root / name
                if not path.is_file():
                    continue
                try:
                    status = json.loads(path.read_text(encoding="utf-8")).get("status")
                    if isinstance(status, str) and re.fullmatch(r"[a-z_]{1,40}", status):
                        result["seed"] = {"status": status}
                except (OSError, ValueError, AttributeError):
                    pass
                break
        return json.dumps(result, ensure_ascii=False, allow_nan=False)
    except Exception:
        return "{}"
