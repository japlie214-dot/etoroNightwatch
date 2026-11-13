package com.nightwatch.etoro.probe

import java.io.File
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/** T080 source wiring and pure flag gates; device alarm delivery remains PM/Boss proof. */
class DemoSeedWiringTest {
    @get:Rule val temporary = TemporaryFolder()

    private fun main(): File = listOf(File("src/main"), File("app/src/main")).first { it.isDirectory }
    private fun source(file: String): String = File(main(), "java/com/nightwatch/etoro/probe/$file").readText()

    @Test
    fun onlyBridgeAndWakeServiceReferenceSeed() {
        val references = File(main(), "java").walkTopDown().filter {
            it.isFile && it.readText().contains("runDemoSeed")
        }.map { it.name }.toSet()
        assertEquals(setOf("PyBridge.kt", "WakeService.kt"), references)
        for (file in listOf("SessionAlarmReceiver.kt", "CommandHandler.kt",
                            "NightwatchProbeMessagingService.kt", "BootReceiver.kt", "MainActivity.kt")) {
            assertFalse(source(file).contains("runDemoSeed"))
        }
        val bridge = source("PyBridge.kt").substringAfter("fun runDemoSeed(").substringBefore("fun planAdoption(")
        assertTrue(bridge.contains("sessionLock.withLock"))
        assertTrue(bridge.contains("bridgeLock.withLock"))
        assertTrue(bridge.contains("getModule(\"probe.demo_seed\")"))
        assertTrue(bridge.contains(".callAttr(\"run\", home)"))
        assertFalse(bridge.contains("runDemoManualBuy("))
    }

    @Test
    fun alarmCallsFlaggedSeedBeforeUnconditionalSessionWithOwnFailureBoundary() {
        val wake = source("WakeService.kt")
        val work = wake.substringAfter("// 5b.").substringBefore("store.log(\"WAKE_END\"")
        val alarm = work.indexOf("trigger == \"alarm\"")
        val seed = work.indexOf("runDemoSeedIfFlagged()")
        val session = work.indexOf("runSessionDry()")
        assertTrue(alarm >= 0 && seed > alarm && session > seed)
        assertEquals(1, Regex("runDemoSeedIfFlagged\\(\\)").findAll(work).count())
        // Whole file, not only step 5b: exactly one call plus the definition, and one bridge call.
        assertEquals(2, Regex("runDemoSeedIfFlagged\\s*\\(").findAll(wake).count())
        assertEquals(1, Regex("runDemoSeed\\s*\\(").findAll(wake).count())
        assertEquals(1, Regex("runDemoSeed\\s*\\(").findAll(source("PyBridge.kt")).count())
        assertTrue(work.indexOf("HaltFlag.isHalted(this)") in (alarm + 1) until seed)
        assertTrue(work.indexOf("sessionRunning.compareAndSet(false, true)") in (alarm + 1) until seed)
        val between = work.substring(seed, session)
        assertTrue(between.contains("catch (e: Exception)"))
        assertFalse(between.contains("return"))
        val helper = wake.substringAfter("private fun runDemoSeedIfFlagged()").substringBefore("private fun runSessionDry()")
        assertTrue(helper.indexOf("shouldRunDemoSeed(") < helper.indexOf("store.log(\"DEMO_SEED_START\""))
        assertTrue(helper.contains("PyBridge.runDemoSeed(this, home.absolutePath)"))
        assertTrue(helper.contains("lock.acquire(15 * 60 * 1000L)"))
        assertTrue(helper.contains("if (lock.isHeld) lock.release()"))
        assertTrue(helper.contains("store.log(\"DEMO_SEED_RESULT\", \"status=\$status\")"))
        assertFalse(helper.contains("store.log(\"DEMO_SEED_RESULT\", raw"))
        assertFalse(helper.contains("e.message"))
    }

    @Test
    fun seedFlagTruthTableRequiresArmedNotHaltedAndPowerAllowed() {
        val home = temporary.newFolder("nightwatch_demo")
        for (halted in listOf(false, true)) for (power in listOf(false, true)) {
            for (armed in listOf(false, true)) for (flagged in listOf(false, true)) {
                for (adopting in listOf(false, true)) {
                    val arm = File(home, "ARM_DEMO")
                    val seed = File(home, "SEED_DEMO")
                    val adopt = File(home, "SEED_DEMO.adopt")
                    if (armed) arm.writeText("") else arm.delete()
                    if (flagged) seed.writeText("5 11000") else seed.delete()
                    if (adopting) adopt.writeText("0") else adopt.delete()
                    assertEquals(!halted && power && armed && (flagged || adopting),
                        WakeService.shouldRunDemoSeed(home, halted, power))
                }
            }
        }
    }

    @Test
    fun noFlagPreservesSessionPathWithoutSeedEvents() {
        val home = temporary.newFolder("no_flag")
        File(home, "ARM_DEMO").writeText("")
        assertFalse(WakeService.shouldRunDemoSeed(home, halted = false, powerAllowed = true))
        val helper = source("WakeService.kt").substringAfter("private fun runDemoSeedIfFlagged()")
            .substringBefore("private fun runSessionDry()")
        assertTrue(helper.indexOf(") return") < helper.indexOf("store.log(\"DEMO_SEED_START\""))
        val work = source("WakeService.kt").substringAfter("// 5b.").substringBefore("store.log(\"WAKE_END\"")
        assertTrue(work.indexOf("runSessionDry()") > work.indexOf("runDemoSeedIfFlagged()"))
    }
}
