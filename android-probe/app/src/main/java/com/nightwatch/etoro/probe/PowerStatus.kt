package com.nightwatch.etoro.probe

import android.app.AlarmManager
import android.content.Context
import android.os.BatteryManager
import android.os.Build
import android.os.PowerManager

/**
 * Mirrors the two host permissions DESIGN §5 gate 2 checks on every real session: exact-alarm
 * scheduling and the battery-optimization exemption. Both can be silently revoked by the OS
 * (App Hibernation, a battery-saver sweep, an OS update) — this probe surfaces their live state
 * in the UI and logs any change it observes.
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

    fun summary(context: Context): String {
        return "exactAlarm=${canScheduleExactAlarms(context)} " +
            "batteryExempt=${isIgnoringBatteryOptimizations(context)} " +
            "battery=${batteryPercent(context)}% charging=${isCharging(context)}"
    }
}
