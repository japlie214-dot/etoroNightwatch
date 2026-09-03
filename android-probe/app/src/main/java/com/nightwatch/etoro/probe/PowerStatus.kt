package com.nightwatch.etoro.probe

import android.app.AlarmManager
import android.app.usage.UsageStatsManager
import android.content.Context
import android.os.BatteryManager
import android.os.Build
import android.os.PowerManager

/**
 * Mirrors the host permissions/state DESIGN §5 gate 2 and the v2 check battery (task requirement
 * 8/12) check on every real session: exact-alarm scheduling, the battery-optimization exemption,
 * the App Standby bucket, and Doze state. All of these can be silently changed by the OS (App
 * Hibernation, a battery-saver sweep, an OS update) — this probe surfaces their live state in the
 * UI and logs any change it observes.
 */
object PowerStatus {

    fun canScheduleExactAlarms(context: Context): Boolean {
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            val am = context.getSystemService(AlarmManager::class.java)
            am.canScheduleExactAlarms()
        } else {
            true // permission not required below API 31
        }
    }

    fun isIgnoringBatteryOptimizations(context: Context): Boolean {
        val pm = context.getSystemService(PowerManager::class.java)
        return pm.isIgnoringBatteryOptimizations(context.packageName)
    }

    fun batteryPercent(context: Context): Int {
        val bm = context.getSystemService(BatteryManager::class.java)
        return bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
    }

    fun isCharging(context: Context): Boolean {
        val bm = context.getSystemService(BatteryManager::class.java)
        val status = bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_STATUS)
        return status == BatteryManager.BATTERY_STATUS_CHARGING ||
            status == BatteryManager.BATTERY_STATUS_FULL
    }

    /**
     * Task requirement 8: getAppStandbyBucket() is the single best early-warning signal for
     * network being silently disabled — RARE and RESTRICTED disable network entirely (DESIGN
     * §1.2's App Standby table). No special permission is required to read the CALLING app's own
     * bucket.
     */
    fun standbyBucket(context: Context): String {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.P) return "N/A (API < 28)"
        val usm = context.getSystemService(UsageStatsManager::class.java) ?: return "unavailable"
        return when (usm.appStandbyBucket) {
            UsageStatsManager.STANDBY_BUCKET_ACTIVE -> "ACTIVE"
            UsageStatsManager.STANDBY_BUCKET_WORKING_SET -> "WORKING_SET"
            UsageStatsManager.STANDBY_BUCKET_FREQUENT -> "FREQUENT"
            UsageStatsManager.STANDBY_BUCKET_RARE -> "RARE (network disabled per DESIGN §1.2)"
            UsageStatsManager.STANDBY_BUCKET_RESTRICTED -> "RESTRICTED (network disabled per DESIGN §1.2)"
            else -> "unknown(${usm.appStandbyBucket})"
        }
    }

    /** Task requirement 12: is the device actually in Doze right now. */
    fun isDeviceIdleMode(context: Context): Boolean {
        val pm = context.getSystemService(PowerManager::class.java)
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) pm.isDeviceIdleMode else false
    }

    fun summary(context: Context): String {
        return "exactAlarm=${canScheduleExactAlarms(context)} " +
            "batteryExempt=${isIgnoringBatteryOptimizations(context)} " +
            "battery=${batteryPercent(context)}% charging=${isCharging(context)} " +
            "standbyBucket=${standbyBucket(context)} doze=${isDeviceIdleMode(context)}"
    }
}
