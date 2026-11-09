package com.nightwatch.etoro.probe

import java.io.File
import org.junit.Assert.*
import org.junit.Test

/** Source-level wiring checks; the embedded Python runtime still needs device proof. */
class AdoptionWiringTest {
    private fun source(path: String): String {
        val main = listOf(File("src/main"), File("app/src/main")).first { it.isDirectory }
        return File(main, path).readText()
    }

    @Test
    fun bridgePassesHomeAndReviewedPlanIdToSeparatePythonEntries() {
        val bridge = source("java/com/nightwatch/etoro/probe/PyBridge.kt")
        val plan = bridge.substringAfter("fun planAdoption(").substringBefore("fun applyAdoption(")
        val apply = bridge.substringAfter("fun applyAdoption(").substringBefore("fun runTakeProfitProbe(")
        assertTrue(plan.contains("getModule(\"probe.adopt_apply\")"))
        assertTrue(plan.contains(".callAttr(\"plan\", home)"))
        assertTrue(apply.contains(".callAttr(\"apply\", home, planId)"))
        assertTrue(plan.contains("synchronized(sessionLock)"))
        assertTrue(apply.contains("synchronized(sessionLock)"))
    }

    @Test
    fun demoApplyRequiresTheConfirmationTapAndStartsDisabled() {
        val activity = source("java/com/nightwatch/etoro/probe/MainActivity.kt")
        assertTrue(activity.contains("File(filesDir, \"nightwatch_demo\")"))
        assertTrue(activity.contains(".setPositiveButton(\"Apply adoption\") { _, _ -> runAdoptionNow(planId) }"))
        assertEquals(1, Regex("runAdoptionNow\\(planId\\)").findAll(activity).count())
        val layout = source("res/layout/activity_main.xml")
        val applyButton = layout.substringAfter("@+id/btnApplyAdoption").substringBefore("/>")
        assertTrue(applyButton.contains("android:enabled=\"false\""))
        for (file in listOf("WakeService.kt", "SessionAlarmReceiver.kt")) {
            val automatic = source("java/com/nightwatch/etoro/probe/$file")
            assertFalse(automatic.contains("Adoption("))
            assertFalse(automatic.contains("probe.adopt_apply"))
        }
    }
}
