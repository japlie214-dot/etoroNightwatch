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

/**
 * DESIGN §7.1 SessionService / §7.6 battery strategy, in miniature:
 * - Foreground service (dataSync) so Android doesn't kill the short run.
 * - Acquires a PARTIAL_WAKE_LOCK, released in `finally` — a wake lock does NOT escape Doze, it
 *   only holds the CPU once the alarm has already gotten us running. The held duration is logged
 *   (task Problem 4 check 10).
 * - On every wake (alarm OR FCM): network-after-wake check, Chaquopy smoke check, tzdata check
 *   (both runtimes), the full v2 check battery (Problem 4), then pushes a snapshot to the Worker
 *   (Problem 3) and reschedules the next alarm before releasing.
 */
class WakeService : Service() {

    private var wakeLock: PowerManager.WakeLock? = null
    private var wakeLockAcquiredAt: Long = 0L
    private val job = Job()
    private val scope = CoroutineScope(Dispatchers.IO + job)
    private val store by lazy { EventStore.get(this) }

    companion object {
        private const val NOTIF_ID = 7001
        const val EXTRA_TRIGGER = "trigger" // "alarm", "fcm", or "boot"
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val trigger = intent?.getStringExtra(EXTRA_TRIGGER) ?: "alarm"
        startForeground(NOTIF_ID, buildNotification())

        scope.launch {
            runWakeWork(trigger)
            stopSelf()
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

    private suspend fun runWakeWork(trigger: String) {
        acquireWakeLock()
        try {
            store.log("WAKE_START", "trigger=$trigger power=${PowerStatus.summary(this)}")
            NotificationHelper.post(this, "Nightwatch Probe woke up", "trigger=$trigger — running checks")

            // 1. Network-after-wake check (DESIGN §5 gate 3, §4.1).
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

            // 2. Chaquopy smoke check.
            try {
                val smokeResult = PyBridge.smoke(this)
                store.log("CHAQUOPY_SMOKE", "trigger=$trigger result=$smokeResult")
            } catch (e: Exception) {
                store.log("CHAQUOPY_SMOKE_FAILED", "trigger=$trigger error=${e.javaClass.simpleName}: ${e.message}")
                NotificationHelper.postFailure(this, "Chaquopy smoke check failed", "${e.javaClass.simpleName}: ${e.message}")
            }

            // 3. tzdata check, both runtimes.
            val kotlinTz = TzCheck.run()
            store.log("TZ_CHECK_KOTLIN", "trigger=$trigger result=$kotlinTz")
            try {
                val pyTz = PyBridge.tzCheck(this)
                store.log("TZ_CHECK_PYTHON", "trigger=$trigger result=$pyTz")
            } catch (e: Exception) {
                store.log("TZ_CHECK_PYTHON_FAILED", "trigger=$trigger error=${e.javaClass.simpleName}: ${e.message}")
            }

            // 4. Full v2 check battery (task Problem 4: all twelve checks, every wake).
            val checkResults = try {
                CheckBattery.runAll(this)
            } catch (e: Exception) {
                store.log("CHECK_BATTERY_FAILED", "trigger=$trigger error=${e.javaClass.simpleName}: ${e.message}")
                emptyList()
            }
            checkResults.forEach { r ->
                store.log("CHECK_${r.name}", "trigger=$trigger ${r.asEventDetail()}")
                if (!r.pass) {
                    NotificationHelper.postFailure(this, "Check ${r.name} failed", r.detail)
                }
            }

            // 5. Push a snapshot to the Worker — automatic on every wake (Problem 3).
            SnapshotPusher.pushNow(this, checkResults)

            // 6. Reschedule before releasing (DESIGN §4.1 step 11 — a crash here must not orphan
            //    the alarm). Only meaningful for the alarm trigger; FCM/boot wakes don't own the
            //    schedule the same way (boot already reschedules itself in BootReceiver).
            if (trigger == "alarm") {
                val next = AlarmScheduler.rescheduleFromSavedState(this)
                store.log("WAKE_RESCHEDULE", "next=$next")
            }

            val heldMs = System.currentTimeMillis() - wakeLockAcquiredAt
            store.log("WAKE_END", "trigger=$trigger wake_lock_held_ms=$heldMs")
            NotificationHelper.post(this, "Nightwatch Probe checks complete", "trigger=$trigger — see event log")
        } finally {
            releaseWakeLock()
        }
    }

    private fun acquireWakeLock() {
        val pm = getSystemService(PowerManager::class.java)
        wakeLockAcquiredAt = System.currentTimeMillis()
        wakeLock = pm.newWakeLock(
            PowerManager.PARTIAL_WAKE_LOCK,
            "NightwatchProbe:WakeLock"
        ).apply {
            setReferenceCounted(false)
            acquire(10 * 60 * 1000L /* 10 min safety timeout, never rely on this alone */)
        }
        store.log("WAKE_LOCK_ACQUIRED", "at=$wakeLockAcquiredAt")
    }

    private fun releaseWakeLock() {
        val heldMs = System.currentTimeMillis() - wakeLockAcquiredAt
        wakeLock?.let {
            if (it.isHeld) it.release()
        }
        wakeLock = null
        store.log("WAKE_LOCK_RELEASED", "held_ms=$heldMs")
    }

    override fun onDestroy() {
        super.onDestroy()
        releaseWakeLock()
        job.cancel()
    }
}
