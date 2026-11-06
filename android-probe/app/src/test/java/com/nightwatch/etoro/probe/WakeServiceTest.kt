package com.nightwatch.etoro.probe

import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class WakeServiceTest {
    @get:Rule val temporary = TemporaryFolder()

    @Test
    fun flagSelectsOnlyDemoBridgeAndDedicatedHomeOtherwiseDry() {
        for (armed in listOf(false, true)) {
            val root = temporary.newFolder()
            if (armed) File(root, "nightwatch_demo/ARM_DEMO").apply {
                parentFile!!.mkdirs()
                createNewFile()
            }
            val events = mutableListOf<String>()
            val result = WakeService.callSelectedSession(root,
                halted = { events.add("halt"); false },
                powerAllowed = { events.add("power"); true },
                selected = { events.add("selected:$it") },
                dry = { events.add("dry:$it"); "dry result" },
                armedDemo = { events.add("armed:$it"); "armed result" })
            val home = File(root, if (armed) "nightwatch_demo" else "nightwatch_config")
            assertEquals(if (armed) "armed result" else "dry result", result)
            assertEquals(listOf("halt", "power", "selected:$armed",
                "${if (armed) "armed" else "dry"}:${home.absolutePath}"), events)
        }
    }

    @Test
    fun haltAndLowPowerSkipBeforeSelectionOrEitherBridge() {
        for (armed in listOf(false, true)) {
            for (halted in listOf(false, true)) {
                val root = temporary.newFolder()
                if (armed) File(root, "nightwatch_demo/ARM_DEMO").apply {
                    parentFile!!.mkdirs()
                    createNewFile()
                }
                val events = mutableListOf<String>()
                assertNull(WakeService.callSelectedSession(root,
                    halted = { events.add("halt"); halted },
                    powerAllowed = {
                        events.add("power")
                        PowerStatus.shouldRunSession(false, 29)
                    },
                    selected = { fail("selected while blocked") },
                    dry = { fail("dry called while blocked"); "" },
                    armedDemo = { fail("armed called while blocked"); "" }))
                assertEquals(if (halted) listOf("halt") else listOf("halt", "power"), events)
            }
        }
    }

    @Test
    fun envelopeArmedFieldIsBooleanAndDryCannotBeArmed() {
        assertTrue(WakeService.acceptsArmed(true, true))
        assertTrue(WakeService.acceptsArmed(true, false)) // Refusals have not started.
        assertTrue(WakeService.acceptsArmed(false, false))
        assertFalse(WakeService.acceptsArmed(false, true))
        for (value in listOf("true", "false", 0, 1)) {
            assertFalse(WakeService.acceptsArmed(true, value))
            assertFalse(WakeService.acceptsArmed(false, value))
        }
    }

    @Test
    fun haltCreatesAndResumeDeletesBothHomes() {
        val root = temporary.newFolder()
        val effects = FileHaltEffects(root)
        CommandHandler.halt(effects)
        assertTrue(effects.latched)
        assertTrue(effects.cancelled && effects.posted)
        for (home in listOf("nightwatch_config", "nightwatch_demo")) {
            assertTrue(File(root, "$home/HALT").isFile)
        }
        assertTrue(CommandHandler.resume(effects))
        assertFalse(effects.latched)
        assertFalse(HaltFlag.fileExists(root))
        assertTrue(effects.cleared)
    }

    @Test
    fun eitherHomeHaltReconcilesAfterProcessDeathAndSuppressesNextSession() {
        for (home in listOf("nightwatch_config", "nightwatch_demo")) {
            val root = temporary.newFolder()
            // Same file written by the Python engine; no remembered Kotlin latch.
            File(root, "$home/HALT").apply { parentFile!!.mkdirs(); writeText("halt") }
            val effects = FileHaltEffects(root)
            assertTrue(HaltFlag.fileExists(root))
            HaltFlag.reconcile(effects)
            assertTrue(effects.latched && effects.cancelled && effects.posted)
            assertNull(WakeService.callSelectedSession(root,
                halted = { HaltFlag.fileExists(root) || effects.latched },
                powerAllowed = { fail("power checked after HALT"); true },
                selected = { fail("selected after HALT") },
                dry = { fail("dry after HALT"); "" },
                armedDemo = { fail("armed after HALT"); "" }))
            assertTrue(CommandHandler.resume(effects))
            assertFalse(HaltFlag.fileExists(root))
            assertFalse(effects.latched)
        }
    }
}

private class FileHaltEffects(private val root: File) : HaltFlag.Effects {
    var latched = false
    var cancelled = false
    var posted = false
    var cleared = false
    override fun fileExists() = HaltFlag.fileExists(root)
    override fun createFile() = HaltFlag.createFiles(root)
    override fun deleteFile() = HaltFlag.deleteFiles(root)
    override fun setHalted(halted: Boolean) { latched = halted }
    override fun isHalted() = latched
    override fun cancelAlarm() { cancelled = true }
    override fun postHalt() { posted = true }
    override fun clearHalt() { cleared = true }
    override fun log(event: String, detail: String) = Unit
    override fun failure(title: String, detail: String) { fail("$title: $detail") }
}
