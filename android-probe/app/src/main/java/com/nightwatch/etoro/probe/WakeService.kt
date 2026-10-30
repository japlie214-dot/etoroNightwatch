package com.nightwatch.etoro.probe

import android.app.Notification
import android.app.Service
import android.content.Intent
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.io.File
import java.util.concurrent.atomic.AtomicBoolean

/** Probe battery plus the alarm-only dry-run desktop-host simulation on Android.
 * Saved alarms are registered before long work, including market-calendar/DST-aware scheduling.
 * Process death, Doze, power gating and device overlap remain device proof.
 */
class WakeService : Service() {

    private val job = Job()
    private val scope = CoroutineScope(Dispatchers.IO + job)
    private val store by lazy { EventStore.get(this) }
    private val sessionRunning = AtomicBoolean(false)
    private val invocationLock = Any()
    private var activeInvocations = 0

    companion object {
        private const val NOTIF_ID = 7001
        const val EXTRA_TRIGGER = "trigger" // "alarm", "fcm", or "boot"
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val trigger = intent?.getStringExtra(EXTRA_TRIGGER) ?: "alarm"
        val skipSession = sessionRunning.get()
        synchronized(invocationLock) { activeInvocations++ }
        startForeground(NOTIF_ID, buildNotification())
        scope.launch {
            try {
                runWakeWork(trigger, skipSession)
            } finally {
                synchronized(invocationLock) {
                    activeInvocations--
                    if (activeInvocations == 0) stopSelf()
                }
            }
        }
        return START_NOT_STICKY
    }

    private fun buildNotification(): Notification {
        NotificationHelper.ensureChannel(this)
        return NotificationCompat.Builder(this, "nightwatch_probe_events")
            .setSmallIcon(android.R.drawable.stat_sys_download)
            .setContentTitle("Nightwatch Probe running checks")
            .setContentText("Full check battery + snapshot push in progress")
            .setOngoing(true)
            .build()
    }

    private suspend fun runWakeWork(trigger: String, skipSession: Boolean) {
        val acquiredAt = System.currentTimeMillis()
        val wakeLock = newWakeLock("NightwatchProbe:WakeLock")
        try {
            wakeLock.acquire(10 * 60 * 1000L)
            try {
                HaltFlag.reconcile(this)
            } catch (e: Exception) {
                store.log("WAKE_HALT_CHECK_FAILED", "error=${e.javaClass.simpleName}")
            }
            // One-shot saved modes return null, retaining their no-reschedule semantics.
            if (trigger == "alarm") {
                try {
                    val next = AlarmScheduler.rescheduleFromSavedState(this)
                    store.log("WAKE_RESCHEDULE", "next=$next")
                } catch (e: Exception) {
                    store.log("WAKE_RESCHEDULE_FAILED", "error=${e.javaClass.simpleName}")
                }
            }
            store.log("WAKE_START", "trigger=$trigger power=${PowerStatus.summary(this)}")
            NotificationHelper.post(this, "Nightwatch Probe woke up", "trigger=$trigger — running checks")

            // 1. Network-after-wake check.
            try {
                val netResult = NetworkCheck.checkHealth(BuildConfig.WORKER_BASE_URL)
                store.log(
                    "NETWORK_CHECK",
                    "trigger=$trigger success=${netResult.success} attempts=${netResult.attempts} " +
                        "latency_ms=${netResult.totalLatencyMs} status=${netResult.httpStatus} " +
                        "error=${netResult.error}"
                )
                if (!netResult.success) {
                    NotificationHelper.postFailure(this, "Network check failed", "trigger=$trigger ${netResult.error}")
                }
            } catch (e: Exception) {
                store.log("NETWORK_CHECK_FAILED", "error=${e.javaClass.simpleName}")
            }

            // 2. Chaquopy smoke check.
            try {
                val smokeResult = PyBridge.smoke(this)
                store.log("CHAQUOPY_SMOKE", "trigger=$trigger result=$smokeResult")
            } catch (e: Exception) {
                store.log("CHAQUOPY_SMOKE_FAILED", "trigger=$trigger error=${e.javaClass.simpleName}: ${e.message}")
                NotificationHelper.postFailure(this, "Chaquopy smoke check failed", "${e.javaClass.simpleName}: ${e.message}")
            }

            // 3. tzdata check, both runtimes.
            try {
                val kotlinTz = TzCheck.run()
                store.log("TZ_CHECK_KOTLIN", "trigger=$trigger result=$kotlinTz")
            } catch (e: Exception) {
                store.log("TZ_CHECK_KOTLIN_FAILED", "error=${e.javaClass.simpleName}")
            }
            try {
                val pyTz = PyBridge.tzCheck(this)
                store.log("TZ_CHECK_PYTHON", "trigger=$trigger result=$pyTz")
            } catch (e: Exception) {
                store.log("TZ_CHECK_PYTHON_FAILED", "trigger=$trigger error=${e.javaClass.simpleName}: ${e.message}")
            }

            // 4. Full v2 check battery.
            val checkResults = try {
                CheckBattery.runAll(this).also { results ->
                    results.forEach { r ->
                        store.log("CHECK_${r.name}", "trigger=$trigger ${r.asEventDetail()}")
                        if (!r.pass) NotificationHelper.postFailure(this, "Check ${r.name} failed", r.detail)
                    }
                }
            } catch (e: Exception) {
                store.log("CHECK_BATTERY_FAILED", "trigger=$trigger error=${e.javaClass.simpleName}: ${e.message}")
                emptyList()
            }

            // 5. Snapshot failures must not prevent the session/finalization.
            try {
                SnapshotPusher.pushNow(this, checkResults)
            } catch (e: Exception) {
                store.log("SNAPSHOT_PUSH_FAILED", "error=${e.javaClass.simpleName}")
            }

            // 5b. Only alarms enter the new read-role, never-armed Python route.
            if (trigger == "alarm") {
                if (HaltFlag.isHalted(this)) {
                    store.log("SESSION_SKIPPED_HALT", "trigger=$trigger")
                } else if (skipSession || !sessionRunning.compareAndSet(false, true)) {
                    store.log("SESSION_SKIPPED_BUSY", "trigger=$trigger")
                } else {
                    try {
                        runSessionDry()
                    } catch (e: Exception) {
                        store.log("SESSION_STEP_FAILED", "error=${e.javaClass.simpleName}")
                        NotificationHelper.postFailure(this, "Nightwatch session step failed", e.javaClass.simpleName)
                    } finally {
                        sessionRunning.set(false)
                    }
                }
            }
            store.log("WAKE_END", "trigger=$trigger wake_lock_held_ms=${System.currentTimeMillis() - acquiredAt}")
            NotificationHelper.post(this, "Nightwatch Probe checks complete", "trigger=$trigger — see event log")
        } finally {
            if (wakeLock.isHeld) wakeLock.release()
            store.log("WAKE_LOCK_RELEASED", "held_ms=${System.currentTimeMillis() - acquiredAt}")
        }
    }

