package com.nightwatch.etoro.probe

import org.json.JSONObject
import java.security.MessageDigest
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * Device side of the demo-only control protocol (cloud/worker/README.md "T083").
 *
 * Exactly two actions exist: refresh_demo_broker and pause_demo_orders. There is no resume,
 * order, cancel or close path here. Authority comes only from the dispatch HMAC checked in
 * [DemoControlHandler]; nothing in the Python response grants or widens it.
 *
 * Pure JVM (no Android types) so the whole decision chain is unit-testable.
 */
internal object DemoControl {
    val ACTIONS = setOf("refresh_demo_broker", "pause_demo_orders")
    const val DEADLINE_MS = 120_000L
    private const val SAFE_MAX = 9_007_199_254_740_991L
    private val UUID = Regex("^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    private val TOKEN = Regex("^[A-Za-z0-9._:-]{1,128}$")
    private val HEX = Regex("^[0-9a-f]{64}$")

    data class Envelope(
        val requestId: String, val action: String, val deviceId: String,
        val issuedAt: Long, val deadlineAt: Long,
    )

    private fun timeOk(v: Long) = v in 0..(SAFE_MAX - DEADLINE_MS)

    /** Strict: exact field set, version 1, demo only, deadline = issue + 120000. */
    fun parseEnvelope(json: String?): Envelope? {
        if (json == null || json.length > 2048) return null
        return try {
            val o = JSONObject(json)
            val keys = o.keys().asSequence().toSet()
            if (keys != setOf("version", "requestId", "action", "deviceId", "environment", "issuedAt", "deadlineAt")) return null
            val issued = o.get("issuedAt") as? Number ?: return null
            val deadline = o.get("deadlineAt") as? Number ?: return null
            if (issued !is Int && issued !is Long || deadline !is Int && deadline !is Long) return null
            val version = o.get("version")
            val id = o.get("requestId") as? String ?: return null
            val action = o.get("action") as? String ?: return null
            val device = o.get("deviceId") as? String ?: return null
            if ((version as? Number)?.toLong() != 1L || o.get("environment") != "demo") return null
            if (!UUID.matches(id) || action !in ACTIONS || !TOKEN.matches(device)) return null
            val i = issued.toLong()
            val d = deadline.toLong()
            if (!timeOk(i) || d != i + DEADLINE_MS) return null
            Envelope(id, action, device, i, d)
        } catch (_: Exception) {
            null
        }
    }

    /** Domain-separated canonical lines the dispatch service must HMAC-SHA256 (hex, lowercase). */
    fun dispatchSigningBytes(e: Envelope): ByteArray = listOf(
        "nightwatch.demo-control.dispatch.v1", "version=1", "requestId=${e.requestId}",
        "action=${e.action}", "deviceId=${e.deviceId}", "environment=demo",
        "issuedAt=${e.issuedAt}", "deadlineAt=${e.deadlineAt}",
    ).joinToString("\n", postfix = "\n").toByteArray(Charsets.UTF_8)

    fun dispatchHmacHex(secret: String, e: Envelope): String {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(secret.toByteArray(Charsets.UTF_8), "HmacSHA256"))
        return mac.doFinal(dispatchSigningBytes(e)).joinToString("") { "%02x".format(it) }
    }

    fun dispatchAuthentic(secret: String, e: Envelope, hmacHex: String?): Boolean {
        if (hmacHex == null || !HEX.matches(hmacHex)) return false
        return MessageDigest.isEqual(
            dispatchHmacHex(secret, e).toByteArray(Charsets.UTF_8), hmacHex.toByteArray(Charsets.UTF_8))
    }

    fun requestJson(e: Envelope): String =
        """{"requestId":"${e.requestId}","action":"${e.action}","deviceId":"${e.deviceId}",""" +
            """"environment":"demo","issuedAt":${e.issuedAt},"deadlineAt":${e.deadlineAt}}"""

    /** What the Python entry returned. Anything unparseable is [status] "unconfirmed". */
    data class PyResult(
        val status: String, val requestId: String?, val action: String?, val deviceId: String?,
        val executedAt: Long?, val pausePersisted: Boolean?, val finalSubmissionGuard: Boolean?,
        val account: Boolean?, val positions: Boolean?, val openOrders: Boolean?, val observedAt: Long?,
        val reasonCode: String?,
    )

    private fun JSONObject.longOrNull(k: String): Long? =
        (opt(k) as? Number)?.takeIf { it is Int || it is Long }?.toLong()

    private fun JSONObject.boolOrNull(k: String): Boolean? = opt(k) as? Boolean

