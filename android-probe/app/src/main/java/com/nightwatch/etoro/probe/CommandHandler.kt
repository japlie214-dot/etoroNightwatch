package com.nightwatch.etoro.probe

import android.content.Context
import android.os.SystemClock
import kotlinx.coroutines.runBlocking

/**
 * Problem 3 — the headline feature: the phone's side of the full remote-command loop.
 *
 * Handles both the HMAC-verified per-command channel (POST /command, task commands: ping,
 * status, logs, runchecks, schedule, selftest — plus the DESIGN §8.2 read-only set as far as this
 * probe can stand in for them) and the two commands the Worker sends unauthenticated-by-hmac
 * because they are already gated by WEB_READ_TOKEN at the Worker: "refresh" (POST /refresh) and
 * "run_session" (the redundant daily cron, DESIGN §7.2/§9.1).
 *
 * NOTE on the Worker's own allowlist: cloud/worker/src/commands.js's ALLOWED_COMMANDS currently
 * only contains the DESIGN §8.2 read-only/halt set (status, positions, pnl, today, logs, next,
 * halt, resume, help) — it does not yet include ping/runchecks/schedule/selftest. This file is
 * built out per the task brief so the phone is ready the moment that allowlist is extended; until
 * then, sending those four via `POST /command` against the live Worker will be rejected by the
 * Worker itself before it ever reaches the phone (cloud/ is out of scope for this build — see
 * README.md's "known gap" note).
 */
object CommandHandler {

    fun handle(context: Context, command: String, data: Map<String, String>) {
        val store = EventStore.get(context)
        when (command) {
            "ping" -> {
                val detail = "uptime_ms=${SystemClock.elapsedRealtime()} " +
                    "app_version=${BuildConfig.VERSION_NAME}(${BuildConfig.VERSION_CODE})"
                store.log("COMMAND_PING", detail)
                NotificationHelper.post(context, "Nightwatch: ping", detail)
            }

            "status" -> {
                val detail = PowerStatus.summary(context) +
                    " uptime_ms=${SystemClock.elapsedRealtime()} " +
                    "app_version=${BuildConfig.VERSION_NAME}(${BuildConfig.VERSION_CODE})"
                store.log("COMMAND_STATUS", detail)
                NotificationHelper.post(context, "Nightwatch: status", detail)
                SnapshotPusher.pushNow(context)
            }

            "logs" -> {
                store.log("COMMAND_LOGS", "pushing recent event log to Worker")
                SnapshotPusher.pushNow(context)
                NotificationHelper.post(context, "Nightwatch: logs", "Recent event log pushed to Worker snapshot")
            }

            "runchecks", "selftest" -> {
                store.log("COMMAND_${command.uppercase()}", "running full check battery")
                runBlocking {
                    val results = CheckBattery.runAll(context)
                    results.forEach { r ->
                        store.log("CHECK_${r.name}", r.asEventDetail())
                        if (!r.pass) {
                            NotificationHelper.postFailure(context, "Check ${r.name} failed", r.detail)
                        }
                    }
                    val summary = results.joinToString("; ") { "${it.name}=${if (it.pass) "PASS" else "FAIL"}" }
                    NotificationHelper.post(context, "Nightwatch: $command complete", summary)
                    SnapshotPusher.pushNow(context, results)
                }
            }

            "schedule" -> {
                val iso = data["arg"] ?: data["iso"] ?: data["time"]
                if (iso.isNullOrBlank()) {
                    store.log("COMMAND_SCHEDULE_FAILED", "no ISO8601 time supplied in command data")
                    NotificationHelper.postFailure(context, "schedule command failed", "no ISO8601 time in payload")
                    return
                }
                try {
                    val instant = java.time.Instant.parse(iso)
                    val triggerAt = instant.toEpochMilli()
                    AlarmScheduler.scheduleAbsolute(context, triggerAt)
                    store.log("COMMAND_SCHEDULE", "next alarm set for $iso (epoch $triggerAt)")
                    NotificationHelper.post(context, "Nightwatch: schedule", "Next alarm set for $iso")
                } catch (e: Exception) {
                    store.log("COMMAND_SCHEDULE_FAILED", "${e.javaClass.simpleName}: ${e.message}")
                    NotificationHelper.postFailure(context, "schedule command failed", "$iso is not valid ISO-8601: ${e.message}")
                }
            }

            "refresh" -> {
                // DESIGN §9.1: "the web app asks the Worker to fire a high-priority FCM push at
                // the phone... the phone wakes, pushes a fresh snapshot."
                store.log("COMMAND_REFRESH", "pushing fresh snapshot on demand")
                SnapshotPusher.pushNow(context)
                NotificationHelper.post(context, "Nightwatch: refresh", "Fresh snapshot pushed on demand")
            }

            "run_session" -> {
                // Redundant daily cron trigger (DESIGN §7.2/§9.1). This probe has no trading
                // session — stand in with the full check battery + snapshot push so the trigger
                // is still visibly proven end to end.
                store.log("COMMAND_RUN_SESSION", "redundant cron trigger received; running check battery")
                runBlocking {
                    val results = CheckBattery.runAll(context)
                    results.forEach { r -> store.log("CHECK_${r.name}", r.asEventDetail()) }
                    SnapshotPusher.pushNow(context, results)
                }
                NotificationHelper.post(context, "Nightwatch: run_session", "Redundant cron trigger handled")
            }

            "halt" -> {
                HaltFlag.set(context, true)
                store.log("COMMAND_HALT", "HALT flag set")
                NotificationHelper.post(context, "Nightwatch: HALT", "Halt flag is now SET")
            }

            "resume" -> {
                HaltFlag.set(context, false)
                store.log("COMMAND_RESUME", "HALT flag cleared")
                NotificationHelper.post(context, "Nightwatch: resume", "Halt flag is now CLEARED")
            }

            "help" -> {
                val detail = "supported: ping, status, logs, runchecks, selftest, schedule, " +
                    "refresh, halt, resume, help"
                store.log("COMMAND_HELP", detail)
                NotificationHelper.post(context, "Nightwatch: help", detail)
            }

            else -> {
                store.log("COMMAND_UNKNOWN", "command=$command data=$data")
                NotificationHelper.postFailure(context, "Unknown command", "command=$command")
            }
        }
    }
}

/** Minimal halt-flag stand-in for DESIGN §6.I / §5 gate 1 — this probe has nothing to halt yet,
 * but the flag is real, persisted, and visible, so the real app's preflight gate 1 has a proven
 * mechanism to build on. */
object HaltFlag {
    private const val PREFS = "nightwatch_probe_halt_prefs"
    private const val KEY_HALT = "halt"

    fun set(context: Context, halted: Boolean) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit().putBoolean(KEY_HALT, halted).apply()
    }

    fun isHalted(context: Context): Boolean =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getBoolean(KEY_HALT, false)
}
