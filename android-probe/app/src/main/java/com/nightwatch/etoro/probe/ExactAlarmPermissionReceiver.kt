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
        if (granted) {
            NotificationHelper.post(
                context,
                "Nightwatch Probe: exact-alarm permission changed",
                "canScheduleExactAlarms() is now $granted"
            )
        } else {
            // DESIGN §5 gate 2 / §7.2: "your app stops, and all future exact alarms are
            // canceled" — this must fail loudly, not just log quietly (task Problem 1).
            NotificationHelper.postFailure(
                context,
                "Exact-alarm permission REVOKED",
                "All future exact alarms are canceled until this is re-granted: " +
                    "Settings -> Apps -> Special app access -> Alarms & reminders"
            )
        }
        if (granted) {
            // Mirrors DESIGN's "re-register alarms when the permission is granted back".
            AlarmScheduler.rescheduleFromSavedState(context)
        }
    }
}
