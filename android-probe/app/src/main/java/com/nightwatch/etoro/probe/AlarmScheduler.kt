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
 * one such alarm at a time (either a "fire in N minutes" test or a nightly HH:mm), reschedules it
 * on every fire (mirroring the real app's "reschedule at the end of every run"), and re-registers
 * it from BootReceiver.
 */
object AlarmScheduler {
    private const val PREFS = "nightwatch_probe_prefs"
    private const val KEY_NIGHTLY_HOUR = "nightly_hour"
    private const val KEY_NIGHTLY_MINUTE = "nightly_minute"
    private const val KEY_MODE = "mode" // "minutes" or "nightly"
    private const val REQUEST_CODE = 42

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

    /** Schedules (or reschedules) the next occurrence of a device-local HH:mm. */
    fun scheduleNightly(context: Context, hour: Int, minute: Int): Long {
        prefs(context).edit()
            .putString(KEY_MODE, "nightly")
            .putInt(KEY_NIGHTLY_HOUR, hour)
            .putInt(KEY_NIGHTLY_MINUTE, minute)
            .apply()
        val triggerAt = nextOccurrence(hour, minute)
        schedule(context, triggerAt)
        return triggerAt
    }

    /** Called after every alarm fire and from BootReceiver: repairs the schedule unconditionally. */
    fun rescheduleFromSavedState(context: Context): Long? {
        val p = prefs(context)
        return when (p.getString(KEY_MODE, null)) {
            "nightly" -> {
                val h = p.getInt(KEY_NIGHTLY_HOUR, 2)
                val m = p.getInt(KEY_NIGHTLY_MINUTE, 30)
                scheduleNightly(context, h, m)
            }
            "minutes" -> {
                // No sensible "repeat" semantics for a one-off test alarm after a reboot;
                // the real app's daily safety-net alarm (DESIGN §7.2) is the nightly path above.
                null
            }
            else -> null
        }
    }

    private fun nextOccurrence(hour: Int, minute: Int): Long {
        val cal = Calendar.getInstance()
        cal.set(Calendar.HOUR_OF_DAY, hour)
        cal.set(Calendar.MINUTE, minute)
        cal.set(Calendar.SECOND, 0)
        cal.set(Calendar.MILLISECOND, 0)
        if (cal.timeInMillis <= System.currentTimeMillis()) {
            cal.add(Calendar.DAY_OF_YEAR, 1)
        }
        return cal.timeInMillis
    }

    private fun schedule(context: Context, triggerAtMillis: Long) {
        val am = context.getSystemService(AlarmManager::class.java)
        if (!PowerStatus.canScheduleExactAlarms(context)) {
            // Never silently downgrade to an inexact alarm (DESIGN §7.2) — log loudly instead.
            EventStore.get(context).log(
                "ALARM_SCHEDULE_DENIED",
                "canScheduleExactAlarms()=false; requested trigger at $triggerAtMillis was NOT set"
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
