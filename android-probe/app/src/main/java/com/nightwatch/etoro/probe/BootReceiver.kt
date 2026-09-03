package com.nightwatch.etoro.probe

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import androidx.core.content.ContextCompat

/**
 * DESIGN §7.5: alarms do not survive a reboot on their own. This must be tested explicitly —
 * reboot the phone and confirm the log shows BOOT_COMPLETED followed by ALARM_SCHEDULED.
 * With autostart disabled on OriginOS, this receiver will silently fail to run at all, which is
 * exactly the failure mode that looks like nothing happened — hence README.md's OriginOS
 * checklist insists autostart is enabled before any overnight test.
 *
 * v2: also re-registers via the standard WakeService path (task "also push a snapshot
 * automatically: on every alarm wake, on boot, and on demand") so a boot is visible in the pushed
 * snapshot the same way an alarm/FCM wake is.
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        EventStore.get(context).log("BOOT_COMPLETED", "re-registering alarms")
        val next = AlarmScheduler.rescheduleFromSavedState(context)
        EventStore.get(context).log(
            "BOOT_RESCHEDULE_RESULT",
            if (next != null) "rescheduled for epoch $next" else "no saved alarm mode to restore"
        )
        NotificationHelper.post(
            context,
            "Nightwatch Probe: booted",
            if (next != null) "Alarm re-registered for epoch $next" else "No saved schedule to restore"
        )
        val serviceIntent = Intent(context, WakeService::class.java)
            .putExtra(WakeService.EXTRA_TRIGGER, "boot")
        ContextCompat.startForegroundService(context, serviceIntent)
    }
}
