package com.nightwatch.etoro.probe

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import androidx.core.content.ContextCompat

/** Fires on the alarm. Does no work itself (DESIGN §7.1) — immediately starts WakeService. */
class SessionAlarmReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        EventStore.get(context).log("ALARM_FIRED", "SessionAlarmReceiver.onReceive")
        val serviceIntent = Intent(context, WakeService::class.java)
            .putExtra(WakeService.EXTRA_TRIGGER, "alarm")
        ContextCompat.startForegroundService(context, serviceIntent)
    }
}