    fun parseResult(json: String?): PyResult {
        val bad = PyResult("unconfirmed", null, null, null, null, null, null, null, null, null, null, "bad_response")
        if (json == null) return bad
        return try {
            val o = JSONObject(json)
            val obs = o.optJSONObject("brokerObservation")
            PyResult(
                status = o.optString("status", "unconfirmed"),
                requestId = o.optString("requestId").takeIf { it.isNotEmpty() },
                action = o.optString("action").takeIf { it.isNotEmpty() },
                deviceId = o.optString("deviceId").takeIf { it.isNotEmpty() },
                executedAt = o.longOrNull("executedAt"),
                pausePersisted = o.boolOrNull("pausePersisted"),
                finalSubmissionGuard = o.boolOrNull("finalSubmissionGuard"),
                account = obs?.boolOrNull("account"), positions = obs?.boolOrNull("positions"),
                openOrders = obs?.boolOrNull("openOrders"), observedAt = obs?.longOrNull("observedAt"),
                reasonCode = o.optString("reasonCode").takeIf { it.isNotEmpty() },
            )
        } catch (_: Exception) {
            bad
        }
    }

    /** Signed evidence, already proven eligible by [signableReceipt]. */
    data class Receipt(
        val keyId: String, val envelope: Envelope, val executedAt: Long,
        val pausePersisted: Boolean = false, val finalSubmissionGuard: Boolean = false,
        val observedAt: Long = 0, val account: Boolean = false, val positions: Boolean = false,
        val openOrders: Boolean = false,
    ) {
        val refresh get() = envelope.action == "refresh_demo_broker"
    }

    /**
     * The ONLY way a receipt comes into existence. Returns null (nothing to sign) unless the
     * Python result is "applied", echoes this exact request, and carries complete evidence.
     * A "partial" refresh -- which is all Python can currently return, since open-order coverage
     * is unproved and observedAt is null -- is therefore never signed as complete.
     */
    fun signableReceipt(keyId: String, e: Envelope, r: PyResult): Receipt? {
        if (!TOKEN.matches(keyId) || r.status != "applied") return null
        if (r.requestId != e.requestId || r.action != e.action || r.deviceId != e.deviceId) return null
        val executed = r.executedAt ?: return null
        if (!timeOk(executed) || executed < e.issuedAt || executed >= e.deadlineAt) return null
        if (e.action == "pause_demo_orders") {
            if (r.pausePersisted != true || r.finalSubmissionGuard != true) return null
            return Receipt(keyId, e, executed, pausePersisted = true, finalSubmissionGuard = true)
        }
        val observed = r.observedAt ?: return null
        if (r.account != true || r.positions != true || r.openOrders != true) return null
        if (!timeOk(observed) || observed <= e.issuedAt || observed > executed) return null
        return Receipt(keyId, e, executed, observedAt = observed, account = true, positions = true, openOrders = true)
    }

    /** Byte-for-byte the Worker's deviceReceiptSigningBytes: fixed order, LF after every line. */
    fun receiptSigningBytes(r: Receipt): ByteArray {
        val e = r.envelope
        val lines = mutableListOf(
            "nightwatch.demo-control.receipt.v1", "alg=RS256", "keyId=${r.keyId}",
            "requestId=${e.requestId}", "action=${e.action}", "deviceId=${e.deviceId}",
            "environment=demo", "issuedAt=${e.issuedAt}", "deadlineAt=${e.deadlineAt}",
            "executedAt=${r.executedAt}",
        )
        if (r.refresh) {
            lines += listOf("observedAt=${r.observedAt}", "account=${r.account}",
                "positions=${r.positions}", "openOrders=${r.openOrders}")
        } else {
            lines += listOf("pausePersisted=${r.pausePersisted}", "finalSubmissionGuard=${r.finalSubmissionGuard}")
        }
        return lines.joinToString("\n", postfix = "\n").toByteArray(Charsets.UTF_8)
    }

    /** Unpadded base64url; java.util.Base64 needs API 26 and minSdk is 24. */
    internal fun base64Url(bytes: ByteArray): String {
        val alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        val out = StringBuilder()
        var i = 0
        while (i < bytes.size) {
            val b0 = bytes[i].toInt() and 0xff
            val b1 = if (i + 1 < bytes.size) bytes[i + 1].toInt() and 0xff else -1
            val b2 = if (i + 2 < bytes.size) bytes[i + 2].toInt() and 0xff else -1
            out.append(alphabet[b0 shr 2])
            out.append(alphabet[((b0 and 3) shl 4) or (if (b1 < 0) 0 else b1 shr 4)])
            if (b1 >= 0) out.append(alphabet[((b1 and 15) shl 2) or (if (b2 < 0) 0 else b2 shr 6)])
            if (b2 >= 0) out.append(alphabet[b2 and 63])
            i += 3
        }
        return out.toString()
    }

