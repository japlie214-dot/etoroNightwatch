package com.nightwatch.etoro.probe

import org.junit.Assert.*
import org.junit.Test
import java.security.KeyPairGenerator
import java.security.Signature

class DemoControlTest {
    private val id = "12345678-1234-4234-8234-123456789abc"
    private val secret = "synthetic-only"
    private val keyPair = KeyPairGenerator.getInstance("RSA").apply { initialize(2048) }.generateKeyPair()

    private fun envelopeJson(action: String, issued: Long = 1_000_000, device: String = "synthetic-device") =
        """{"version":1,"requestId":"$id","action":"$action","deviceId":"$device","environment":"demo",""" +
            """"issuedAt":$issued,"deadlineAt":${issued + 120_000}}"""

    private fun signed(action: String): Map<String, String> {
        val json = envelopeJson(action)
        val env = DemoControl.parseEnvelope(json)!!
        return mapOf("command" to "demo_control", "envelope" to json,
            "hmac" to DemoControl.dispatchHmacHex(secret, env))
    }

    private class Deps(
        var response: String?, var now: Long = 1_001_000, var secret: String? = "synthetic-only",
        var device: String? = "synthetic-device", var signer: ReceiptSigner? = null, var postStatus: Int? = 200,
    ) : DemoControlDeps {
        var applied = 0
        var posted = mutableListOf<String>()
        override fun dispatchSecret() = secret
        override fun deviceId() = device
        override fun signer() = signer
        override fun nowMs() = now
        override fun apply(requestJson: String, deviceId: String): String? { applied++; return response }
        override fun post(body: String): Int? { posted += body; return postStatus }
    }

    private fun signer(kp: java.security.KeyPair = keyPair) = RsaReceiptSigner("synthetic-pin-v1", kp.private)

    private fun pauseApplied(executedAt: Long = 1_001_500) =
        """{"requestId":"$id","action":"pause_demo_orders","deviceId":"synthetic-device","environment":"demo",""" +
            """"pausePersisted":true,"finalSubmissionGuard":true,"executedAt":$executedAt,"status":"applied"}"""

    // Exactly the shape handle_demo_control returns for refresh today.
    private val refreshPartial =
        """{"requestId":"$id","action":"refresh_demo_broker","deviceId":"synthetic-device","environment":"demo",""" +
            """"status":"partial","executedAt":1001500,"brokerObservation":{"account":true,"positions":true,""" +
            """"openOrders":false,"observedAt":null},"portfolioArrays":{},"portfolioRead":{"status":"read"},""" +
            """"reasonCode":"open_orders_unestablished"}"""

    @Test
    fun pauseAppliedProducesAVerifiableReceiptMatchingTheWorkerBytes() {
        val deps = Deps(pauseApplied(), signer = signer())
        val outcome = DemoControlHandler.handle(signed("pause_demo_orders"), deps)
        assertEquals("receipted:200", outcome.label)
        assertEquals(1, deps.posted.size)
        val body = deps.posted.single()
        val expectedText = "nightwatch.demo-control.receipt.v1\nalg=RS256\nkeyId=synthetic-pin-v1\n" +
            "requestId=$id\naction=pause_demo_orders\ndeviceId=synthetic-device\nenvironment=demo\n" +
            "issuedAt=1000000\ndeadlineAt=1120000\nexecutedAt=1001500\npausePersisted=true\n" +
            "finalSubmissionGuard=true\n"
        val sigB64 = Regex("\"signature\":\"([A-Za-z0-9_-]+)\"").find(body)!!.groupValues[1]
        assertEquals(342, sigB64.length) // 256 bytes, unpadded
        val sigBytes = java.util.Base64.getUrlDecoder().decode(sigB64)
        val verifier = Signature.getInstance("SHA256withRSA").apply {
            initVerify(keyPair.public); update(expectedText.toByteArray(Charsets.UTF_8))
        }
        assertTrue(verifier.verify(sigBytes))
        assertFalse(body.contains(" "))
        assertFalse(body.contains("brokerObservation"))
    }

