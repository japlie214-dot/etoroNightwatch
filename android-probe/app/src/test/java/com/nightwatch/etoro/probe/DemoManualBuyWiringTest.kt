package com.nightwatch.etoro.probe

import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock
import org.junit.Assert.*
import org.junit.Test

/** Source wiring only; actual dialog and broker fills require PM/Boss device proof. */
class DemoManualBuyWiringTest {
    private fun source(path: String): String {
        val main = listOf(File("src/main"), File("app/src/main")).first { it.isDirectory }
        return File(main, path).readText()
    }

    @Test
    fun bridgeUsesTheSameMonitorsAsSessionsAndAdoption() {
        val bridge = source("java/com/nightwatch/etoro/probe/PyBridge.kt")
        val buy = bridge.substringAfter("fun runDemoManualBuy(").substringBefore("fun planAdoption(")
        assertTrue(buy.contains("withDemoManualBuyLocks"))
        assertTrue(buy.indexOf("withDemoManualBuyLocks") < buy.indexOf("ensureStarted(context)"))
        for (name in listOf("runSessionDry", "runSessionArmedDemo", "planAdoption", "applyAdoption")) {
            val entry = bridge.substringAfter("fun $name(").substringBefore("\n    }")
            assertTrue(entry.contains("sessionLock.withLock"))
        }
        for (name in listOf("planAdoption", "applyAdoption")) {
            val entry = bridge.substringAfter("fun $name(").substringBefore("\n    }")
            assertTrue(entry.contains("bridgeLock.withLock"))
        }
        assertFalse(bridge.contains("@Synchronized"))
        assertTrue(bridge.contains("fun ensureStarted(context: Context) = bridgeLock.withLock"))
        assertTrue(buy.contains("getModule(\"probe.demo_manual_buy\")"))
        assertTrue(buy.contains(".callAttr(\"run\", home, count, usdEach)"))
    }

    @Test
    fun onlyConfirmationTapCallsBackgroundBuyWithCountAmountAndTotal() {
        val activity = source("java/com/nightwatch/etoro/probe/MainActivity.kt")
        assertTrue(activity.contains(".setPositiveButton(\"Open demo positions\") { _, _ -> runDemoManualBuyNow(count, usdEach) }"))
        assertEquals(1, Regex("runDemoManualBuyNow\\(count, usdEach\\)").findAll(activity).count())
        val dialog = activity.substringAfter("binding.btnDemoManualBuy.setOnClickListener")
            .substringBefore("binding.btnPlanAdoption.setOnClickListener")
        assertTrue(dialog.contains("\$count SCHG market buys, USD \$usdEach each"))
        assertTrue(dialog.contains("total USD \${count * usdEach}"))
        assertTrue(dialog.contains("count !in 1..10"))
        assertTrue(dialog.contains("!usdEach.isFinite()"))
        assertTrue(dialog.contains("usdEach !in 50.0..15000.0"))
        val run = activity.substringAfter("private fun runDemoManualBuyNow(")
            .substringBefore("private fun runAdoptionNow(")
        assertTrue(run.contains("File(filesDir, \"nightwatch_demo\")"))
        assertTrue(run.contains("withContext(Dispatchers.IO)"))
        assertTrue(run.contains("PyBridge.runDemoManualBuy(this@MainActivity, home, count, usdEach)"))
        assertFalse(run.contains("e.message"))
        val layout = source("res/layout/activity_main.xml")
        assertTrue(layout.substringAfter("@+id/txtDemoBuyStatus").substringBefore("/>")
            .contains("android:textIsSelectable=\"true\""))
        assertTrue(layout.contains("@+id/editDemoBuyCount"))
        assertTrue(layout.contains("@+id/editDemoBuyUsd"))
    }

    @Test
    fun dialogAndInFlightGuardsPrecedeConfirmationAndBackgroundDispatch() {
        val activity = source("java/com/nightwatch/etoro/probe/MainActivity.kt")
        val dialog = activity.substringAfter("binding.btnDemoManualBuy.setOnClickListener")
            .substringBefore("binding.btnPlanAdoption.setOnClickListener")
        assertTrue(dialog.contains("demoBuyDialogOpen || demoBuyRunning || PyBridge.isDemoManualBuyBusy()"))
        assertTrue(dialog.indexOf("PyBridge.isDemoManualBuyBusy()") < dialog.indexOf("AlertDialog.Builder"))
        assertTrue(dialog.indexOf("demoBuyDialogOpen = true") < dialog.indexOf("AlertDialog.Builder"))
        assertTrue(dialog.contains(".setOnDismissListener { demoBuyDialogOpen = false }"))
        assertTrue(dialog.contains("binding.txtDemoBuyStatus.text = PyBridge.DEMO_BUY_BUSY"))
        val run = activity.substringAfter("private fun runDemoManualBuyNow(")
            .substringBefore("private fun runAdoptionNow(")
        assertTrue(run.contains("demoBuyRunning || PyBridge.isDemoManualBuyBusy()"))
        assertTrue(run.indexOf("demoBuyRunning = true") < run.indexOf("lifecycleScope.launch"))
        assertTrue(run.contains("demoBuyRunning = false"))
    }

