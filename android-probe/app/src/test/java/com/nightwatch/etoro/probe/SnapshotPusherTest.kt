package com.nightwatch.etoro.probe

import java.io.File
import org.junit.Assert.*
import org.junit.Test

/** Source wiring checks; JSON merge execution additionally needs a JVM org.json runtime. */
class SnapshotPusherTest {
    private fun source(name: String): String {
        val main = listOf(File("src/main"), File("app/src/main")).first { it.isDirectory }
        return File(main, "java/com/nightwatch/etoro/probe/$name").readText()
    }

    @Test
    fun exportUsesOnlyDemoHomeAndKeepsOriginalFields() {
        val build = source("SnapshotPusher.kt").substringAfter("fun buildSnapshot(")
            .substringBefore("internal fun mergeDashboard(")
        assertTrue(build.contains("JSONObject(snapshotFields("))
        assertTrue(build.contains("File(context.filesDir, \"nightwatch_demo\")"))
        assertTrue(build.indexOf("ARM_DEMO") < build.indexOf("PyBridge.dashboardSnapshot"))
        assertTrue(build.contains("return original"))
        assertFalse(build.contains("nightwatch_config"))
    }

    @Test
    fun mergeHasNineKeysReasonSanitizationAndUtf8Fallback() {
        val merge = source("SnapshotPusher.kt").substringAfter("internal fun mergeDashboard(")
            .substringBefore("internal fun snapshotFields(")
        val keys = merge.substringAfter("for (key in listOf(").substringBefore(")) {")
        assertEquals(listOf("environment", "trading_date", "account", "sleeve", "positions",
            "last_session", "last_decision", "recent_sessions", "seed"),
            Regex("\"([^\"]+)\"").findAll(keys).map { it.groupValues[1] }.toList())
        assertTrue(merge.contains("extra.opt(\"environment\") != \"demo\""))
        assertTrue(merge.contains("reason?.let(redact)?.take(300)"))
        assertTrue(merge.contains("obj.remove(\"reason\")"))
        assertTrue(merge.contains("toByteArray(Charsets.UTF_8).size > 256 * 1024"))
        assertTrue(merge.contains("failed(\"PayloadTooLarge\")"))
        assertTrue(merge.contains("failed(e.javaClass.simpleName)"))
        assertFalse(merge.contains("e.message"))
    }

    @Test
    fun bridgeIsReadOnlyAndDoesNotTakeSessionLock() {
        val bridge = source("PyBridge.kt").substringAfter("fun dashboardSnapshot(")
            .substringBefore("// Separate monitor")
        assertTrue(bridge.contains("bridgeLock.withLock"))
        assertTrue(bridge.contains("nightwatch.notify.dashboard_export"))
        assertTrue(bridge.contains("callAttr(\"export_json\", home)"))
        assertFalse(bridge.contains("sessionLock"))
        assertFalse(bridge.contains("e.message"))
    }

    @Test
    fun secondPushFollowsSessionFinalizationInsideAttemptBranch() {
        val wake = source("WakeService.kt")
        assertEquals(2, Regex("SnapshotPusher.pushNow\\(this, checkResults\\)").findAll(wake).count())
        val work = wake.substringAfter("// 5b.").substringBefore("store.log(\"WAKE_END\"")
        val push = work.indexOf("SnapshotPusher.pushNow")
        assertTrue(work.indexOf("sessionRunning.compareAndSet(false, true)") < push)
        assertTrue(work.indexOf("runSessionDry()") < push)
        assertTrue(work.indexOf("sessionRunning.set(false)") < push)
        assertTrue(work.substring(push).contains("catch (e: Exception)"))
        assertTrue(work.trimEnd().endsWith("}\n            }"))
    }
}
