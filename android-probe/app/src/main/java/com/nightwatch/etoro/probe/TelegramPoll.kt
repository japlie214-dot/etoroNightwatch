package com.nightwatch.etoro.probe

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.PowerManager
import org.json.JSONObject
import java.io.File

/**
 * T084: answers pending Telegram commands between sessions. Before this, the bot was read only
 * at the start of a session (up to a day apart). A repeating inexact alarm calls the same Python
 * path the session uses (`nightwatch.android_entry.poll_telegram`), so sender, chat and
 * staleness checks are unchanged. It never reads the broker and never trades.
 */
internal object TelegramPollScheduler {
    const val INTERVAL_MS = 15 * 60 * 1000L // Doze allows about one alarm per 9 minutes per app
    const val FIRST_DELAY_MS = 60_000L
    private const val REQUEST_CODE = 8402
    private const val PREFS = "nightwatch_probe_telegram_poll"
    private const val KEY_NEXT = "next_poll_at"
    const val OVERDUE_GRACE_MS = 2 * 60 * 1000L

    private fun pending(context: Context, flags: Int): PendingIntent? = PendingIntent.getBroadcast(
        context, REQUEST_CODE, Intent(context, TelegramPollReceiver::class.java),
        flags or PendingIntent.FLAG_IMMUTABLE)

    fun schedule(context: Context, delayMs: Long = INTERVAL_MS) {
        val intent = pending(context, PendingIntent.FLAG_UPDATE_CURRENT) ?: return
        val alarms = context.getSystemService(Context.ALARM_SERVICE) as AlarmManager
        val at = System.currentTimeMillis() + delayMs
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putLong(KEY_NEXT, at).apply()
        // An inexact alarm drifted up to ~26 minutes on this phone. The exact-alarm permission is
        // already granted for the session alarm; fall back to inexact if it is ever revoked.
        val exact = android.os.Build.VERSION.SDK_INT < 31 || alarms.canScheduleExactAlarms()
        try {
            if (exact) alarms.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, intent)
            else alarms.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, intent)
        } catch (_: SecurityException) {
            alarms.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, intent)
        }
    }

    internal fun shouldRearm(pendingExists: Boolean, nextAt: Long, now: Long): Boolean =
        !pendingExists || nextAt == 0L || now > nextAt + OVERDUE_GRACE_MS

    /** Cheap on every process start: arms the chain only when no alarm is pending. */
    fun ensureScheduled(context: Context) {
        // The PendingIntent outlives a lost alarm, so also treat an overdue recorded time as lost.
        val next = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getLong(KEY_NEXT, 0L)
        if (shouldRearm(pending(context, PendingIntent.FLAG_NO_CREATE) != null, next, System.currentTimeMillis())) {
            schedule(context, FIRST_DELAY_MS)
        }
    }
}

class TelegramPollReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        // Re-arm first so a crash or a skipped poll can never end the chain.
        TelegramPollScheduler.schedule(context)
        val app = context.applicationContext
        val pending = goAsync()
        val wake = (app.getSystemService(Context.POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "nightwatch:telegram-poll")
        wake.acquire(60_000L)
        Thread {
            try {
                poll(app)
            } finally {
                if (wake.isHeld) wake.release()
                pending.finish()
            }
        }.start()
    }

    private fun poll(context: Context) {
        val store = EventStore.get(context)
        val home = File(context.filesDir, "nightwatch_demo")
        if (!File(home, "config/config.toml").isFile) return
        val raw = PyBridge.pollTelegram(context, home.absolutePath)
        // Skipped (a session holds the lock, which polls for itself): nothing to record.
        if (raw == null) return
        try {
            val result = JSONObject(raw)
            val status = result.optString("status", "unknown").takeIf { Regex("^[a-z_]{1,20}$").matches(it) } ?: "other"
            val processed = result.optInt("processed", 0)
            // Quiet when idle: one line per 15 minutes would swamp the event log.
            if (processed > 0 || status !in setOf("ok", "busy")) {
                store.log("TELEGRAM_POLL", "status=$status processed=$processed")
            }
        } catch (_: Exception) {
            store.log("TELEGRAM_POLL", "status=bad_response processed=0")
        }
    }
}
