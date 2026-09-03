"""
Chaquopy smoke check + tzdata verification (task requirements 5 and 6).

Deliberately light: no numpy/pandas here (proven separately in the chaquopy-probe throwaway
build documented in doc/RUNBOOK_INFRA.md Part G.7 — this probe only needs to prove the embedded
CPython runtime survives a cold wake and that America/New_York resolves correctly).
"""

import sys
import platform
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
