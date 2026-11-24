package com.nightwatch.etoro.probe

import org.junit.Assert.*
import org.junit.Test
import java.io.File

/** T075: positions/pnl/today/next are answered on the phone; source wiring checks. */
class ReadCommandParityTest {
    private fun main() = listOf(File("src/main"), File("app/src/main")).first { it.isDirectory }
    private fun src(name: String) = File(main(), "java/com/nightwatch/etoro/probe/$name").readText()

    @Test
    fun theFourReadCommandsAreNoLongerUnknown() {
        val handler = src("CommandHandler.kt")
        assertTrue(handler.contains("\"positions\", \"pnl\", \"today\", \"next\" ->"))
        assertTrue(handler.contains("PyBridge.readCommand("))
    }

    @Test
    fun theBridgeNeverWaitsBehindOrAheadOfASessionAndCannotTrade() {
        val bridge = src("PyBridge.kt").substringAfter("fun readCommand(").substringBefore("/** Manual adoption")
        assertTrue(bridge.contains("sessionLock.tryLock()") && bridge.contains("bridgeLock.tryLock()"))
        assertTrue(bridge.contains("run_read_command"))
        for (forbidden in listOf("operator_write_authorized", "run_session", "apply_demo_control")) {
            assertFalse(bridge.contains(forbidden))
        }
    }

    @Test
    fun theWorkerAllowsExactlyWhatThePhoneNowAnswers() {
        val worker = File("../cloud/worker/src/commands.js").takeIf { it.exists() }
            ?: File("../../cloud/worker/src/commands.js")
        val allowed = worker.readText()
        for (c in listOf("positions", "pnl", "today", "next")) assertTrue(allowed.contains("\"$c\""))
    }
}
