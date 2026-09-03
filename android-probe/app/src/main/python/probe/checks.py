"""
Chaquopy smoke check + tzdata verification (task requirements 5 and 6).

Deliberately light: no numpy/pandas here (proven separately in the chaquopy-probe throwaway
build documented in doc/RUNBOOK_INFRA.md Part G.7 — this probe only needs to prove the embedded
CPython runtime survives a cold wake and that America/New_York resolves correctly).
"""

import os
import sys
import time
import platform
import sqlite3
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
    _ZONEINFO_IMPORT_ERROR = None
except Exception as e:  # pragma: no cover - defensive, zoneinfo is stdlib since 3.9
    ZoneInfo = None
    _ZONEINFO_IMPORT_ERROR = repr(e)


def smoke(counter):
    """Trivial call proving the embedded interpreter runs after a cold wake, not just when the
    app is open. `counter` is a small int passed from Kotlin; echoed back with cheap arithmetic
    so a caller can sanity-check the round trip actually happened."""
    return (
        "python=%s impl=%s platform=%s counter_in=%s computed=%s"
        % (sys.version.split()[0], platform.python_implementation(), sys.platform,
           counter, counter * 2 + 1)
    )


def tz_check():
    """
    Resolves America/New_York via zoneinfo and reports the UTC offset for a known EST date
    (2026-01-15, should be UTC-05:00) and a known EDT date (2026-07-15, should be UTC-04:00).

    DESIGN §1.2/§1.3: Android ships tzdata as an APEX module updated via Google Play system
    updates, independently of the OS image. The device's Play system update is ~10 months stale
    (task brief). Python's zoneinfo on Android may NOT find system tzdata the way it does on
    desktop Linux, because CPython's zoneinfo looks for the platform's tz database in specific
    filesystem locations (or falls back to the `tzdata` pip package if importable) rather than
    talking to Android's APEX module through any Java-level API. The build.gradle.kts for this
    probe therefore bundles the pure-Python `tzdata` package unconditionally as a safety net —
    this function reports whether ZoneInfo resolution actually succeeded and, if so, whether the
    `tzdata` package was importable (a signal of which source satisfied it), so the on-device UI
    can show the ground truth rather than an assumption.
    """
    if ZoneInfo is None:
        return "FAIL: zoneinfo could not be imported: %s" % _ZONEINFO_IMPORT_ERROR

    tzdata_pkg_available = False
    try:
        import tzdata  # noqa: F401  (the bundled pip package; presence alone is informative)
        tzdata_pkg_available = True
    except Exception:
        tzdata_pkg_available = False

    try:
        zone = ZoneInfo("America/New_York")
    except Exception as e:
        return (
            "FAIL: ZoneInfo('America/New_York') raised %r "
            "(tzdata pip package importable=%s) -- this is exactly the silent-wrong-trade-time "
            "failure mode DESIGN warns about; the 'tzdata' pip dependency in build.gradle.kts "
            "is required here, not optional." % (e, tzdata_pkg_available)
        )

    def offset_for(iso_date):
        dt = datetime.fromisoformat(iso_date + "T15:30:00").replace(tzinfo=zone)
        return dt.utcoffset()

    try:
        est_offset = offset_for("2026-01-15")  # standard time, expect -05:00
        edt_offset = offset_for("2026-07-15")  # daylight time, expect -04:00

        def fmt(td):
            total_minutes = int(td.total_seconds() // 60)
            sign = "-" if total_minutes < 0 else "+"
            total_minutes = abs(total_minutes)
            return "%s%02d:%02d" % (sign, total_minutes // 60, total_minutes % 60)

        est_str = fmt(est_offset)
        edt_str = fmt(edt_offset)
        est_ok = est_str == "-05:00"
        edt_ok = edt_str == "-04:00"
        verdict = "PASS" if (est_ok and edt_ok) else "MISMATCH"
        return (
            "%s: EST(2026-01-15)=%s (expect -05:00, ok=%s), "
            "EDT(2026-07-15)=%s (expect -04:00, ok=%s), "
            "tzdata_pip_package_importable=%s"
            % (verdict, est_str, est_ok, edt_str, edt_ok, tzdata_pkg_available)
        )
    except Exception as e:
        return "FAIL: offset computation raised %r" % e


def numpy_pandas_check():
    """
    v2 check 4: numpy/pandas were deliberately omitted from v1 (proven only at BUILD time in a
    throwaway project, never at RUNTIME in an APK actually installed on the phone). The real
    strategy's cone_5_z signal (DESIGN §1.4) needs a 260-bar rolling population standard
    deviation — this computes exactly that operation on synthetic data and reports the result and
    elapsed time, so it is a genuine runtime proof, not just "pip install didn't error".
    """
    try:
        import numpy as np
        import pandas as pd
    except Exception as e:
        return "FAIL: numpy/pandas import raised %r" % e

    try:
        t0 = time.monotonic()
        rng = np.random.default_rng(seed=42)
        # Synthetic daily-return-like series, same shape as the real cone_5_z input (DESIGN §1.4:
        # 260 lookback + 5 shift = 265 minimum bars; use a generous 400 to mirror the real seed).
        n = 400
        series = pd.Series(rng.normal(loc=0.0004, scale=0.01, size=n))
        rolling_std = series.rolling(window=260).std(ddof=0)  # population stdev, matches cone_5_z
        last_value = rolling_std.iloc[-1]
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        if last_value is None or (hasattr(last_value, "__float__") and last_value != last_value):
            return "FAIL: rolling std computation produced NaN/None on a full 400-bar series"
        return (
            "PASS: numpy=%s pandas=%s 260-bar rolling population stdev (last value)=%.8f "
            "elapsed_ms=%.2f n_bars=%d"
            % (np.__version__, pd.__version__, float(last_value), elapsed_ms, n)
        )
    except Exception as e:
        return "FAIL: rolling std computation raised %r" % e


def python_sqlite_check(cache_dir):
    """
    v2 check 5: the real app's DB layer is Python (DESIGN §2/§3, db/repo.py). Confirms Python's
    stdlib sqlite3 works inside Chaquopy's embedded interpreter: create, write, read, WAL.
    """
    db_path = os.path.join(cache_dir, "probe_python_sqlite_check.db")
    try:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass

        conn = sqlite3.connect(db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
            with conn:
                conn.execute("INSERT INTO t (v) VALUES (?)", ("hello-from-python",))
            row = conn.execute("SELECT v FROM t LIMIT 1").fetchone()
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            ok = row is not None and row[0] == "hello-from-python" and str(mode).lower() == "wal"
            verdict = "PASS" if ok else "FAIL"
            return "%s: sqlite3.version=%s journal_mode=%s read_back=%s" % (
                verdict, sqlite3.sqlite_version, mode, row[0] if row else None
            )
        finally:
            conn.close()
    except Exception as e:
        return "FAIL: %r" % e
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass
