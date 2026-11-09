package com.nightwatch.etoro.probe

import org.junit.Assert.*
import org.junit.Test

class EventStoreRetentionTest {
    @Test
    fun cutoffIsExactlyNinetyDaysBeforeNow() {
        val now = 1_800_000_000_000L
        assertEquals(90, EventStore.RETENTION_DAYS)
        assertEquals(now - 7_776_000_000L, EventStore.retentionCutoff(now))
    }

    @Test
    fun strictCutoffKeepsBoundaryRecentAndFutureEvents() {
        val now = 1_800_000_000_000L
        val cutoff = EventStore.retentionCutoff(now)
        val events = listOf(
            EventStore.Event(cutoff - 1, "OLD", "expired"),
            EventStore.Event(cutoff, "BOUNDARY", "keep"),
            EventStore.Event(now, "RECENT", "keep"),
            EventStore.Event(now + 1, "FUTURE", "keep")
        )
        // Mirror the strict SQL deletion predicate without an Android database.
        val retained = events.filterNot { it.tsEpochMs < cutoff }
        assertEquals(listOf("BOUNDARY", "RECENT", "FUTURE"), retained.map { it.type })
        assertTrue(retained.first().formatted().endsWith("  [BOUNDARY]  keep"))
    }
}
