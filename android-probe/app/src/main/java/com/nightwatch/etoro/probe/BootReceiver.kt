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
    companion object {
        /** Maps a re-arm broadcast to its short reason; anything else is ignored. */
        internal fun reasonFor(action: String?): String? = when (action) {
            Intent.ACTION_BOOT_COMPLETED -> "BOOT"
            Intent.ACTION_MY_PACKAGE_REPLACED -> "UPDATE"
            Intent.ACTION_TIME_CHANGED -> "TIME_SET"
            Intent.ACTION_TIMEZONE_CHANGED -> "TIMEZONE"
            else -> null
        }
    }

    override fun onReceive(context: Context, intent: Intent) {
        val reason = reasonFor(intent.action) ?: return
        // Android drops alarms on reboot and on app update, and a clock or timezone change moves
        // the market-time target; all four must re-arm the next session alarm (T075).
        EventStore.get(context).log(if (reason == "BOOT") "BOOT_COMPLETED" else "ALARM_TRIGGER_$reason",
            "re-registering alarms")
        AlarmScheduler.rescheduleFromReceiver(context, goAsync(), reason)
    }
}
