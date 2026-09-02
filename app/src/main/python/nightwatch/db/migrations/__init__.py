"""Forward-only schema migration runner.

Version 1 is ``db/schema.sql`` (the baseline).  Every later change is a file in
this package named ``NNN_description.sql`` with a zero-padded three-digit
version, applied in numeric order, exactly once, inside a transaction.  There is
no down-migration: rolling back a schema change on a device that has already
traded is not a thing we want to be able to do quickly.

Files are read through :mod:`importlib.resources` rather than
:data:`__file__`, because on Chaquopy the package may live inside the APK's
asset zip and have no real filesystem path.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import resources

__all__ = ["Migration", "BASELINE_VERSION", "current_version", "discover", "migrate"]

#: The version ``schema.sql`` establishes.
BASELINE_VERSION = 1

_FILENAME = re.compile(r"^(\d{3})_([A-Za-z0-9_]+)\.sql$")


@dataclass(frozen=True, slots=True)
class Migration:
    """One numbered forward migration."""

    version: int
    name: str
    sql: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )


def current_version(conn: sqlite3.Connection) -> int:
    """Return the highest applied schema version, or 0 for an empty database."""
    _ensure_version_table(conn)
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    value = row[0] if row is not None else None
    return int(value) if value is not None else 0


def _baseline() -> Migration:
    sql = resources.files("nightwatch.db").joinpath("schema.sql").read_text(
        encoding="utf-8"
    )
    return Migration(version=BASELINE_VERSION, name="baseline", sql=sql)


def discover() -> list[Migration]:
    """Return every known migration, baseline first, in version order."""
    found: list[Migration] = [_baseline()]
    package = resources.files(__name__)
    for entry in package.iterdir():
        match = _FILENAME.match(entry.name)
        if match is None:
            continue
        version = int(match.group(1))
        if version <= BASELINE_VERSION:
            raise RuntimeError(
                f"migration {entry.name} uses version {version}, which is reserved "
                f"for the baseline schema"
            )
        found.append(
            Migration(
                version=version,
                name=match.group(2),
                sql=entry.read_text(encoding="utf-8"),
            )
        )
    found.sort(key=lambda m: m.version)
    versions = [m.version for m in found]
    if len(set(versions)) != len(versions):
        raise RuntimeError(f"duplicate migration versions: {versions}")
    return found


def migrate(conn: sqlite3.Connection) -> int:
    """Bring ``conn`` up to the newest schema version.

    Each migration's DDL runs inside an explicit ``BEGIN``/``COMMIT`` wrapped
    around the script — SQLite DDL is transactional, so a failed migration
    leaves no half-built schema.  The ``schema_version`` row is then written as
    a separate parameterised statement.  The narrow gap between the two is
    covered by requiring every migration to be idempotent (``IF NOT EXISTS``,
    as the baseline is): a crash in the gap simply re-runs the migration next
    time, which is a no-op, and the version is then recorded.  It errs towards
    "run twice", never towards "recorded but not applied".

    Returns:
        The schema version after migrating.
    """
    version = current_version(conn)
    for migration in discover():
        if migration.version <= version:
            continue
        # executescript() issues an implicit COMMIT first, so the transaction
        # has to be declared inside the script text, not around the call.
        conn.executescript(f"BEGIN;\n{migration.sql}\nCOMMIT;")
        conn.execute(
            "INSERT OR REPLACE INTO schema_version (version, name, applied_at) "
            "VALUES (?, ?, ?)",
            (migration.version, migration.name, _now()),
        )
        conn.commit()
        version = migration.version
    return version
