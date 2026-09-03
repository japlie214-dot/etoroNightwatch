package com.nightwatch.etoro.probe

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.os.Build
import androidx.core.app.NotificationCompat

/**
 * DESIGN §8.2: every FCM command must post a VISIBLE notification, or Google demotes the app's
 * high-priority FCM privilege after ~7 days of silent sends. This is not cosmetic — it's the
 * mechanism that keeps getPriority() reporting PRIORITY_HIGH across the multi-night test.
 */
object NotificationHelper {
    private const val CHANNEL_ID = "nightwatch_probe_events"
    private var nextNotificationId = 1000

    fun ensureChannel(context: Context) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val mgr = context.getSystemService(NotificationManager::class.java)
            val channel = NotificationChannel(
                CHANNEL_ID,
                context.getString(R.string.notification_channel_name),
                NotificationManager.IMPORTANCE_HIGH
            ).apply {
                description = "Nightwatch probe wake/FCM/alarm events"
            }
            mgr.createNotificationChannel(channel)
        }
    }

    @Synchronized
    fun post(context: Context, title: String, text: String) {
        ensureChannel(context)
        val notification = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)
            .build()
        val mgr = context.getSystemService(NotificationManager::class.java)
        mgr.notify(nextNotificationId++, notification)
    }
}