    /** Compact hand-built JSON: every value is regex-validated, so no escaping can occur. */
    fun receiptBody(r: Receipt, signature: ByteArray): String {
        val e = r.envelope
        val sig = base64Url(signature)
        val evidence = if (r.refresh) {
            """"brokerObservation":{"observedAt":${r.observedAt},"account":${r.account},"positions":${r.positions},"openOrders":${r.openOrders}}"""
        } else {
            """"pausePersisted":${r.pausePersisted},"finalSubmissionGuard":${r.finalSubmissionGuard}"""
        }
        return """{"version":1,"keyId":"${r.keyId}","requestId":"${e.requestId}","action":"${e.action}",""" +
            """"deviceId":"${e.deviceId}","environment":"demo","issuedAt":${e.issuedAt},""" +
            """"deadlineAt":${e.deadlineAt},"executedAt":${r.executedAt},$evidence,"signature":"$sig"}"""
    }
}

/** RSASSA-PKCS1-v1_5 / SHA-256 signer; the private key never leaves its provider. */
internal interface ReceiptSigner {
    val keyId: String
    fun sign(bytes: ByteArray): ByteArray
}

internal sealed class DemoControlOutcome(val label: String) {
    object Unavailable : DemoControlOutcome("unavailable")
    object Rejected : DemoControlOutcome("rejected")
    object Expired : DemoControlOutcome("expired")
    /** Python ran but did not apply; only fixed-vocabulary status/reason are ever logged. */
    class NotApplied(status: String, reason: String?) : DemoControlOutcome("not_applied:$status${reason?.let { "/$it" } ?: ""}")
    /** Applied or partial on the device, but no verifiable receipt was produced or delivered. */
    class Unreceipted(why: String) : DemoControlOutcome("unreceipted:$why")
    class Receipted(val httpStatus: Int) : DemoControlOutcome("receipted:$httpStatus")
}

/** Dependencies injected so tests never touch Android, Python, the network or a real key. */
internal interface DemoControlDeps {
    fun dispatchSecret(): String?
    fun deviceId(): String?
    fun signer(): ReceiptSigner?
    fun nowMs(): Long
    /** Runs Python with write authority already verified; null means the call itself failed. */
    fun apply(requestJson: String, deviceId: String): String?
    /** POSTs a signed receipt; returns the HTTP status or null when undeliverable. */
    fun post(body: String): Int?
}

private fun safeToken(v: String): String = if (Regex("^[a-z_]{1,40}$").matches(v)) v else "other"

internal object DemoControlHandler {
    fun handle(data: Map<String, String>, deps: DemoControlDeps): DemoControlOutcome {
        val secret = deps.dispatchSecret() ?: return DemoControlOutcome.Unavailable
        val deviceId = deps.deviceId() ?: return DemoControlOutcome.Unavailable
        val envelope = DemoControl.parseEnvelope(data["envelope"]) ?: return DemoControlOutcome.Rejected
        if (!DemoControl.dispatchAuthentic(secret, envelope, data["hmac"])) return DemoControlOutcome.Rejected
        if (envelope.deviceId != deviceId) return DemoControlOutcome.Rejected
        val now = deps.nowMs()
        if (now >= envelope.deadlineAt) return DemoControlOutcome.Expired

        val result = DemoControl.parseResult(deps.apply(DemoControl.requestJson(envelope), deviceId))
        // Python is the authority on duplicate/pause/read; refused, partial and unconfirmed are
        // all deliberately receipt-less, so the cloud keeps showing Requested, never Applied.
        if (result.status != "applied") return DemoControlOutcome.NotApplied(safeToken(result.status), result.reasonCode?.let(::safeToken))
        val signer = deps.signer() ?: return DemoControlOutcome.Unreceipted("signer_unavailable")
        val receipt = DemoControl.signableReceipt(signer.keyId, envelope, result)
            ?: return DemoControlOutcome.Unreceipted("evidence_incomplete")
        val signature = try {
            signer.sign(DemoControl.receiptSigningBytes(receipt))
        } catch (_: Exception) {
            return DemoControlOutcome.Unreceipted("sign_failed")
        }
        val status = try {
            deps.post(DemoControl.receiptBody(receipt, signature))
        } catch (_: Exception) {
            null
        } ?: return DemoControlOutcome.Unreceipted("post_failed")
        return DemoControlOutcome.Receipted(status)
    }
}
