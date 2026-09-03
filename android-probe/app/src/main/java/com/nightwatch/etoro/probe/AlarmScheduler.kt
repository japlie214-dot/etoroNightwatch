package com.nightwatch.etoro.probe

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import java.util.Calendar

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
    private const val KEY_MODE = "mode" // "minutes" | "nightly" | "absolute"
    private const val REQUEST_CODE = 42

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
    fun scheduleInMinutes(context: Context, minutes: Int): Long {
        prefs(context).edit().putString(KEY_MODE, "minutes").apply()
        val triggerAt = System.currentTimeMillis() + minutes * 60_000L
        schedule(context, triggerAt)
        return triggerAt
    }

    /** Schedules (or reschedules) the next occurrence of a device-local HH:mm on the given
     * weekdays (Calendar.MONDAY..SUNDAY). */
    fun scheduleNightly(context: Context, hour: Int, minute: Int, days: Set<Int> = DEFAULT_DAYS): Long {
        val effectiveDays = if (days.isEmpty()) DEFAULT_DAYS else days
        prefs(context).edit()
            .putString(KEY_MODE, "nightly")
            .putInt(KEY_NIGHTLY_HOUR, hour)
            .putInt(KEY_NIGHTLY_MINUTE, minute)
            .putString(KEY_NIGHTLY_DAYS, effectiveDays.joinToString(","))
            .apply()
        val triggerAt = nextOccurrence(hour, minute, effectiveDays)
        schedule(context, triggerAt)
        return triggerAt
    }

    /** Remote `schedule <iso8601>` command (Problem 3): sets the very next alarm to an explicit
     * instant, independent of the nightly HH:mm/day pattern. The nightly pattern (if any) resumes
     * on the following reschedule. */
    fun scheduleAbsolute(context: Context, epochMillis: Long): Long {
        prefs(context).edit().putString(KEY_MODE, "absolute").apply()
        schedule(context, epochMillis)
        return epochMillis
    }

    /** Called after every alarm fire and from BootReceiver: repairs the schedule unconditionally. */
    fun rescheduleFromSavedState(context: Context): Long? {
        val p = prefs(context)
        return when (p.getString(KEY_MODE, null)) {
            "nightly" -> {
                val h = p.getInt(KEY_NIGHTLY_HOUR, 2)
                val m = p.getInt(KEY_NIGHTLY_MINUTE, 30)
                val days = parseDays(p.getString(KEY_NIGHTLY_DAYS, null))
                scheduleNightly(context, h, m, days)
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
        val p = prefs(context)
        return when (p.getString(KEY_MODE, null)) {
            "nightly" -> {
                val h = p.getInt(KEY_NIGHTLY_HOUR, 2)
                val m = p.getInt(KEY_NIGHTLY_MINUTE, 30)
                val days = parseDays(p.getString(KEY_NIGHTLY_DAYS, null))
                nextOccurrence(h, m, days)
            }
            else -> null
        }
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

    private fun schedule(context: Context, triggerAtMillis: Long) {
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
            return
        }
        val info = AlarmManager.AlarmClockInfo(triggerAtMillis, showIntent(context))
        am.setAlarmClock(info, pendingIntent(context))
        EventStore.get(context).log(
            "ALARM_SCHEDULED",
            "next fire at epoch $triggerAtMillis (${java.util.Date(triggerAtMillis)})"
        )
    }

    fun cancel(context: Context) {
        val am = context.getSystemService(AlarmManager::class.java)
        am.cancel(pendingIntent(context))
    }
}
