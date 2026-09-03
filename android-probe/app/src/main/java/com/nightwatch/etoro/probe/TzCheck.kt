package com.nightwatch.etoro.probe

import java.time.LocalDateTime
import java.time.ZoneId
import java.time.ZoneOffset

/**
 * Kotlin/java.time side of task requirement 6. Android 10+ ships tz rules as a Play-updatable
 * APEX module (DESIGN §1.2) — this checks that path independently of Chaquopy's zoneinfo
 * (PyBridge.tzCheck / probe/checks.py), since the two runtimes source tzdata differently.
 */
object TzCheck {
    fun run(): String {
        return try {
            val zone = ZoneId.of("America/New_York")
            val est = LocalDateTime.of(2026, 1, 15, 15, 30).atZone(zone).offset
            val edt = LocalDateTime.of(2026, 7, 15, 15, 30).atZone(zone).offset
            val estOk = est == ZoneOffset.of("-05:00")
            val edtOk = edt == ZoneOffset.of("-04:00")
            val verdict = if (estOk && edtOk) "PASS" else "MISMATCH"
            "$verdict: EST(2026-01-15)=$est (expect -05:00, ok=$estOk), " +
                "EDT(2026-07-15)=$edt (expect -04:00, ok=$edtOk)"
        } catch (e: Exception) {
            "FAIL: ${e.javaClass.simpleName}: ${e.message}"
        }
    }
}
