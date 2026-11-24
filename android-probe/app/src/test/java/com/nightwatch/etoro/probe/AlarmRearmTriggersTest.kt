package com.nightwatch.etoro.probe

import org.junit.Assert.*
import org.junit.Test
import java.io.File

/** T075: every event that can silently drop or misplace the session alarm re-arms it. */
class AlarmRearmTriggersTest {
    @Test
    fun fourBroadcastsRearmAndEverythingElseIsIgnored() {
        assertEquals("BOOT", BootReceiver.reasonFor("android.intent.action.BOOT_COMPLETED"))
        assertEquals("UPDATE", BootReceiver.reasonFor("android.intent.action.MY_PACKAGE_REPLACED"))
        assertEquals("TIME_SET", BootReceiver.reasonFor("android.intent.action.TIME_SET"))
        assertEquals("TIMEZONE", BootReceiver.reasonFor("android.intent.action.TIMEZONE_CHANGED"))
        for (other in listOf(null, "", "android.intent.action.BATTERY_LOW", "com.evil.REARM")) {
            assertNull(BootReceiver.reasonFor(other))
        }
    }

    @Test
    fun manifestDeclaresAllFourActionsOnTheReceiver() {
        val main = listOf(File("src/main"), File("app/src/main")).first { it.isDirectory }
        val manifest = File(main, "AndroidManifest.xml").readText()
        val block = manifest.substringAfter("android:name=\".BootReceiver\"").substringBefore("</receiver>")
        for (action in listOf("BOOT_COMPLETED", "MY_PACKAGE_REPLACED", "TIME_SET", "TIMEZONE_CHANGED")) {
            assertTrue(action, block.contains("android.intent.action.$action"))
        }
    }
}
