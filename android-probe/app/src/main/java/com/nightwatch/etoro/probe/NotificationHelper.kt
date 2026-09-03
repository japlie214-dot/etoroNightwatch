package com.nightwatch.etoro.probe

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.os.Build
import androidx.core.app.NotificationCompat

/**
 * DESIGN §8.2: every FCM command must post a VISIBLE notification, or Google demotes the app's
 * high-priority FCM privilege after ~7 days of silent sends. That is not cosmetic — it's the
 * mechanism that keeps getPriority() reporting PRIORITY_HIGH across the multi-night test.
 *
 * v2 (Problem 1 — "silent success"): v1 posted nothing on a routine alarm fire, so the user saw
 * nothing and reasonably concluded it failed. Now every significant event posts a notification:
 * routine events (alarm fired, FCM received, command executed, snapshot pushed) go on a
 * low-importance channel so they don't wake the phone with a sound every time; any check
 * FAILING goes on a separate default-importance channel so failures actually surface instead of
 * getting lost among routine chatter.
 */
object NotificationHelper {
    private const val CHANNEL_ROUTINE = "nightwatch_probe_events"
    private const val CHANNEL_FAILURE = "nightwatch_probe_failures"
    private var nextNotificationId = 1000

    fun ensureChannel(context: Context) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val mgr = context.getSystemService(NotificationManager::class.java)
            mgr.createNotificationChannel(
                NotificationChannel(
                    CHANNEL_ROUTINE,
                    context.getString(R.string.notification_channel_name),
                    NotificationManager.IMPORTANCE_LOW
                ).apply { description = "Routine wake/FCM/alarm/command events" }
            )
            mgr.createNotificationChannel(
                NotificationChannel(
                    CHANNEL_FAILURE,
                    context.getString(R.string.notification_channel_name_failure),
                    NotificationManager.IMPORTANCE_DEFAULT
                ).apply { description = "Any check that FAILED — these must not be missed" }
            )
        }
    }

    /** Routine event: alarm fired, FCM received, command executed, snapshot pushed, etc. */
    @Synchronized
    fun post(context: Context, title: String, text: String) {
        postOn(context, CHANNEL_ROUTINE, android.R.drawable.stat_notify_sync, title, text)
    }

    /** Any check FAILING — a test tool that succeeds silently is worse than useless. */
    @Synchronized
    fun postFailure(context: Context, title: String, text: String) {
        postOn(context, CHANNEL_FAILURE, android.R.drawable.stat_notify_error, "FAILED: $title", text)
    }

    private fun postOn(
        context: Context,
        channelId: String,
        icon: Int,
        title: String,
        text: String
    ) {
        ensureChannel(context)
        val notification = NotificationCompat.Builder(context, channelId)
            .setSmallIcon(icon)
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setPriority(
                if (channelId == CHANNEL_FAILURE) NotificationCompat.PRIORITY_DEFAULT
                else NotificationCompat.PRIORITY_LOW
            )
            .setAutoCancel(true)
            .build()
        val mgr = context.getSystemService(NotificationManager::class.java)
        mgr.notify(nextNotificationId++, notification)
    }
}