    private fun lock(name: String): ReentrantLock = PyBridge::class.java.getDeclaredField(name).let {
        it.isAccessible = true
        it.get(PyBridge) as ReentrantLock
    }

    @Test(timeout = 5000)
    fun heldSessionOrBridgeLockRefusesImmediatelyWithoutExecutingOrQueueing() {
        for (name in listOf("sessionLock", "bridgeLock")) {
            val entered = CountDownLatch(1)
            val release = CountDownLatch(1)
            val executor = Executors.newSingleThreadExecutor()
            val holder = executor.submit {
                lock(name).withLock {
                    entered.countDown()
                    check(release.await(3, TimeUnit.SECONDS))
                }
            }
            try {
                assertTrue(entered.await(1, TimeUnit.SECONDS))
                assertTrue(PyBridge.isDemoManualBuyBusy())
                assertEquals(PyBridge.DEMO_BUY_BUSY, PyBridge.withDemoManualBuyLocks {
                    fail("busy buy must not execute")
                    "unexpected"
                })
                assertTrue(PyBridge.DEMO_BUY_BUSY.contains("already running"))
                if (name == "bridgeLock") assertFalse(lock("sessionLock").isLocked)
            } finally {
                release.countDown()
                holder.get(1, TimeUnit.SECONDS)
                executor.shutdownNow()
            }
            assertFalse(PyBridge.isDemoManualBuyBusy())
        }
    }

    @Test(timeout = 5000)
    fun secondBuyIsRefusedNotDelayedAndNextExplicitBuyCanRun() {
        val entered = CountDownLatch(1)
        val release = CountDownLatch(1)
        val calls = AtomicInteger()
        val executor = Executors.newSingleThreadExecutor()
        val first = executor.submit<String> {
            PyBridge.withDemoManualBuyLocks {
                calls.incrementAndGet()
                entered.countDown()
                check(release.await(3, TimeUnit.SECONDS))
                "first"
            }
        }
        try {
            assertTrue(entered.await(1, TimeUnit.SECONDS))
            assertEquals(PyBridge.DEMO_BUY_BUSY, PyBridge.withDemoManualBuyLocks {
                calls.incrementAndGet()
                "queued"
            })
        } finally {
            release.countDown()
            assertEquals("first", first.get(1, TimeUnit.SECONDS))
            executor.shutdownNow()
        }
        assertEquals(1, calls.get())
        assertEquals("next", PyBridge.withDemoManualBuyLocks { "next" })
        assertFalse(PyBridge.isDemoManualBuyBusy())
    }

    @Test
    fun exceptionReleasesBothLocks() {
        try {
            PyBridge.withDemoManualBuyLocks { throw IllegalStateException("synthetic") }
            fail("exception expected")
        } catch (_: IllegalStateException) {
            assertFalse(PyBridge.isDemoManualBuyBusy())
        }
        assertEquals("next", PyBridge.withDemoManualBuyLocks { "next" })
    }

    @Test
    fun automaticEntriesCannotInvokeManualBuy() {
        for (file in listOf("WakeService.kt", "SessionAlarmReceiver.kt", "CommandHandler.kt",
                            "NightwatchProbeMessagingService.kt")) {
            val automatic = source("java/com/nightwatch/etoro/probe/$file")
            assertFalse(automatic.contains("DemoManualBuy"))
            assertFalse(automatic.contains("demo_manual_buy"))
            if (file == "WakeService.kt") {
                for (entry in listOf("planAdoption", "applyAdoption", "adopt_apply")) {
                    assertFalse(automatic.contains(entry))
                }
                assertTrue(automatic.contains("PyBridge.runDemoSeed("))
            }
        }
        val entry = source("../../../../app/src/main/python/nightwatch/android_entry.py")
        assertFalse(entry.contains("demo_manual_buy"))
    }
}
