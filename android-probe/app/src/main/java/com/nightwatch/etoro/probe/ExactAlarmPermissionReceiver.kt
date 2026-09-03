package com.nightwatch.etoro.probe

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * DESIGN §7.2/§7.3: "When the SCHEDULE_EXACT_ALARMS permission is revoked for your app, your
 * app stops, and all future exact alarms are canceled." This receiver detects the transition
 * either way and logs it loudly — a design requirement, not a nice-to-have.
 */
class ExactAlarmPermissionReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val granted = PowerStatus.canScheduleExactAlarms(context)
        EventStore.get(context).log(
            "EXACT_ALARM_PERMISSION_STATE_CHANGED",
            "canScheduleExactAlarms()=$granted"
        )
        NotificationHelper.post(
            context,
            "Nightwatch Probe: exact-alarm permission changed",
            "canScheduleExactAlarms() is now $granted"
        )
        if (granted) {
            // Mirrors DESIGN's "re-register alarms when the permission is granted back".
            AlarmScheduler.rescheduleFromSavedState(context)
        }
    }
}
