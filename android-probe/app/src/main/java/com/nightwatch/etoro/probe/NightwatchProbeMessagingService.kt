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

/**
 * DESIGN §8.2 tier 1 (FCM high-priority push) and the demotion canary.
 *
 * getPriority() is logged on every message — this is the required design element that catches
 * Google's 7-day silent-high-priority demotion before it ever costs a live session. Posting a
 * VISIBLE notification for every message is what prevents that demotion in the first place.
 */
class NightwatchProbeMessagingService : FirebaseMessagingService() {

    private val scope = CoroutineScope(Dispatchers.IO + Job())
    private val http by lazy { OkHttpClient() }

    override fun onMessageReceived(message: RemoteMessage) {
        val store = EventStore.get(this)
        val priorityStr = priorityToString(message.priority)
        val origPriorityStr = priorityToString(message.originalPriority)

        // The demotion canary (task requirement 2 / DESIGN §8.2): log getPriority() every time.
        store.log(
            "FCM_RECEIVED",
            "priority=$priorityStr originalPriority=$origPriorityStr " +
                "data=${message.data} notification=${message.notification?.body}"
        )

        // Required to avoid the 7-day silent-high-priority demotion.
        NotificationHelper.post(
            this,
            "Nightwatch Probe: FCM received",
            "priority=$priorityStr data=${message.data}"
        )

        // Do the work inside onMessageReceived, inside the temporary network/wakelock grant
        // FCM high-priority delivery provides (DESIGN §8.2) — start WakeService for the network
        // check + Chaquopy smoke check + tzdata check, same as an alarm-triggered wake.
        val serviceIntent = Intent(this, WakeService::class.java)
            .putExtra(WakeService.EXTRA_TRIGGER, "fcm")
        ContextCompat.startForegroundService(this, serviceIntent)
    }

    override fun onNewToken(token: String) {
        val store = EventStore.get(this)
        store.log("FCM_NEW_TOKEN", token)
        TokenHolder.set(token)

        // Task requirement 2: POST the token to the Worker's /device-token endpoint so real
        // pushes can be sent. PHONE_PUSH_SECRET comes from BuildConfig, injected from
        // local.properties (gitignored) at build time — never hardcoded here.
        scope.launch {
            postTokenToWorker(token, store)
        }
    }

    private fun postTokenToWorker(token: String, store: EventStore) {
        try {
            val body = JSONObject().apply {
                put("token", token)
                put("platform", "android")
                put("app", "nightwatch-probe")
            }.toString().toRequestBody("application/json".toMediaType())

            val request = Request.Builder()
                .url("${BuildConfig.WORKER_BASE_URL}/device-token")
                .header("x-phone-push-secret", BuildConfig.PHONE_PUSH_SECRET)
                .post(body)
                .build()

            http.newCall(request).execute().use { response ->
                store.log(
                    "FCM_TOKEN_POST",
                    "status=${response.code} success=${response.isSuccessful} " +
                        "body=${response.body?.string()?.take(500)}"
                )
            }
        } catch (e: IOException) {
            store.log("FCM_TOKEN_POST_FAILED", "${e.javaClass.simpleName}: ${e.message}")
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
