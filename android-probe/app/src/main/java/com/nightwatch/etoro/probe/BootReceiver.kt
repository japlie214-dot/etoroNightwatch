package com.nightwatch.etoro.probe

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * DESIGN §7.5: alarms do not survive a reboot on their own. This must be tested explicitly —
 * reboot the phone and confirm the log shows BOOT_COMPLETED followed by ALARM_SCHEDULED.
 * With autostart disabled on OriginOS, this receiver will silently fail to run at all, which is
 * exactly the failure mode that looks like nothing happened — hence README.md's OriginOS
 * checklist insists autostart is enabled before any overnight test.
 *
 * Recovery is bounded independently of synchronous Chaquopy. Do not start a dataSync foreground
 * service from boot on targetSdk 36; the next alarm wake or schedule command repairs a timeout.
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        EventStore.get(context).log("BOOT_COMPLETED", "re-registering alarms")
        AlarmScheduler.rescheduleFromReceiver(context, goAsync(), "BOOT")
    }
}
