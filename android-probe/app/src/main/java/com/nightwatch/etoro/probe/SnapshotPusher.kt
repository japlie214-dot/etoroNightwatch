package com.nightwatch.etoro.probe

import android.app.AlarmManager
import android.content.Context
import android.os.Build
import android.os.SystemClock
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Problem 3 — the headline feature: "This is the mini-server proof." Pushes a JSON snapshot of
 * device status + recent events + latest check results to the Worker's POST /snapshot, using
 * PHONE_PUSH_SECRET (already wired via BuildConfig from local.properties). The user then does a
 * plain `GET /snapshot` from their side to read what the phone is doing.
 */
object SnapshotPusher {
    private val http = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(20, TimeUnit.SECONDS)
        .build()

    data class Result(val success: Boolean, val httpStatus: Int?, val detail: String)

    fun buildSnapshot(
        context: Context,
        lastCheckResults: List<CheckBattery.CheckResult>?
    ): JSONObject {
        val store = EventStore.get(context)
        val device = JSONObject().apply {
            put("app_version_code", BuildConfig.VERSION_CODE)
            put("app_version_name", BuildConfig.VERSION_NAME)
            put("battery_pct", PowerStatus.batteryPercent(context))
            put("charging", PowerStatus.isCharging(context))
            put("exact_alarm_permission", PowerStatus.canScheduleExactAlarms(context))
            put("battery_exempt", PowerStatus.isIgnoringBatteryOptimizations(context))
            put("standby_bucket", PowerStatus.standbyBucket(context))
            put("doze_active", PowerStatus.isDeviceIdleMode(context))
            put("uptime_ms", SystemClock.elapsedRealtime())
            put("android_release", Build.VERSION.RELEASE)
            put("android_sdk_int", Build.VERSION.SDK_INT)
            put("device", "${Build.MANUFACTURER} ${Build.MODEL}")
            put("next_alarm_epoch_ms", nextAlarmEpochMs(context))
            put("server_time_now", java.util.Date().toString())
        }

        val events = JSONArray()
        store.recent(50).forEach { events.put(it.formatted()) }

        val checks = JSONArray()
        lastCheckResults?.forEach { r ->
            checks.put(
                JSONObject().apply {
                    put("name", r.name)
                    put("pass", r.pass)
                    put("detail", r.detail)
                }
            )
        }

        return JSONObject().apply {
            put("probe", "nightwatch-android-probe-v2")
            put("device", device)
            put("recent_events", events)
            put("latest_checks", checks)
        }
    }

    private fun nextAlarmEpochMs(context: Context): Long? {
        val am = context.getSystemService(AlarmManager::class.java)
        return am.nextAlarmClock?.triggerTime
    }

    fun push(context: Context, snapshot: JSONObject): Result {
        return try {
            val body = snapshot.toString().toRequestBody("application/json".toMediaType())
            val request = Request.Builder()
                .url("${BuildConfig.WORKER_BASE_URL}/snapshot")
                .header("x-phone-push-secret", BuildConfig.PHONE_PUSH_SECRET)
                .post(body)
                .build()
            http.newCall(request).execute().use { response ->
                Result(
                    success = response.isSuccessful,
                    httpStatus = response.code,
                    detail = "status=${response.code} body=${response.body?.string()?.take(300)}"
                )
            }
        } catch (e: IOException) {
            Result(false, null, "${e.javaClass.simpleName}: ${e.message}")
        }
    }

    /** Convenience: build + push in one call, logging the result either way. */
    fun pushNow(context: Context, lastCheckResults: List<CheckBattery.CheckResult>? = null) {
        val store = EventStore.get(context)
        val snapshot = buildSnapshot(context, lastCheckResults)
        val result = push(context, snapshot)
        if (result.success) {
            store.log("SNAPSHOT_PUSHED", result.detail)
        } else {
            store.log("SNAPSHOT_PUSH_FAILED", result.detail)
            NotificationHelper.postFailure(context, "Snapshot push failed", result.detail)
        }
    }
}
