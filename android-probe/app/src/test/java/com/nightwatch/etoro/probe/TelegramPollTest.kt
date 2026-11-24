package com.nightwatch.etoro.probe

import org.junit.Assert.*
import org.junit.Test
import java.io.File

/** Source wiring checks; the alarm itself needs a device. */
class TelegramPollTest {
    private fun main() = listOf(File("src/main"), File("app/src/main")).first { it.isDirectory }
    private fun src(name: String) = File(main(), "java/com/nightwatch/etoro/probe/$name").readText()

    @Test
    fun intervalRespectsDozeAndFirstPollIsSoon() {
        assertTrue(TelegramPollScheduler.INTERVAL_MS >= 9 * 60 * 1000L)
        assertTrue(TelegramPollScheduler.FIRST_DELAY_MS in 1_000L..TelegramPollScheduler.INTERVAL_MS)
    }

    @Test
    fun receiverRearmsFirstAndIsRegisteredNotExported() {
        val receiver = src("TelegramPoll.kt").substringAfter("class TelegramPollReceiver")
        assertTrue(receiver.indexOf("TelegramPollScheduler.schedule(context)") < receiver.indexOf("goAsync()"))
        assertTrue(receiver.contains("wake.release()") && receiver.contains("pending.finish()"))
        val manifest = File(main(), "AndroidManifest.xml").readText()
        val block = manifest.substringAfter("android:name=\".TelegramPollReceiver\"").substringBefore("/>")
        assertTrue(block.contains("android:exported=\"false\""))
        assertTrue(src("NightwatchApp.kt").contains("TelegramPollScheduler.ensureScheduled(this)"))
    }

    @Test
    fun pollNeverBlocksBehindOrAheadOfASessionAndTouchesNoTradingPath() {
        val bridge = src("PyBridge.kt").substringAfter("fun pollTelegram(").substringBefore("/** Manual adoption")
        assertTrue(bridge.contains("sessionLock.tryLock()") && bridge.contains("bridgeLock.tryLock()"))
        assertTrue(bridge.contains("poll_telegram"))
        for (forbidden in listOf("operator_write_authorized", "run_session", "demo_manual_buy", "apply_demo_control")) {
            assertFalse(bridge.contains(forbidden))
        }
        val receiver = src("TelegramPoll.kt")
        assertFalse(receiver.contains("e.message"))
    }

    @Test
    fun aLostAlarmIsRearmedEvenWhenItsPendingIntentSurvives() {
        val t = 1_000_000L
        assertTrue(TelegramPollScheduler.shouldRearm(false, t + 5_000, t))   // no pending intent
        assertTrue(TelegramPollScheduler.shouldRearm(true, 0L, t))           // never recorded
        assertTrue(TelegramPollScheduler.shouldRearm(true, t - 3 * 60_000, t)) // overdue: lost
        assertFalse(TelegramPollScheduler.shouldRearm(true, t + 60_000, t))  // pending, not yet due
        assertFalse(TelegramPollScheduler.shouldRearm(true, t - 60_000, t))  // slightly late, within grace
    }
}
