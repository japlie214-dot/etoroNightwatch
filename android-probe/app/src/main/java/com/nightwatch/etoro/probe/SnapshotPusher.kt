package com.nightwatch.etoro.probe

import android.app.AlarmManager
import android.content.Context
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * Problem 3 — the headline feature: "This is the mini-server proof." Pushes a JSON snapshot of
 * device status + recent events + latest check results to the Worker's POST /snapshot, using
 * PHONE_PUSH_SECRET from a trusted runtime provider. The user then does a
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
        val original = JSONObject(snapshotFields(store.recent(50), lastCheckResults,
            nextAlarmEpochMs(context), HaltFlag.isHalted(context)) {
            PyBridge.redactDiagnostic(context, it)
        })
        val home = File(context.filesDir, "nightwatch_demo")
        if (!File(home, "ARM_DEMO").exists()) return original
        return mergeDashboard(original, PyBridge.dashboardSnapshot(context, home.absolutePath),
            redact = { PyBridge.redactDiagnostic(context, it) },
            failed = { store.log("SNAPSHOT_EXPORT_FAILED", it) })
    }

    internal fun mergeDashboard(
        original: JSONObject,
        exported: String?,
        redact: (String) -> String?,
        failed: (String) -> Unit = {}
    ): JSONObject {
        if (exported == null) {
            failed("BridgeUnavailable")
            return original
        }
        return try {
            val extra = JSONObject(exported)
            if (extra.length() == 0) return original
            if (extra.opt("environment") != "demo") return original
            fun redactReason(obj: JSONObject) {
                if (!obj.has("reason")) return
                val reason = obj.opt("reason") as? String
                val safe = try { reason?.let(redact)?.take(300) } catch (_: Exception) { null }
                obj.remove("reason")
                safe?.let { obj.put("reason", it) }
            }
            extra.optJSONObject("last_decision")?.let { redactReason(it) }
            extra.optJSONArray("recent_sessions")?.let { rows ->
                for (i in 0 until rows.length()) rows.optJSONObject(i)?.let { redactReason(it) }
            }
            val merged = JSONObject(original.toString())
            for (key in listOf("environment", "trading_date", "account", "sleeve", "positions",
                    "last_session", "last_decision", "recent_sessions", "seed")) {
                if (extra.has(key)) merged.put(key, extra.get(key))
            }
            if (merged.toString().toByteArray(Charsets.UTF_8).size > 256 * 1024) {
                failed("PayloadTooLarge")
                original
            } else merged
        } catch (e: Exception) {
            failed(e.javaClass.simpleName)
            original
        }
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
        val secret = RuntimeCredentials.value(RuntimeCredentials.Key.PHONE_PUSH_SECRET)
            ?: return Result(false, null, "snapshot authentication unavailable: runtime provisioning required")
        return try {
            val body = snapshot.toString().toRequestBody("application/json".toMediaType())
            val request = Request.Builder()
                .url("${BuildConfig.WORKER_BASE_URL}/snapshot")
                .header("x-phone-push-secret", secret)
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
