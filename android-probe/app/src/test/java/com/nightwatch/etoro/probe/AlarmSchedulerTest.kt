package com.nightwatch.etoro.probe

import android.content.SharedPreferences
import org.junit.Assert.*
import org.junit.Test
import java.io.IOException
import java.time.Instant
import java.util.Calendar
import java.util.TimeZone

class AlarmSchedulerTest {
    @Test
    fun haltLatchesCancelsAndNotifiesInOrder() {
        val effects = FakeAlarmEffects()
        AlarmScheduler.scheduleMarket(effects)
        effects.events.clear()
        CommandHandler.halt(effects.halt)
        assertTrue(effects.halt.latched)
        assertTrue(effects.halt.present)
        assertNull(effects.scheduled)
        assertFalse(effects.preferences.contains("market_next"))
        assertEquals(listOf("latch:true", "create", "COMMAND_HALT", "cancel", "postHalt"), effects.events)
    }

    @Test
    fun failedFileCreationStillLatchesCancelsAndNotifies() {
        val effects = FakeAlarmEffects()
        effects.halt.createFails = true
        CommandHandler.halt(effects.halt)
        assertTrue(effects.halt.latched)
        assertFalse(effects.halt.present)
        assertTrue(effects.events.contains("COMMAND_HALT_FAILED"))
        assertTrue(effects.events.contains("cancel"))
        assertEquals("postHalt", effects.events.last())
    }

    @Test
    fun haltCancellationFailureStillPostsOngoingNotification() {
        val effects = FakeAlarmEffects()
        effects.cancelFails = true
        CommandHandler.halt(effects.halt)
        assertTrue(effects.halt.latched)
        assertTrue(effects.events.contains("COMMAND_HALT_CANCEL_FAILED"))
        assertEquals("postHalt", effects.events.last())
    }

    @Test
    fun latchSuppressesEveryDirectAndSavedAlarmMode() {
        for (mode in listOf("minutes", "nightly", "absolute", "market")) {
            val effects = FakeAlarmEffects()
            effects.halt.latched = true
            val result = when (mode) {
                "minutes" -> AlarmScheduler.scheduleInMinutes(effects, 5)
                "nightly" -> AlarmScheduler.scheduleNightly(effects, 2, 30)
                "absolute" -> AlarmScheduler.scheduleAbsolute(effects, effects.now() + 1000)
                else -> AlarmScheduler.scheduleMarket(effects)
            }
            assertNull(mode, result)
            assertNull(mode, effects.scheduled)
            assertNull(mode, AlarmScheduler.rescheduleFromSavedState(effects))
            assertFalse(mode, effects.events.contains("setAlarm"))
            assertFalse(mode, effects.events.contains("marketWake"))
            assertFalse(mode, effects.preferences.contains("next"))
            assertFalse(mode, effects.preferences.contains("market_next"))
        }
    }

    @Test
    fun bootRecoveryReconcilesPresentFileBeforeRescheduling() {
        val effects = FakeAlarmEffects()
        effects.preferences.edit().putString("mode", "market").apply()
        effects.halt.present = true
        assertNull(AlarmScheduler.recover(effects))
        assertTrue(effects.halt.latched)
        assertEquals("reconcile", effects.events.first())
        assertTrue(effects.events.indexOf("latch:true") < effects.events.indexOf("ALARM_SUPPRESSED_HALT"))
        assertTrue(effects.events.contains("postHalt"))
        assertFalse(effects.events.contains("marketWake"))
        assertFalse(effects.events.contains("setAlarm"))
    }

    @Test
    fun missingFileNeverClearsLatchDuringRecovery() {
        val effects = FakeAlarmEffects()
        effects.halt.latched = true
        assertNull(AlarmScheduler.recover(effects))
        assertTrue(effects.halt.latched)
        assertFalse(effects.events.contains("latch:false"))
    }

