package com.nightwatch.etoro.probe

import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith
import org.junit.runners.Parameterized

@RunWith(Parameterized::class)
class PowerStatusTest(private val charging: Boolean, private val capacity: Int) {
    companion object {
        @JvmStatic
        @Parameterized.Parameters(name = "charging={0}, capacity={1}")
        fun cases(): List<Array<Any>> = listOf(false, true).flatMap { charging ->
            listOf(Int.MIN_VALUE, -1, 0, 29, 30, 100, 101).map { arrayOf<Any>(charging, it) }
        }
    }

    @Test
    fun sessionPowerTruthTable() {
        val expected = charging || capacity == 30 || capacity == 100
        assertEquals(expected, PowerStatus.shouldRunSession(charging, capacity))
    }
}
