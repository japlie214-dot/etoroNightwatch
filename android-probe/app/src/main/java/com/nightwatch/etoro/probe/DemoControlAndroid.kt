package com.nightwatch.etoro.probe

import android.content.Context
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.File
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.security.KeyPairGenerator
import java.security.KeyStore
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

    internal const val DEVICE_ID = "z10-demo"
    internal const val KEY_ID = "z10-receipt-v1"
    private const val ALIAS = "nightwatch_demo_receipt_v1"

    /** Hardware-backed RSA-2048 PKCS#1 v1.5 key; the private half is never exportable. */
    @Synchronized
    fun keystoreSigner(): ReceiptSigner? = try {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        if (!store.containsAlias(ALIAS)) {
            KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_RSA, "AndroidKeyStore").apply {
                initialize(KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_SIGN)
                    .setKeySize(2048)
                    .setDigests(KeyProperties.DIGEST_SHA256)
                    .setSignaturePaddings(KeyProperties.SIGNATURE_PADDING_RSA_PKCS1)
                    .build())
            }.generateKeyPair()
        }
        RsaReceiptSigner(KEY_ID, store.getKey(ALIAS, null) as PrivateKey)
    } catch (_: Exception) {
        null
    }

    /** Public SPKI, base64: the value to pin on the Worker. Public, safe to log. */
    fun publicKeySpkiBase64(): String? = try {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        keystoreSigner()?.let {
            android.util.Base64.encodeToString(store.getCertificate(ALIAS).publicKey.encoded, android.util.Base64.NO_WRAP)
        }
    } catch (_: Exception) {
        null
    }
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
    override fun deviceId() = DemoControlIdentity.identity?.deviceId ?: DemoControlIdentity.DEVICE_ID
    override fun signer() = DemoControlIdentity.identity?.signer ?: DemoControlIdentity.keystoreSigner()
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