    @Test
    fun unreadableHaltFileFailsClosed() {
        val effects = FakeAlarmEffects()
        effects.halt.existsFails = true
        assertNull(AlarmScheduler.recover(effects))
        assertTrue(effects.halt.latched)
        assertTrue(effects.events.contains("HALT_RECONCILE_FAILED"))
        assertTrue(effects.events.contains("postHalt"))
    }

    @Test
    fun resumeConfirmsAbsenceBeforeClearingLatchAndNotification() {
        val effects = FakeAlarmEffects()
        effects.halt.latched = true
        effects.halt.present = true
        assertTrue(CommandHandler.resume(effects.halt))
        assertFalse(effects.halt.latched)
        assertFalse(effects.halt.present)
        assertEquals(listOf("exists", "delete", "exists", "latch:false", "clearHalt", "COMMAND_RESUME"), effects.events)
        assertNotNull(AlarmScheduler.scheduleMarket(effects))
    }

    @Test
    fun resumeAlreadyAbsentFileClearsLatchWithoutDelete() {
        val effects = FakeAlarmEffects()
        effects.halt.latched = true
        assertTrue(CommandHandler.resume(effects.halt))
        assertFalse(effects.halt.latched)
        assertFalse(effects.events.contains("delete"))
        assertTrue(effects.events.contains("clearHalt"))
    }

    @Test
    fun failedDeleteKeepsLatchAndNotification() {
        val effects = FakeAlarmEffects()
        effects.halt.latched = true
        effects.halt.present = true
        effects.halt.deleteSucceeds = false
        assertFalse(CommandHandler.resume(effects.halt))
        assertTrue(effects.halt.latched)
        assertTrue(effects.halt.present)
        assertTrue(effects.events.contains("cancel"))
        assertTrue(effects.events.contains("postHalt"))
        assertFalse(effects.events.contains("clearHalt"))
    }

    @Test
    fun successfulDeleteThatLeavesFilePresentCannotResume() {
        val effects = FakeAlarmEffects()
        effects.halt.present = true
        effects.halt.keepFileAfterDelete = true
        assertFalse(CommandHandler.resume(effects.halt))
        assertTrue(effects.halt.latched)
        assertFalse(effects.events.contains("clearHalt"))
    }

    @Test
    fun permissionDeniedClearsOldTimestampAndReturnsFailure() {
        val effects = FakeAlarmEffects()
        assertNotNull(AlarmScheduler.scheduleMarket(effects))
        effects.permission = false
        assertNull(AlarmScheduler.scheduleMarket(effects))
        assertNoTimestamp(effects)
        assertNull(effects.scheduled)
        assertTrue(effects.events.contains("ALARM_SCHEDULE_DENIED"))
        assertTrue(effects.events.any { it.startsWith("failure:") })
    }

    @Test
    fun securityExceptionClearsOldTimestampAndReturnsFailure() {
        val effects = FakeAlarmEffects()
        assertNotNull(AlarmScheduler.scheduleMarket(effects))
        effects.setFails = true
        assertNull(AlarmScheduler.scheduleMarket(effects))
        assertNoTimestamp(effects)
        assertNull(effects.scheduled)
        assertTrue(effects.events.contains("ALARM_SCHEDULE_DENIED"))
    }

    @Test
    fun marketTimestampIsPersistedOnlyAfterAlarmSinkSucceedsAndClearedOnCancel() {
        val effects = FakeAlarmEffects()
        assertEquals(effects.marketAt, AlarmScheduler.scheduleMarket(effects))
        assertEquals(effects.marketAt, effects.preferences.getLong("market_next", 0))
        assertEquals(effects.marketAt, effects.preferences.getLong("next", 0))
        assertEquals(effects.marketAt, effects.scheduled)
        AlarmScheduler.cancel(effects)
        assertNull(effects.scheduled)
        assertNoTimestamp(effects)
    }