    private fun runSessionDry() {
        var sessionWakeLock: PowerManager.WakeLock? = null
        val result = try {
            val handle = newWakeLock("NightwatchProbe:Session")
            sessionWakeLock = handle
            handle.acquire(50 * 60 * 1000L)
            if (HaltFlag.isHalted(this)) {
                store.log("SESSION_SKIPPED_HALT", "latched before Python session call")
                return
            }
            val battery = PowerStatus.batteryPercent(this)
            val charging = PowerStatus.isCharging(this)
            if (!PowerStatus.shouldRunSession(charging, battery)) {
                val detail = "battery=$battery charging=$charging"
                store.log("SESSION_SKIPPED_POWER", detail)
                NotificationHelper.postFailure(this, "Nightwatch session skipped: low power", detail)
                return
            }
            val raw = PyBridge.runSessionDry(this, File(filesDir, "nightwatch_config").absolutePath)
            val parsed = JSONObject(raw)
            require(parsed.get("status") is String && parsed.get("armed") == false)
            require(parsed.get("halt") is Boolean)
            require(parsed.get("reason_code") is String)
            require(parsed.get("error_type") == JSONObject.NULL || parsed.get("error_type") is String)
            for (key in listOf("session_id", "failed_gate")) {
                val value = parsed.get(key)
                require(value == JSONObject.NULL || value is Int || value is Long)
            }
            val gates = parsed.getJSONArray("gates")
            for (index in 0 until gates.length()) {
                val gate = gates.getJSONArray(index)
                require(gate.length() == 3 && gate.get(0) is Int &&
                    gate.get(1) is String && gate.get(2) is Boolean)
            }
            parsed
        } catch (e: Exception) {
            // Interpreter/module/call/JSON failures never expose exception messages.
            JSONObject().put("status", "bridge_error").put("armed", false)
                .put("session_id", JSONObject.NULL).put("failed_gate", JSONObject.NULL)
                .put("reason_code", "bridge_error").put("error_type", e.javaClass.simpleName)
                .put("gates", org.json.JSONArray()).put("halt", HaltFlag.isHalted(this))
        } finally {
            sessionWakeLock?.let { if (it.isHeld) it.release() }
        }
        try {
            if (result.optBoolean("halt", false) ||
                (result.optString("status") == "preflight_failed" && result.optInt("failed_gate") == 1)) {
                synchronized(AlarmScheduler) {
                    HaltFlag.set(this, true)
                    try {
                        AlarmScheduler.cancel(this)
                    } finally {
                        NotificationHelper.postOngoingHalt(this)
                    }
                    store.log("HALT_LINKED", "session HALT latched; sessions stopped until resume")
                }
            }
        } catch (e: Exception) {
            store.log("HALT_LINK_FAILED", "error=${e.javaClass.simpleName}")
            NotificationHelper.postFailure(this, "HALT link failed", e.javaClass.simpleName)
        }
        try {
            val line = result.toString()
            store.log("SESSION_RESULT", line)
            if (result.getString("status") in setOf("would_have_done", "already_ran", "busy", "out_of_window")) {
                NotificationHelper.post(this, "Nightwatch dry-run session", line)
            } else {
                NotificationHelper.postFailure(this, "Nightwatch dry-run session failed", line)
            }
        } catch (e: Exception) {
            store.log("SESSION_RESULT_FAILED", "error=${e.javaClass.simpleName}")
        }
    }

    private fun newWakeLock(tag: String): PowerManager.WakeLock =
        getSystemService(PowerManager::class.java)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, tag).apply { setReferenceCounted(false) }

    override fun onDestroy() {
        super.onDestroy()
        // Each invocation owns and releases its handles in finally, never another wake's lock.
        job.cancel()
    }
}
