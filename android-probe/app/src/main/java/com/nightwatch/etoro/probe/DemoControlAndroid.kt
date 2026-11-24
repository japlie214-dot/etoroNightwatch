package com.nightwatch.etoro.probe

import android.content.Context
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.File
import java.security.PrivateKey
import java.security.Signature

/**
 * Trusted, restart-safe provisioning of the device identity and receipt key is a separate,
 * unfinished release step. Until [identity] is set nothing here signs or acknowledges anything,
 * and the dispatch secret comes from the same [RuntimeCredentials] provider as the other secrets.
 */
internal object DemoControlIdentity {
    class Identity(val deviceId: String, val signer: ReceiptSigner)

    @Volatile
    var identity: Identity? = null
}

/** Wraps a provider-held RSA private key (e.g. Android Keystore); never reads key bytes. */
internal class RsaReceiptSigner(override val keyId: String, private val key: PrivateKey) : ReceiptSigner {
    override fun sign(bytes: ByteArray): ByteArray = Signature.getInstance("SHA256withRSA").run {
        initSign(key)
        update(bytes)
        sign()
    }
}

internal class AndroidDemoControlDeps(private val context: Context) : DemoControlDeps {
    private val http = OkHttpClient()
    override fun dispatchSecret() = RuntimeCredentials.value(RuntimeCredentials.Key.COMMAND_HMAC_SECRET)
    override fun deviceId() = DemoControlIdentity.identity?.deviceId
    override fun signer() = DemoControlIdentity.identity?.signer
    override fun nowMs() = System.currentTimeMillis()

    override fun apply(requestJson: String, deviceId: String): String? {
        val home = File(context.filesDir, "nightwatch_demo")
        return PyBridge.applyDemoControl(context, home.absolutePath, requestJson, deviceId)
    }

    override fun post(body: String): Int? = try {
        val request = Request.Builder().url("${BuildConfig.WORKER_BASE_URL}/demo-control/ack")
            .post(body.toRequestBody("application/json".toMediaType())).build()
        http.newCall(request).execute().use { it.code }
    } catch (_: java.io.IOException) {
        null
    }
}
