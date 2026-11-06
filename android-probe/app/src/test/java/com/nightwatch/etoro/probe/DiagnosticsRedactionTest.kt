package com.nightwatch.etoro.probe

import org.junit.Assert.*
import org.junit.Test

class DiagnosticsRedactionTest {
    @Test
    fun fcmDiagnosticsContainOnlyKeysAndTokenFingerprint() {
        val data = mapOf("command" to "short-secret", "hmac" to "synthetic-hmac",
            "body" to "private-message", "counter" to "12345")
        val line = NightwatchProbeMessagingService.messageDetail(data)
        assertEquals("type=data data_keys=[body, command, counter, hmac]", line)
        data.values.forEach { assertFalse(line.contains(it)) }
        assertEquals("length=3 sha256=ba7816bf", NightwatchProbeMessagingService.tokenDetail("abc"))
        val token = "synthetic-registration-token"
        val tokenLine = NightwatchProbeMessagingService.tokenDetail(token)
        assertTrue(tokenLine.matches(Regex("length=28 sha256=[0-9a-f]{8}")))
        assertFalse(tokenLine.contains(token))
        assertFalse(tokenLine.contains(token.takeLast(4)))
    }

    @Test
    fun eventStoreBoundaryRedactsBeforePersistenceAndFailsClosed() {
        val secret = "short-secret"
        assertEquals("value=***REDACTED***", EventStore.redactedDetail("CHECK", "value=$secret") {
            it.replace(secret, "***REDACTED***")
        })
        assertEquals("", EventStore.redactedDetail("CHECK", secret) { null })
        assertEquals("", EventStore.redactedDetail("CHECK", secret) { error(secret) })
        assertEquals("details omitted", EventStore.redactedDetail("COMMAND_UNKNOWN", "data=$secret") {
            fail("arbitrary FCM payload must not depend on pattern redaction"); it
        })
    }

    @Test
    fun snapshotHasOnlyAllowlistedFieldsAndRedactsLegacyEventsAndChecks() {
        val secret = "short-secret"
        val fields = SnapshotPusher.snapshotFields(
            listOf(EventStore.Event(123L, "CHECK", "token=$secret")),
            listOf(CheckBattery.CheckResult("SQLITE_WAL", false, "error=$secret")),
            456L, true
        ) { it.replace(secret, "***REDACTED***") }
        assertEquals(setOf("probe", "device", "halt", "recent_events", "latest_checks"), fields.keys)
        assertEquals(mapOf("next_alarm_epoch_ms" to 456L), fields["device"])
        assertEquals(true, fields["halt"])
        assertEquals(listOf(mapOf("type" to "CHECK", "ts_epoch_ms" to 123L,
            "detail" to "token=***REDACTED***")), fields["recent_events"])
        assertEquals(listOf(mapOf("name" to "SQLITE_WAL", "pass" to false,
            "detail" to "error=***REDACTED***")), fields["latest_checks"])
        assertFalse(fields.toString().contains(secret))
    }

    @Test
    fun snapshotOmitsDetailsWhenSanitizerIsMissingOrThrows() {
        for (redact in listOf<(String) -> String?>({ null }, { error("synthetic-secret") })) {
            val fields = SnapshotPusher.snapshotFields(
                listOf(EventStore.Event(123L, "synthetic-secret", "synthetic-secret")),
                listOf(CheckBattery.CheckResult("synthetic-secret", true, "synthetic-secret")),
                null, false, redact)
            assertEquals(listOf(mapOf("type" to "EVENT", "ts_epoch_ms" to 123L)), fields["recent_events"])
            assertEquals(listOf(mapOf("name" to "CHECK", "pass" to true)), fields["latest_checks"])
            assertFalse(fields.toString().contains("synthetic-secret"))
            assertEquals(emptyList<Any>(), SnapshotPusher.snapshotFields(
                emptyList(), null, null, false, redact)["latest_checks"])
        }
    }
}