    @Test
    fun modeSwitchCancelsOldAlarmBeforeFailedMarketComputation() {
        val effects = FakeAlarmEffects()
        assertNotNull(AlarmScheduler.scheduleInMinutes(effects, 3))
        effects.events.clear()
        effects.marketFails = true
        assertNull(AlarmScheduler.scheduleMarket(effects))
        assertEquals("market", effects.preferences.getString("mode", null))
        assertNull(effects.scheduled)
        assertNoTimestamp(effects)
        assertTrue(effects.events.indexOf("cancel") < effects.events.indexOf("marketWake"))
        assertTrue(effects.events.contains("ALARM_MARKET_COMPUTE_FAILED"))
    }

    @Test
    fun nightlyUsesInjectedClockCalendarAndSavedDaysOnRecovery() {
        val effects = FakeAlarmEffects()
        // Saturday advances to Monday, then recovery advances to Tuesday after Monday's fire.
        val monday = Instant.parse("2026-11-02T02:30:00Z").toEpochMilli()
        assertEquals(monday, AlarmScheduler.scheduleNightly(effects, 2, 30, emptySet()))
        effects.time = monday + 1
        assertEquals(monday + 86_400_000, AlarmScheduler.recover(effects))
        assertEquals("nightly", effects.preferences.getString("mode", null))
        assertFalse(effects.preferences.contains("market_next"))
    }

    @Test
    fun oneShotModesDoNotRepeatOnRecovery() {
        val effects = FakeAlarmEffects()
        AlarmScheduler.scheduleAbsolute(effects, effects.now() + 1000)
        effects.events.clear()
        assertNull(AlarmScheduler.recover(effects))
        assertFalse(effects.events.contains("setAlarm"))
        AlarmScheduler.scheduleInMinutes(effects, 3)
        effects.events.clear()
        assertNull(AlarmScheduler.recover(effects))
        assertFalse(effects.events.contains("setAlarm"))
    }

    @Test
    fun expiredMarketComputationCannotRegisterLateAlarm() {
        val effects = FakeAlarmEffects()
        var allowed = true
        effects.duringMarket = { allowed = false }
        assertNull(AlarmScheduler.scheduleMarket(effects) { allowed })
        assertFalse(effects.events.contains("setAlarm"))
        assertNoTimestamp(effects)
    }

    @Test
    fun changedGenerationCannotReplaceNewerModeWithLateMarketResult() {
        val effects = FakeAlarmEffects()
        val replacement = effects.now() + 1000
        effects.duringMarket = { AlarmScheduler.scheduleAbsolute(effects, replacement) }
        assertNull(AlarmScheduler.scheduleMarket(effects))
        assertEquals(replacement, effects.scheduled)
        assertEquals("absolute", effects.preferences.getString("mode", null))
        assertFalse(effects.preferences.contains("market_next"))
    }

    private fun assertNoTimestamp(effects: FakeAlarmEffects) {
        assertFalse(effects.preferences.contains("next"))
        assertFalse(effects.preferences.contains("market_next"))
    }
}

private class FakeAlarmEffects : AlarmScheduler.Effects {
    override val preferences = MemoryPreferences()
    val events = mutableListOf<String>()
    val halt = FakeHaltEffects(this)
    var time = Instant.parse("2026-10-31T12:00:00Z").toEpochMilli()
    val marketAt = time + 3_600_000
    var permission = true
    var setFails = false
    var cancelFails = false
    var marketFails = false
    var scheduled: Long? = null
    var duringMarket: () -> Unit = {}
    override fun now() = time
    override fun calendar(): Calendar = Calendar.getInstance(TimeZone.getTimeZone("UTC")).apply {
        timeInMillis = this@FakeAlarmEffects.time
    }
    override fun isHalted() = halt.latched
    override fun reconcileHalt() {
        events.add("reconcile")
        HaltFlag.reconcile(halt)
    }
    override fun canSchedule() = permission
    override fun marketWake(): Long {
        events.add("marketWake")
        duringMarket()
        if (marketFails) throw IOException("fake calendar unavailable")
        return marketAt
    }
    override fun setAlarm(triggerAtMillis: Long) {
        assertFalse(preferences.contains("next"))
        assertFalse(preferences.contains("market_next"))
        if (setFails) throw SecurityException("fake permission revoked")
        events.add("setAlarm")
        scheduled = triggerAtMillis
    }
    override fun cancelAlarm() {
        events.add("cancel")
        if (cancelFails) throw SecurityException("fake cancellation failed")
        scheduled = null
    }
    override fun log(event: String, detail: String) { events.add(event) }
    override fun failure(title: String, detail: String) { events.add("failure:$title") }
}

