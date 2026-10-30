package com.nightwatch.etoro.probe

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.os.SystemClock
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.async
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeout
import java.util.Calendar
import java.util.concurrent.atomic.AtomicBoolean

/**
 * DESIGN §7.2: setAlarmClock() is chosen over every other AlarmManager variant because it is the
 * only one Android documents as never being deferred, even in Doze. This probe schedules exactly
 * one such alarm at a time (a "fire in N minutes" test, a nightly HH:mm on chosen weekdays, or an
 * explicit remote-scheduled instant), reschedules it on every fire (mirroring the real app's
 * "reschedule at the end of every run"), and re-registers it from BootReceiver.
 *
 * v2: nightly scheduling now carries a Mon-Sun day set (the real app trades weekdays only) and
 * exposes the resolved next-fire instant so MainActivity can show a live countdown plus the
 * absolute local timestamp (task Problem 2).
 */
object AlarmScheduler {
    private const val PREFS = "nightwatch_probe_prefs"
    private const val KEY_NIGHTLY_HOUR = "nightly_hour"
    private const val KEY_NIGHTLY_MINUTE = "nightly_minute"
    private const val KEY_NIGHTLY_DAYS = "nightly_days" // comma-separated Calendar.DAY_OF_WEEK ints
    private const val KEY_MODE = "mode" // "minutes" | "nightly" | "absolute" | "market"
    private const val KEY_MARKET_NEXT = "market_next"
    private const val KEY_NEXT = "next"
    private const val REQUEST_CODE = 42
    private var generation = 0L

    /** Calendar.MONDAY..SUNDAY, the default weekday-only set (the real app trades weekdays). */
    val DEFAULT_DAYS = setOf(
        Calendar.MONDAY, Calendar.TUESDAY, Calendar.WEDNESDAY, Calendar.THURSDAY, Calendar.FRIDAY
    )