    @Test
    fun partialRefreshIsNeverSignedOrPosted() {
        val deps = Deps(refreshPartial, signer = signer())
        val outcome = DemoControlHandler.handle(signed("refresh_demo_broker"), deps)
        assertEquals("not_applied:partial/open_orders_unestablished", outcome.label)
        assertEquals(1, deps.applied)
        assertTrue(deps.posted.isEmpty())
    }

    @Test
    fun evenAnApplyingStatusWithIncompleteEvidenceIsNotSigned() {
        val env = DemoControl.parseEnvelope(envelopeJson("refresh_demo_broker"))!!
        fun refresh(open: String, observed: String) = DemoControl.parseResult(
            """{"requestId":"$id","action":"refresh_demo_broker","deviceId":"synthetic-device","status":"applied",""" +
                """"executedAt":1001500,"brokerObservation":{"account":true,"positions":true,"openOrders":$open,"observedAt":$observed}}""")
        assertNull(DemoControl.signableReceipt("k", env, refresh("false", "1000600")))
        assertNull(DemoControl.signableReceipt("k", env, refresh("true", "null")))
        assertNull(DemoControl.signableReceipt("k", env, refresh("true", "1000000"))) // not strictly post-issue
        assertNull(DemoControl.signableReceipt("k", env, refresh("true", "1001501"))) // after execution
        assertNotNull(DemoControl.signableReceipt("k", env, refresh("true", "1000600")))
    }

    @Test
    fun pauseEvidenceMustBeBothTrueAndOnTime() {
        val env = DemoControl.parseEnvelope(envelopeJson("pause_demo_orders"))!!
        fun r(p: String, g: String, t: Long) = DemoControl.parseResult(
            """{"requestId":"$id","action":"pause_demo_orders","deviceId":"synthetic-device","status":"applied",""" +
                """"pausePersisted":$p,"finalSubmissionGuard":$g,"executedAt":$t}""")
        assertNull(DemoControl.signableReceipt("k", env, r("false", "true", 1_001_500)))
        assertNull(DemoControl.signableReceipt("k", env, r("true", "false", 1_001_500)))
        assertNull(DemoControl.signableReceipt("k", env, r("true", "true", 1_120_000)))
        assertNull(DemoControl.signableReceipt("k", env, r("true", "true", 999_999)))
        assertNotNull(DemoControl.signableReceipt("k", env, r("true", "true", 1_001_500)))
    }

    @Test
    fun receiptForADifferentRequestOrActionIsNotSigned() {
        val env = DemoControl.parseEnvelope(envelopeJson("pause_demo_orders"))!!
        val other = pauseApplied().replace(id, "12345678-1234-4234-8234-123456789abd")
        assertNull(DemoControl.signableReceipt("k", env, DemoControl.parseResult(other)))
        val wrongAction = pauseApplied().replace("pause_demo_orders", "refresh_demo_broker")
        assertNull(DemoControl.signableReceipt("k", env, DemoControl.parseResult(wrongAction)))
        val wrongDevice = pauseApplied().replace("synthetic-device", "other-device")
        assertNull(DemoControl.signableReceipt("k", env, DemoControl.parseResult(wrongDevice)))
    }

    @Test
    fun badAuthenticationNeverReachesPython() {
        val good = signed("pause_demo_orders")
        val cases = listOf(
            good - "hmac",
            good + ("hmac" to "0".repeat(64)),
            good + ("hmac" to good.getValue("hmac").uppercase()),
            good + ("envelope" to envelopeJson("pause_demo_orders", issued = 1_000_001)), // tampered
            good - "envelope",
        )
        for (data in cases) {
            val deps = Deps(pauseApplied(), signer = signer())
            assertEquals("rejected", DemoControlHandler.handle(data, deps).label)
            assertEquals(0, deps.applied)
        }
        val noSecret = Deps(pauseApplied(), secret = null, signer = signer())
        assertEquals("unavailable", DemoControlHandler.handle(good, noSecret).label)
        assertEquals(0, noSecret.applied)
        val noDevice = Deps(pauseApplied(), device = null, signer = signer())
        assertEquals("unavailable", DemoControlHandler.handle(good, noDevice).label)
        val wrongDevice = Deps(pauseApplied(), device = "other-device", signer = signer())
        assertEquals("rejected", DemoControlHandler.handle(good, wrongDevice).label)
        assertEquals(0, wrongDevice.applied)
    }

