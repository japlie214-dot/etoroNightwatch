package com.nightwatch.etoro.probe

import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
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
 *   only holds the CPU once the alarm has already gotten us running.
 * - On every wake (alarm OR FCM, task requirement 3): network-after-wake check, Chaquopy smoke
 *   check, tzdata check (both runtimes), then reschedule the next alarm before releasing.
 */
class WakeService : Service() {

    private var wakeLock: PowerManager.WakeLock? = null
    private val job = Job()
    private val scope = CoroutineScope(Dispatchers.IO + job)
    private val store by lazy { EventStore.get(this) }

    companion object {
        private const val NOTIF_ID = 7001
        const val EXTRA_TRIGGER = "trigger" // "alarm" or "fcm"
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
            .setContentText("Network + Chaquopy + tzdata wake checks in progress")
            .setOngoing(true)
            .build()
    }

    private suspend fun runWakeWork(trigger: String) {
        acquireWakeLock()
        try {
            store.log("WAKE_START", "trigger=$trigger power=${PowerStatus.summary(this)}")

            // 1. Network-after-wake check (task requirement 3 / DESIGN §5 gate 3, §4.1).
            val netResult = NetworkCheck.checkHealth(BuildConfig.WORKER_BASE_URL)
            store.log(
                "NETWORK_CHECK",
                "trigger=$trigger success=${netResult.success} attempts=${netResult.attempts} " +
                    "latency_ms=${netResult.totalLatencyMs} status=${netResult.httpStatus} " +
                    "error=${netResult.error}"
            )

            // 2. Chaquopy smoke check (task requirement 5).
            try {
                val smokeResult = PyBridge.smoke(this)
                store.log("CHAQUOPY_SMOKE", "trigger=$trigger result=$smokeResult")
            } catch (e: Exception) {
                store.log("CHAQUOPY_SMOKE_FAILED", "trigger=$trigger error=${e.javaClass.simpleName}: ${e.message}")
            }

            // 3. tzdata check, both runtimes (task requirement 6).
            val kotlinTz = TzCheck.run()
            store.log("TZ_CHECK_KOTLIN", "trigger=$trigger result=$kotlinTz")
            try {
                val pyTz = PyBridge.tzCheck(this)
                store.log("TZ_CHECK_PYTHON", "trigger=$trigger result=$pyTz")
            } catch (e: Exception) {
                store.log("TZ_CHECK_PYTHON_FAILED", "trigger=$trigger error=${e.javaClass.simpleName}: ${e.message}")
            }

            // 4. Reschedule before releasing (DESIGN §4.1 step 11 — a crash here must not orphan
            //    the alarm). Only meaningful for the alarm trigger; FCM wakes don't own the
            //    schedule.
            if (trigger == "alarm") {
                val next = AlarmScheduler.rescheduleFromSavedState(this)
                store.log("WAKE_RESCHEDULE", "next=$next")
            }

            store.log("WAKE_END", "trigger=$trigger")
        } finally {
            releaseWakeLock()
        }
    }

    private fun acquireWakeLock() {
        val pm = getSystemService(PowerManager::class.java)
        wakeLock = pm.newWakeLock(
            PowerManager.PARTIAL_WAKE_LOCK,
            "NightwatchProbe:WakeLock"
        ).apply {
            setReferenceCounted(false)
            acquire(10 * 60 * 1000L /* 10 min safety timeout, never rely on this alone */)
        }
    }

    private fun releaseWakeLock() {
        wakeLock?.let {
            if (it.isHeld) it.release()
        }
        wakeLock = null
    }

    override fun onDestroy() {
        super.onDestroy()
        releaseWakeLock()
        job.cancel()
    }
}