    private fun prefs(context: Context): SharedPreferences =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    private fun pendingIntent(context: Context): PendingIntent {
        val intent = Intent(context, SessionAlarmReceiver::class.java)
        return PendingIntent.getBroadcast(
            context,
            REQUEST_CODE,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
    }

    private fun showIntent(context: Context): PendingIntent {
        val intent = Intent(context, MainActivity::class.java)
        return PendingIntent.getActivity(
            context,
            REQUEST_CODE,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
    }

    internal interface Effects {
        val preferences: SharedPreferences
        fun now(): Long
        fun calendar(): Calendar
        fun isHalted(): Boolean
        fun reconcileHalt()
        fun canSchedule(): Boolean
        fun marketWake(): Long
        fun setAlarm(triggerAtMillis: Long)
        fun cancelAlarm()
        fun log(event: String, detail: String)
        fun failure(title: String, detail: String)
    }

    private class AndroidEffects(private val context: Context) : Effects {
        override val preferences: SharedPreferences get() = prefs(context)
        override fun now() = System.currentTimeMillis()
        override fun calendar(): Calendar = Calendar.getInstance()
        override fun isHalted() = HaltFlag.isHalted(context)
        override fun reconcileHalt() = HaltFlag.reconcile(context)
        override fun canSchedule() = PowerStatus.canScheduleExactAlarms(context)
        override fun marketWake() = PyBridge.nextSessionWakeEpochMs(context)
        override fun setAlarm(triggerAtMillis: Long) {
            val am = context.getSystemService(AlarmManager::class.java)
            am.setAlarmClock(AlarmManager.AlarmClockInfo(triggerAtMillis, showIntent(context)), pendingIntent(context))
        }
        override fun cancelAlarm() {
            context.getSystemService(AlarmManager::class.java).cancel(pendingIntent(context))
        }
        override fun log(event: String, detail: String) = EventStore.get(context).log(event, detail)
        override fun failure(title: String, detail: String) = NotificationHelper.postFailure(context, title, detail)
    }

    fun scheduleInMinutes(context: Context, minutes: Int): Long? =
        scheduleInMinutes(AndroidEffects(context), minutes)

    fun scheduleNightly(
        context: Context, hour: Int, minute: Int, days: Set<Int> = DEFAULT_DAYS,
        allowed: () -> Boolean = { true }
    ): Long? = scheduleNightly(AndroidEffects(context), hour, minute, days, allowed)

    fun scheduleAbsolute(context: Context, epochMillis: Long): Long? =
        scheduleAbsolute(AndroidEffects(context), epochMillis)

    fun scheduleMarket(context: Context, allowed: () -> Boolean = { true }): Long? =
        scheduleMarket(AndroidEffects(context), allowed)

    fun rescheduleFromSavedState(context: Context, allowed: () -> Boolean = { true }): Long? =
        rescheduleFromSavedState(AndroidEffects(context), allowed)

    fun cancel(context: Context) = cancel(AndroidEffects(context))

    /** Schedules a one-off test alarm N minutes from now. */
    @Synchronized
    internal fun scheduleInMinutes(effects: Effects, minutes: Int): Long? {
        cancel(effects)
        effects.preferences.edit().putString(KEY_MODE, "minutes").apply()
        val triggerAt = effects.now() + minutes * 60_000L
        return schedule(effects, triggerAt)
    }

    /** Schedules (or reschedules) the next occurrence of a device-local HH:mm on the given
     * weekdays (Calendar.MONDAY..SUNDAY). */
    @Synchronized
    internal fun scheduleNightly(
        effects: Effects, hour: Int, minute: Int, days: Set<Int> = DEFAULT_DAYS,
        allowed: () -> Boolean = { true }
    ): Long? {
        if (!allowed()) return null
        cancel(effects)
        val effectiveDays = if (days.isEmpty()) DEFAULT_DAYS else days
        effects.preferences.edit()
            .putString(KEY_MODE, "nightly")
            .putInt(KEY_NIGHTLY_HOUR, hour)
            .putInt(KEY_NIGHTLY_MINUTE, minute)
            .putString(KEY_NIGHTLY_DAYS, effectiveDays.joinToString(","))
            .apply()
        val triggerAt = nextOccurrence(effects, hour, minute, effectiveDays)
        return schedule(effects, triggerAt, allowed)
    }

    /** Remote `schedule <iso8601>` command (Problem 3): sets the very next alarm to an explicit
     * instant, independent of the nightly HH:mm/day pattern. The nightly pattern (if any) resumes
     * on the following reschedule. */
    @Synchronized
    internal fun scheduleAbsolute(effects: Effects, epochMillis: Long): Long? {
        cancel(effects)
        effects.preferences.edit().putString(KEY_MODE, "absolute").apply()
        return schedule(effects, epochMillis)
    }

    /** Background callers only. The 55-minute lead is a margin, not a delivery/network guarantee. */
    internal fun scheduleMarket(effects: Effects, allowed: () -> Boolean = { true }): Long? {
        try {
            val request = synchronized(this) {
                if (!allowed()) return null
                cancel(effects)
                effects.preferences.edit().putString(KEY_MODE, "market").apply()
                if (suppressedByHalt(effects)) return null
                generation
            }
            val triggerAt = try {
                effects.marketWake()
            } catch (e: Exception) {
                effects.log("ALARM_MARKET_COMPUTE_FAILED", "error=${e.javaClass.simpleName}")
                effects.failure("Market alarm not scheduled", e.javaClass.simpleName)
                return null
            }
            return synchronized(this) {
                // Chaquopy is synchronous: an expired receiver must never register its late result.
                if (!allowed() || request != generation) return null
                schedule(effects, triggerAt, allowed)
            }
        } catch (e: Exception) {
            effects.log("ALARM_MARKET_SCHEDULE_FAILED", "error=${e.javaClass.simpleName}")
            effects.failure("Market alarm not scheduled", e.javaClass.simpleName)
            return null
        }
    }

    /** Called on an IO worker after every alarm fire and on receiver recovery. */
    internal fun rescheduleFromSavedState(effects: Effects, allowed: () -> Boolean = { true }): Long? {
        if (!allowed()) return null
        if (suppressedByHalt(effects)) return null
        val p = effects.preferences
        return when (p.getString(KEY_MODE, null)) {
            "market" -> scheduleMarket(effects, allowed)
            "nightly" -> synchronized(this) {
                if (!allowed()) return null
                val h = p.getInt(KEY_NIGHTLY_HOUR, 2)
                val m = p.getInt(KEY_NIGHTLY_MINUTE, 30)
                val days = parseDays(p.getString(KEY_NIGHTLY_DAYS, null))
                scheduleNightly(effects, h, m, days, allowed)
            }
            "minutes", "absolute" -> {
                // No sensible "repeat" semantics for a one-off alarm after a reboot; the real
                // app's daily safety-net alarm (DESIGN §7.2) is the nightly path above.
                null
            }
            else -> null
        }
    }

    /** Live "fires in HH:mm:ss" / absolute-timestamp support for MainActivity (Problem 2). */
    fun currentNextFireEpochMs(context: Context): Long? {
        if (HaltFlag.isHalted(context) || !PowerStatus.canScheduleExactAlarms(context)) return null
        val p = prefs(context)
        val key = if (p.getString(KEY_MODE, null) == "market") KEY_MARKET_NEXT else KEY_NEXT
        return p.getLong(key, 0L).takeIf { it > 0L }
    }

    fun savedDays(context: Context): Set<Int> =
        parseDays(prefs(context).getString(KEY_NIGHTLY_DAYS, null))

    fun savedHourMinute(context: Context): Pair<Int, Int> {
        val p = prefs(context)
        return p.getInt(KEY_NIGHTLY_HOUR, 2) to p.getInt(KEY_NIGHTLY_MINUTE, 30)
    }

    private fun parseDays(raw: String?): Set<Int> {
        if (raw.isNullOrBlank()) return DEFAULT_DAYS
        return raw.split(",").mapNotNull { it.trim().toIntOrNull() }.toSet().ifEmpty { DEFAULT_DAYS }
    }

    private fun nextOccurrence(effects: Effects, hour: Int, minute: Int, days: Set<Int>): Long {
        val cal = effects.calendar()
        cal.set(Calendar.HOUR_OF_DAY, hour)
        cal.set(Calendar.MINUTE, minute)
        cal.set(Calendar.SECOND, 0)
        cal.set(Calendar.MILLISECOND, 0)
        if (cal.timeInMillis <= effects.now()) {
            cal.add(Calendar.DAY_OF_YEAR, 1)
        }
        // Walk forward at most 7 days to find a matching weekday.
        repeat(8) {
            if (cal.get(Calendar.DAY_OF_WEEK) in days) return cal.timeInMillis
            cal.add(Calendar.DAY_OF_YEAR, 1)
        }
        return cal.timeInMillis // unreachable in practice (days is never empty)
    }

    @Synchronized
    private fun suppressedByHalt(effects: Effects): Boolean {
        if (!effects.isHalted()) return false
        cancel(effects)
        effects.log("ALARM_SUPPRESSED_HALT", "sessions stopped until resume")
        return true
    }

    internal fun recover(effects: Effects, allowed: () -> Boolean = { true }): Long? {
        effects.reconcileHalt()
        return rescheduleFromSavedState(effects, allowed)
    }

    /** Finish receivers within eight seconds even if the synchronous Python worker is stuck. */
    fun rescheduleFromReceiver(context: Context, pending: BroadcastReceiver.PendingResult, event: String) {
        val app = context.applicationContext
        val deadline = SystemClock.elapsedRealtime() + 8_000L
        val finished = AtomicBoolean(false)
        CoroutineScope(Dispatchers.Default).launch {
            val worker = CoroutineScope(Dispatchers.IO).async {
                recover(AndroidEffects(app)) {
                    !finished.get() && SystemClock.elapsedRealtime() < deadline
                }
            }
            try {
                val next = withTimeout((deadline - SystemClock.elapsedRealtime()).coerceAtLeast(1L)) {
                    worker.await()
                }
                EventStore.get(app).log("${event}_RESCHEDULE_RESULT", "next=$next; null means not scheduled")
            } catch (e: TimeoutCancellationException) {
                EventStore.get(app).log("${event}_RESCHEDULE_TIMEOUT", "repair on next alarm wake or schedule command")
                NotificationHelper.postFailure(app, "Alarm recovery timed out", "Alarm not scheduled; use schedule command")
            } catch (e: Exception) {
                EventStore.get(app).log("${event}_RESCHEDULE_FAILED", "error=${e.javaClass.simpleName}")
                NotificationHelper.postFailure(app, "Alarm recovery failed", e.javaClass.simpleName)
            } finally {
                finished.set(true)
                worker.cancel()
                pending.finish()
            }
        }
    }

    private fun schedule(effects: Effects, triggerAtMillis: Long, allowed: () -> Boolean = { true }): Long? {
        if (suppressedByHalt(effects)) return null
        effects.preferences.edit().remove(KEY_MARKET_NEXT).remove(KEY_NEXT).apply()
        if (!effects.canSchedule()) {
            // Never silently downgrade to an inexact alarm (DESIGN §7.2) — log loudly instead.
            effects.log(
                "ALARM_SCHEDULE_DENIED",
                "canScheduleExactAlarms()=false; requested trigger at $triggerAtMillis was NOT set"
            )
            effects.failure(
                "Cannot schedule alarm",
                "SCHEDULE_EXACT_ALARM permission is not granted"
            )
            return null
        }
        try {
            if (!allowed()) return null
            effects.setAlarm(triggerAtMillis)
        } catch (e: SecurityException) {
            effects.log("ALARM_SCHEDULE_DENIED", "error=${e.javaClass.simpleName}")
            effects.failure("Alarm not scheduled", "Exact-alarm permission denied")
            return null
        }
        val editor = effects.preferences.edit().putLong(KEY_NEXT, triggerAtMillis)
        if (effects.preferences.getString(KEY_MODE, null) == "market") {
            editor.putLong(KEY_MARKET_NEXT, triggerAtMillis)
        }
        editor.apply()
        effects.log(
            "ALARM_SCHEDULED",
            "next fire at epoch $triggerAtMillis (${java.util.Date(triggerAtMillis)})"
        )
        return triggerAtMillis
    }

    @Synchronized
    internal fun cancel(effects: Effects) {
        generation++
        effects.preferences.edit().remove(KEY_MARKET_NEXT).remove(KEY_NEXT).apply()
        effects.cancelAlarm()
    }
}
