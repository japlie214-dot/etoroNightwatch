package com.nightwatch.etoro.probe

import android.app.AlarmManager
import android.content.Context
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
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
        return JSONObject(snapshotFields(store.recent(50), lastCheckResults,
            nextAlarmEpochMs(context), HaltFlag.isHalted(context)) {
            PyBridge.redactDiagnostic(context, it)
        })
    }

    internal fun snapshotFields(
        events: List<EventStore.Event>,
        checks: List<CheckBattery.CheckResult>?,
        nextRun: Long?,
        halt: Boolean,
        redact: (String) -> String?
    ): Map<String, Any?> {
        fun safe(text: String): String? = try { redact(text) } catch (_: Exception) { null }
        return mapOf(
            "probe" to "nightwatch-android-probe-v2",
            "device" to mapOf("next_alarm_epoch_ms" to nextRun),
            "halt" to halt,
            "recent_events" to events.map { event ->
                mutableMapOf<String, Any>(
                    "type" to (safe(event.type) ?: "EVENT"),
                    "ts_epoch_ms" to event.tsEpochMs
                ).apply {
                    safe(event.detail)?.let { put("detail", it) }
                }
            },
            "latest_checks" to (checks ?: emptyList()).map { check ->
                mutableMapOf<String, Any>(
                    "name" to (safe(check.name) ?: "CHECK"), "pass" to check.pass
                ).apply {
                    safe(check.detail)?.let { put("detail", it) }
                }
            }
        )
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
                    detail = "status=${response.code}"
                )
            }
        } catch (e: IOException) {
            Result(false, null, "error=${e.javaClass.simpleName}")
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
