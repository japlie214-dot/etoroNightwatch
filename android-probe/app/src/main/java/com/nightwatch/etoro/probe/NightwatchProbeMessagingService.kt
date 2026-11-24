package com.nightwatch.etoro.probe

import android.content.Intent
import androidx.core.content.ContextCompat
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.security.MessageDigest

/**
 * DESIGN §8.2 tier 1 (FCM high-priority push), the demotion canary, and — v2 — the full inbound
 * remote-command loop (task Problem 3, the headline feature).
 *
 * **Design constraint, load-bearing, do not reintroduce a `notification` block:** the Worker
 * (`cloud/worker/src/fcm.js`) sends DATA-ONLY FCM messages on purpose. A message that carries
 * both `notification` and `data` is rendered by the FCM SDK itself while the app is backgrounded,
 * and `onMessageReceived()` is **never invoked** in that case — confirmed empirically on the real
 * device (three pushes displayed as system-tray notifications, zero `FCM_MESSAGE_RECEIVED` log
 * entries). Because of this, `title`/`body` travel inside `data` here, `remoteMessage
 * .notification` is always null and is never read, and this handler builds the visible
 * notification itself from `data` on every call — this is what makes onMessageReceived fire
 * reliably (foreground and background) AND what prevents the 7-day silent-send demotion.
 *
 * getPriority() is logged AND surfaced loudly in the UI on every message — the demotion canary
 * that catches Google's 7-day silent-high-priority demotion before it ever costs a live session.
 *
 * Every inbound command's `data` payload is one of:
 *  - {command, counter, hmac, title?, body?}  — from the Worker's POST /command (HMAC-verified,
 *                                 replay-checked)
 *  - {command: "refresh", title?, body?} — from the Worker's POST /refresh (already gated by
 *                                 WEB_READ_TOKEN at the Worker; no per-message hmac by design,
 *                                 DESIGN §9.1/§9.4)
 *  - {command: "run_session", trading_date, title?, body?} — the redundant daily cron trigger
 *                                 (DESIGN §7.2/§9.1)
 */
class NightwatchProbeMessagingService : FirebaseMessagingService() {

    companion object {
        internal fun messageDetail(data: Map<String, String>): String =
            "type=data data_keys=${data.keys.sorted()}"

        internal fun tokenDetail(token: String): String {
            val hash = MessageDigest.getInstance("SHA-256").digest(token.toByteArray(Charsets.UTF_8))
                .take(4).joinToString("") { "%02x".format(it.toInt() and 0xff) }
            return "length=${token.length} sha256=$hash"
        }
    }

    private val scope = CoroutineScope(Dispatchers.IO + Job())
    private val http by lazy { OkHttpClient() }

    override fun onMessageReceived(message: RemoteMessage) {
        val store = EventStore.get(this)
        val priorityStr = priorityToString(message.priority)
        val origPriorityStr = priorityToString(message.originalPriority)
        FcmPriorityHolder.lastPriority = priorityStr

        // IMPORTANT — data-only messages by design (coordinated with the Worker side,
        // cloud/worker/src/fcm.js): the Worker sends NO `notification` block at all. Per
        // Firebase's documented behaviour, a message carrying BOTH `notification` and `data`
        // is rendered by the FCM SDK itself while the app is backgrounded, and
        // onMessageReceived() is NEVER invoked in that case — this was confirmed empirically on
        // the real device (three pushes displayed as system-tray notifications, zero
        // FCM_MESSAGE_RECEIVED log entries, because the handler never ran). `remoteMessage
        // .notification` is therefore ALWAYS null here by construction — never read it. Title
        // and body travel inside `data` instead, and this handler builds the visible
        // notification itself so it always fires, foreground or background.
        val command = message.data["command"]
        val title = message.data["title"] ?: "Nightwatch Probe"
        val body = message.data["body"] ?: "Data message received"

        // The unmistakable event this bug exposed the absence of: log it BEFORE any dispatch
        // logic, unconditionally, every single time onMessageReceived actually runs.
        store.log(
            "FCM_MESSAGE_RECEIVED",
            "priority=$priorityStr originalPriority=$origPriorityStr ${messageDetail(message.data)}"
        )

        if (priorityStr != "PRIORITY_HIGH") {
            NotificationHelper.postFailure(
                this,
                "FCM priority demoted",
                "getPriority()=$priorityStr — Google may be demoting our high-priority privilege " +
                    "after silent sends (DESIGN §8.2). Escalation-path logic should trigger."
            )
        }

        // The app posts its own notification, built from `data`, not from a `notification`
        // block (which the Worker never sends) — required both to avoid the 7-day
        // silent-high-priority demotion and to give the user visible confirmation.
        NotificationHelper.post(this, title, body)

        if (command != null) {
            dispatchCommand(store, command, message.data)
        } else {
            // No recognized command — still run the standard wake-check battery so an
            // unstructured/legacy push still proves the wake path end to end.
            val serviceIntent = Intent(this, WakeService::class.java)
                .putExtra(WakeService.EXTRA_TRIGGER, "fcm")
            ContextCompat.startForegroundService(this, serviceIntent)
        }
    }

