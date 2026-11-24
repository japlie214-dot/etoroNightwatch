package com.nightwatch.etoro.probe

import android.content.Context
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

// No provider is installed here: trusted, restart-safe provisioning is a release blocker.
internal object RuntimeCredentials {
    enum class Key { PHONE_PUSH_SECRET, COMMAND_HMAC_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID }

    fun interface Provider {
        fun read(key: Key): String?
    }

    @Volatile
    var provider: Provider? = null

    fun value(key: Key): String? = try {
        provider?.read(key)?.takeUnless { it.isBlank() }
    } catch (_: Exception) {
        null
    }
}

/**
 * DESIGN §8.2: "HMAC-of-(command, counter), with replay rejection by counter." Mirrors
 * cloud/worker/src/commands.js's hmacCommand() exactly: HMAC-SHA256 over "<command>:<counter>",
 * hex-encoded, using the shared COMMAND_HMAC_SECRET from a trusted runtime provider.
 * A counter at or below the last-accepted value is rejected as a replay,
 * mirroring the Worker's own monotonic KV counter.
 */
object CommandVerifier {
    private const val PREFS = "nightwatch_probe_command_prefs"
    private const val KEY_LAST_ACCEPTED_COUNTER = "last_accepted_counter"

    sealed class Verdict {
        data class Accepted(val counter: Long) : Verdict()
        data class Rejected(val reason: String) : Verdict()
    }

    private fun hmacHex(secret: String, message: String): String {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(secret.toByteArray(Charsets.UTF_8), "HmacSHA256"))
        val sig = mac.doFinal(message.toByteArray(Charsets.UTF_8))
        return sig.joinToString("") { "%02x".format(it) }
    }

    /**
     * Verifies an inbound command carrying an explicit counter+hmac (the POST /command path).
     * Commands with no hmac/counter at all (the Worker's own /refresh and the redundant cron's
     * run_session push) are not put through this — they are already authenticated at the Worker
     * by WEB_READ_TOKEN / are the Worker's own scheduled trigger, per DESIGN §8.2 / §9.4.
     */
    @Synchronized
    fun verify(
        context: Context,
        command: String,
        counterStr: String?,
        hmacHex: String?,
        arg: String? = null,
    ): Verdict {
        val secret = RuntimeCredentials.value(RuntimeCredentials.Key.COMMAND_HMAC_SECRET)
            ?: return Verdict.Rejected("command authentication unavailable: runtime provisioning required")
        val counter = counterStr?.toLongOrNull()
            ?: return Verdict.Rejected("missing or non-numeric counter")
        if (hmacHex.isNullOrBlank()) {
            return Verdict.Rejected("missing hmac")
        }
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val lastAccepted = prefs.getLong(KEY_LAST_ACCEPTED_COUNTER, -1L)
        if (counter <= lastAccepted) {
            return Verdict.Rejected("replay: counter=$counter <= last_accepted=$lastAccepted")
        }
        // Mirrors cloud/worker/src/commands.js hmacCommand() exactly: when an argument is
        // present (e.g. schedule's ISO-8601 time) it is folded INTO the signed message, so a
        // tampered arg fails verification even if the command name and counter are untouched.
        val message = if (!arg.isNullOrEmpty()) "$command:$counter:$arg" else "$command:$counter"
        val expected = hmacHex(secret, message)
        // Constant-time-ish compare (defense in depth; this is a local check, not a network
        // timing-attack surface, but there is no reason to compare the easy way).
        var diff = 0
        val a = expected.toByteArray(Charsets.UTF_8)
        val b = hmacHex.lowercase().toByteArray(Charsets.UTF_8)
        if (a.size != b.size) {
            return Verdict.Rejected("hmac mismatch (length)")
        }
        for (i in a.indices) diff = diff or (a[i].toInt() xor b[i].toInt())
        if (diff != 0) {
            return Verdict.Rejected("hmac mismatch")
        }
        prefs.edit().putLong(KEY_LAST_ACCEPTED_COUNTER, counter).apply()
        return Verdict.Accepted(counter)
    }
}
