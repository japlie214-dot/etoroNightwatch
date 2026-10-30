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

    /** Schedules a one-off test alarm N minutes from now. */
    @Synchronized
    fun scheduleInMinutes(context: Context, minutes: Int): Long? {
        cancel(context)
        prefs(context).edit().putString(KEY_MODE, "minutes").apply()
        val triggerAt = System.currentTimeMillis() + minutes * 60_000L
        return schedule(context, triggerAt)
    }

    /** Schedules (or reschedules) the next occurrence of a device-local HH:mm on the given
     * weekdays (Calendar.MONDAY..SUNDAY). */
    @Synchronized
    fun scheduleNightly(
        context: Context, hour: Int, minute: Int, days: Set<Int> = DEFAULT_DAYS,
        allowed: () -> Boolean = { true }
    ): Long? {
        if (!allowed()) return null
        cancel(context)
        val effectiveDays = if (days.isEmpty()) DEFAULT_DAYS else days
        prefs(context).edit()
            .putString(KEY_MODE, "nightly")
            .putInt(KEY_NIGHTLY_HOUR, hour)
            .putInt(KEY_NIGHTLY_MINUTE, minute)
            .putString(KEY_NIGHTLY_DAYS, effectiveDays.joinToString(","))
            .apply()
        val triggerAt = nextOccurrence(hour, minute, effectiveDays)
        return schedule(context, triggerAt, allowed)
    }

    /** Remote `schedule <iso8601>` command (Problem 3): sets the very next alarm to an explicit
     * instant, independent of the nightly HH:mm/day pattern. The nightly pattern (if any) resumes
     * on the following reschedule. */
    @Synchronized
    fun scheduleAbsolute(context: Context, epochMillis: Long): Long? {
        cancel(context)
        prefs(context).edit().putString(KEY_MODE, "absolute").apply()
        return schedule(context, epochMillis)
    }

    /** Background callers only. The 55-minute lead is a margin, not a delivery/network guarantee. */
    fun scheduleMarket(context: Context, allowed: () -> Boolean = { true }): Long? {
        try {
            val request = synchronized(this) {
                if (!allowed()) return null
                cancel(context)
                prefs(context).edit().putString(KEY_MODE, "market").apply()
                if (suppressedByHalt(context)) return null
                generation
            }
            val triggerAt = try {
                PyBridge.nextSessionWakeEpochMs(context)
            } catch (e: Exception) {
                EventStore.get(context).log("ALARM_MARKET_COMPUTE_FAILED", "error=${e.javaClass.simpleName}")
                NotificationHelper.postFailure(context, "Market alarm not scheduled", e.javaClass.simpleName)
                return null
            }
            return synchronized(this) {
                // Chaquopy is synchronous: an expired receiver must never register its late result.
                if (!allowed() || request != generation) return null
                schedule(context, triggerAt, allowed)
            }
        } catch (e: Exception) {
            EventStore.get(context).log("ALARM_MARKET_SCHEDULE_FAILED", "error=${e.javaClass.simpleName}")
            NotificationHelper.postFailure(context, "Market alarm not scheduled", e.javaClass.simpleName)
            return null
        }
    }

    /** Called on an IO worker after every alarm fire and on receiver recovery. */
    fun rescheduleFromSavedState(context: Context, allowed: () -> Boolean = { true }): Long? {
        if (!allowed()) return null
        if (suppressedByHalt(context)) return null
        val p = prefs(context)
        return when (p.getString(KEY_MODE, null)) {
            "market" -> scheduleMarket(context, allowed)
            "nightly" -> synchronized(this) {
                if (!allowed()) return null
                val h = p.getInt(KEY_NIGHTLY_HOUR, 2)
                val m = p.getInt(KEY_NIGHTLY_MINUTE, 30)
                val days = parseDays(p.getString(KEY_NIGHTLY_DAYS, null))
                scheduleNightly(context, h, m, days, allowed)
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

    private fun nextOccurrence(hour: Int, minute: Int, days: Set<Int>): Long {
        val cal = Calendar.getInstance()
        cal.set(Calendar.HOUR_OF_DAY, hour)
        cal.set(Calendar.MINUTE, minute)
        cal.set(Calendar.SECOND, 0)
        cal.set(Calendar.MILLISECOND, 0)
        if (cal.timeInMillis <= System.currentTimeMillis()) {
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
    private fun suppressedByHalt(context: Context): Boolean {
        if (!HaltFlag.isHalted(context)) return false
        cancel(context)
        EventStore.get(context).log("ALARM_SUPPRESSED_HALT", "sessions stopped until resume")
        return true
    }

    /** Finish receivers within eight seconds even if the synchronous Python worker is stuck. */
    fun rescheduleFromReceiver(context: Context, pending: BroadcastReceiver.PendingResult, event: String) {
        val app = context.applicationContext
        val deadline = SystemClock.elapsedRealtime() + 8_000L
        val finished = AtomicBoolean(false)
        CoroutineScope(Dispatchers.Default).launch {
            val worker = CoroutineScope(Dispatchers.IO).async {
                HaltFlag.reconcile(app)
                rescheduleFromSavedState(app) {
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

    private fun schedule(context: Context, triggerAtMillis: Long, allowed: () -> Boolean = { true }): Long? {
        if (suppressedByHalt(context)) return null
        prefs(context).edit().remove(KEY_MARKET_NEXT).remove(KEY_NEXT).apply()
        val am = context.getSystemService(AlarmManager::class.java)
        if (!PowerStatus.canScheduleExactAlarms(context)) {
            // Never silently downgrade to an inexact alarm (DESIGN §7.2) — log loudly instead.
            EventStore.get(context).log(
                "ALARM_SCHEDULE_DENIED",
                "canScheduleExactAlarms()=false; requested trigger at $triggerAtMillis was NOT set"
            )
            NotificationHelper.postFailure(
                context,
                "Cannot schedule alarm",
                "SCHEDULE_EXACT_ALARM permission is not granted"
            )
            return null
        }
        try {
            val info = AlarmManager.AlarmClockInfo(triggerAtMillis, showIntent(context))
            val alarm = pendingIntent(context)
            if (!allowed()) return null
            am.setAlarmClock(info, alarm)
        } catch (e: SecurityException) {
            EventStore.get(context).log("ALARM_SCHEDULE_DENIED", "error=${e.javaClass.simpleName}")
            NotificationHelper.postFailure(context, "Alarm not scheduled", "Exact-alarm permission denied")
            return null
        }
        val editor = prefs(context).edit().putLong(KEY_NEXT, triggerAtMillis)
        if (prefs(context).getString(KEY_MODE, null) == "market") {
            editor.putLong(KEY_MARKET_NEXT, triggerAtMillis)
        }
        editor.apply()
        EventStore.get(context).log(
            "ALARM_SCHEDULED",
            "next fire at epoch $triggerAtMillis (${java.util.Date(triggerAtMillis)})"
        )
        return triggerAtMillis
    }

    @Synchronized
    fun cancel(context: Context) {
        generation++
        prefs(context).edit().remove(KEY_MARKET_NEXT).remove(KEY_NEXT).apply()
        val am = context.getSystemService(AlarmManager::class.java)
        am.cancel(pendingIntent(context))
    }
}