    @Test
    fun expiredRequestIsNotApplied() {
        val deps = Deps(pauseApplied(), now = 1_120_000, signer = signer())
        assertEquals("expired", DemoControlHandler.handle(signed("pause_demo_orders"), deps).label)
        assertEquals(0, deps.applied)
    }

    @Test
    fun malformedEnvelopesAreRejected() {
        val ok = envelopeJson("pause_demo_orders")
        assertNotNull(DemoControl.parseEnvelope(ok))
        listOf(
            ok.replace("\"demo\"", "\"real\""),
            ok.replace("pause_demo_orders", "resume_demo_orders"),
            ok.replace("1120000", "1120001"),
            ok.replace(id, id.uppercase()),
            ok.replace("\"version\":1", "\"version\":2"),
            ok.dropLast(1) + ""","extra":1}""",
            ok.replace("1000000", "1000000.5"),
            ok.replace("synthetic-device", "bad device"),
            "not json", "",
        ).forEach { assertNull(it, DemoControl.parseEnvelope(it)) }
        assertNull(DemoControl.parseEnvelope(null))
    }

    @Test
    fun missingSignerOrFailedPostLeavesNoReceipt() {
        val noSigner = Deps(pauseApplied(), signer = null)
        assertEquals("unreceipted:signer_unavailable", DemoControlHandler.handle(signed("pause_demo_orders"), noSigner).label)
        assertTrue(noSigner.posted.isEmpty())
        val failing = Deps(pauseApplied(), signer = object : ReceiptSigner {
            override val keyId = "synthetic-pin-v1"
            override fun sign(bytes: ByteArray): ByteArray = error("keystore locked")
        })
        assertEquals("unreceipted:sign_failed", DemoControlHandler.handle(signed("pause_demo_orders"), failing).label)
        val offline = Deps(pauseApplied(), signer = signer(), postStatus = null)
        assertEquals("unreceipted:post_failed", DemoControlHandler.handle(signed("pause_demo_orders"), offline).label)
    }

    @Test
    fun unparseableOrRefusedPythonResultsAreNeverReceipted() {
        for (response in listOf(null, "garbage", """{"status":"refused","reasonCode":"duplicate"}""",
            """{"status":"unconfirmed","reasonCode":"guard_not_enforced"}""",
            """{"status":"expired_unconfirmed"}""")) {
            val deps = Deps(response, signer = signer())
            assertTrue(DemoControlHandler.handle(signed("pause_demo_orders"), deps).label.startsWith("not_applied"))
            assertTrue(deps.posted.isEmpty())
        }
    }

    @Test
    fun base64UrlMatchesTheStandardLibraryForEveryLengthRemainder() {
        for (n in 0..40) {
            val bytes = ByteArray(n) { (it * 37 + 11).toByte() }
            val expected = java.util.Base64.getUrlEncoder().withoutPadding().encodeToString(bytes)
            assertEquals(expected, DemoControl.base64Url(bytes))
        }
    }

    @Test
    fun wiringKeepsWriteAuthorityAndActionsNarrow() {
        val main = listOf(java.io.File("src/main"), java.io.File("app/src/main")).first { it.isDirectory }
        fun src(n: String) = java.io.File(main, "java/com/nightwatch/etoro/probe/$n").readText()
        val bridge = src("PyBridge.kt")
        assertEquals(1, bridge.split("Kwarg(\"operator_write_authorized\"").size - 1)
        val applyFn = bridge.substringAfter("fun applyDemoControl(").substringBefore("/** Manual adoption")
        assertFalse(applyFn.contains("sessionLock"))
        assertFalse(applyFn.contains("bridgeLock"))
        assertTrue(src("NightwatchProbeMessagingService.kt").contains("command == \"demo_control\""))
        assertEquals(setOf("refresh_demo_broker", "pause_demo_orders"), DemoControl.ACTIONS)
    }
}
