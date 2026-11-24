package com.nightwatch.etoro.probe

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import org.json.JSONObject
import java.io.File
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * Runtime credential provider backed by an AES-256-GCM key that lives in the Android Keystore
 * (non-exportable). Values are provisioned once, off-build: `tools/provision_phone_secrets.py`
 * streams a JSON file into this app's private `files/provision.json` through `adb run-as`
 * (never as a command-line argument). [importPending] encrypts it, verifies the round trip,
 * then overwrites and deletes the plaintext. Nothing here logs a value.
 */
internal object KeystoreCredentials {
    private const val ALIAS = "nightwatch_secrets_v1"
    private const val PREFS = "nightwatch_probe_secrets"
    const val PENDING_FILE = "provision.json"

    /** Pure and unit-tested: only the four known keys, blank/non-string values dropped. */
    fun parseProvision(json: String): Map<RuntimeCredentials.Key, String> {
        val o = JSONObject(json)
        return RuntimeCredentials.Key.entries.mapNotNull { k ->
            (o.opt(k.name) as? String)?.trim()?.takeIf { it.isNotEmpty() && it.length <= 4096 }?.let { k to it }
        }.toMap()
    }

    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").apply {
            init(KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setKeySize(256)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setRandomizedEncryptionRequired(true)
                .build())
        }.generateKey()
    }

    private fun encrypt(plain: String, name: String): String {
        val c = Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.ENCRYPT_MODE, key()) }
        c.updateAAD(name.toByteArray(Charsets.UTF_8)) // binds the ciphertext to its key name
        val ct = c.doFinal(plain.toByteArray(Charsets.UTF_8))
        return Base64.encodeToString(c.iv, Base64.NO_WRAP) + ":" + Base64.encodeToString(ct, Base64.NO_WRAP)
    }

    private fun decrypt(blob: String, name: String): String? = try {
        val (iv, ct) = blob.split(":", limit = 2).map { Base64.decode(it, Base64.NO_WRAP) }
        val c = Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, iv)) }
        c.updateAAD(name.toByteArray(Charsets.UTF_8))
        String(c.doFinal(ct), Charsets.UTF_8)
    } catch (_: Exception) {
        null
    }

    /** Installs the provider and imports any pending file. Safe to call on every process start. */
    fun install(context: Context) {
        val app = context.applicationContext
        val prefs = app.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        RuntimeCredentials.provider = RuntimeCredentials.Provider { k ->
            prefs.getString(k.name, null)?.let { decrypt(it, k.name) }
        }
        importPending(app)
    }

    private fun importPending(app: Context) {
        val file = File(app.filesDir, PENDING_FILE)
        if (!file.isFile) return
        val log = EventStore.get(app)
        try {
            val values = parseProvision(file.readText(Charsets.UTF_8))
            require(values.isNotEmpty()) { "no known keys" }
            val prefs = app.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            val edit = prefs.edit()
            values.forEach { (k, v) -> edit.putString(k.name, encrypt(v, k.name)) }
            check(edit.commit()) { "commit failed" }
            for ((k, v) in values) check(RuntimeCredentials.value(k) == v) { "round trip failed" }
            log.log("CREDENTIALS_PROVISIONED", "keys=${values.keys.map { it.name }.sorted()}")
            // Best effort wipe of the plaintext, then delete.
            file.writeBytes(ByteArray(file.length().toInt().coerceAtLeast(1)))
            file.delete()
        } catch (e: Exception) {
            log.log("CREDENTIALS_PROVISION_FAILED", "error=${e.javaClass.simpleName}; plaintext file left for retry")
        }
    }
}