    private fun dispatchCommand(store: EventStore, command: String, data: Map<String, String>) {
        val counter = data["counter"]
        val hmac = data["hmac"]
        val arg = data["arg"]
        // "refresh" (POST /refresh) and "run_session" (the redundant cron) are not per-message
        // HMAC'd by the Worker — they're already gated by WEB_READ_TOKEN / are the Worker's own
        // scheduled trigger (DESIGN §9.1/§9.4). Everything else must carry a valid, non-replayed
        // HMAC or it is rejected and audited, never silently ignored.
        if (command == "refresh" || command == "run_session") {
            store.log("COMMAND_RECEIVED", "${messageDetail(data)} channel=fcm verdict=accepted (unauthenticated-by-design)")
            CommandHandler.handle(this, command, data)
            return
        }

        if (command == "demo_control") {
            // Its own HMAC (over the canonical envelope, not a counter) is checked in the handler.
            store.log("DEMO_CONTROL_RECEIVED", messageDetail(data))
            scope.launch {
                val outcome = try {
                    DemoControlHandler.handle(data, AndroidDemoControlDeps(this@NightwatchProbeMessagingService))
                } catch (e: Exception) {
                    DemoControlOutcome.Unreceipted("error=${e.javaClass.simpleName}")
                }
                store.log("DEMO_CONTROL_OUTCOME", outcome.label)
            }
            return
        }

        val verdict = CommandVerifier.verify(this, command, counter, hmac, arg)
        when (verdict) {
            is CommandVerifier.Verdict.Accepted -> {
                store.log(
                    "COMMAND_RECEIVED",
                    "${messageDetail(data)} channel=fcm verdict=accepted"
                )
                CommandHandler.handle(this, command, data)
            }
            is CommandVerifier.Verdict.Rejected -> {
                store.log(
                    "COMMAND_REJECTED",
                    "${messageDetail(data)} channel=fcm verdict=rejected"
                )
                NotificationHelper.postFailure(
                    this,
                    "Command rejected",
                    "command=$command reason=${verdict.reason}"
                )
            }
        }
    }

    override fun onNewToken(token: String) {
        val store = EventStore.get(this)
        store.log("FCM_NEW_TOKEN", tokenDetail(token))
        TokenHolder.set(token)

        // POST the token to the Worker's /device-token endpoint so real pushes can be sent.
        // Registration requires a trusted runtime credential provider.
        scope.launch {
            postTokenToWorker(token, store)
        }
    }

    private fun postTokenToWorker(token: String, store: EventStore) {
        val secret = RuntimeCredentials.value(RuntimeCredentials.Key.PHONE_PUSH_SECRET)
        if (secret == null) {
            store.log("FCM_TOKEN_POST_FAILED", "registration authentication unavailable: runtime provisioning required")
            return
        }
        try {
            val body = JSONObject().apply {
                put("token", token)
                put("platform", "android")
                put("app", "nightwatch-probe")
            }.toString().toRequestBody("application/json".toMediaType())

            val request = Request.Builder()
                .url("${BuildConfig.WORKER_BASE_URL}/device-token")
                .header("x-phone-push-secret", secret)
                .post(body)
                .build()

            http.newCall(request).execute().use { response ->
                store.log(
                    "FCM_TOKEN_POST",
                    "status=${response.code} success=${response.isSuccessful}"
                )
            }
        } catch (e: IOException) {
            store.log("FCM_TOKEN_POST_FAILED", "error=${e.javaClass.simpleName}")
        }
    }

    private fun priorityToString(p: Int): String = when (p) {
        RemoteMessage.PRIORITY_HIGH -> "PRIORITY_HIGH"
        RemoteMessage.PRIORITY_NORMAL -> "PRIORITY_NORMAL"
        RemoteMessage.PRIORITY_UNKNOWN -> "PRIORITY_UNKNOWN"
        else -> "UNKNOWN($p)"
    }
}

/** Simple in-memory holder so MainActivity can show the last-seen token without re-fetching. */
object TokenHolder {
    @Volatile
    var lastToken: String? = null
        private set

    fun set(token: String) {
        lastToken = token
    }
}

/** Task requirement 9: make FCM priority demotion loudly visible in the UI, not just a log line. */
object FcmPriorityHolder {
    @Volatile
    var lastPriority: String? = null
}