private class FakeHaltEffects(private val alarm: FakeAlarmEffects) : HaltFlag.Effects {
    var latched = false
    var present = false
    var createFails = false
    var existsFails = false
    var deleteSucceeds = true
    var keepFileAfterDelete = false
    override fun fileExists(): Boolean {
        alarm.events.add("exists")
        if (existsFails) throw IOException("fake file unreadable")
        return present
    }
    override fun createFile() {
        alarm.events.add("create")
        if (createFails) throw IOException("fake disk full")
        present = true
    }
    override fun deleteFile(): Boolean {
        alarm.events.add("delete")
        if (deleteSucceeds && !keepFileAfterDelete) present = false
        return deleteSucceeds
    }
    override fun setHalted(halted: Boolean) {
        alarm.events.add("latch:$halted")
        latched = halted
    }
    override fun isHalted() = latched
    override fun cancelAlarm() = AlarmScheduler.cancel(alarm)
    override fun postHalt() { alarm.events.add("postHalt") }
    override fun clearHalt() { alarm.events.add("clearHalt") }
    override fun log(event: String, detail: String) = alarm.log(event, detail)
    override fun failure(title: String, detail: String) = alarm.failure(title, detail)
}

private class MemoryPreferences : SharedPreferences {
    private val values = mutableMapOf<String, Any?>()
    override fun getAll(): MutableMap<String, *> = values.toMutableMap()
    override fun getString(key: String?, defValue: String?) = values[key] as String? ?: defValue
    override fun getInt(key: String?, defValue: Int) = values[key] as Int? ?: defValue
    override fun getLong(key: String?, defValue: Long) = values[key] as Long? ?: defValue
    override fun getFloat(key: String?, defValue: Float) = values[key] as Float? ?: defValue
    override fun getBoolean(key: String?, defValue: Boolean) = values[key] as Boolean? ?: defValue
    @Suppress("UNCHECKED_CAST")
    override fun getStringSet(key: String?, defValues: MutableSet<String>?) =
        (values[key] as Set<String>?)?.toMutableSet() ?: defValues
    override fun contains(key: String?) = values.containsKey(key)
    override fun registerOnSharedPreferenceChangeListener(listener: SharedPreferences.OnSharedPreferenceChangeListener?) = Unit
    override fun unregisterOnSharedPreferenceChangeListener(listener: SharedPreferences.OnSharedPreferenceChangeListener?) = Unit
    override fun edit(): SharedPreferences.Editor = object : SharedPreferences.Editor {
        private val pending = mutableMapOf<String, Any?>()
        private var clear = false
        override fun putString(key: String, value: String?) = apply { pending[key] = value }
        override fun putStringSet(key: String, value: MutableSet<String>?) = apply { pending[key] = value?.toSet() }
        override fun putInt(key: String, value: Int) = apply { pending[key] = value }
        override fun putLong(key: String, value: Long) = apply { pending[key] = value }
        override fun putFloat(key: String, value: Float) = apply { pending[key] = value }
        override fun putBoolean(key: String, value: Boolean) = apply { pending[key] = value }
        override fun remove(key: String) = apply { pending[key] = null }
        override fun clear() = apply { clear = true }
        override fun commit(): Boolean {
            apply()
            return true
        }
        override fun apply() {
            if (clear) values.clear()
            pending.forEach { (key, value) -> if (value == null) values.remove(key) else values[key] = value }
        }
    }
}
