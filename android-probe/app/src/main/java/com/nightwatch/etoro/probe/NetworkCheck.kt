package com.nightwatch.etoro.probe

import kotlinx.coroutines.delay
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import kotlin.math.min

/**
 * DESIGN §4.1 budgets 5 minutes of slack partly to absorb lazy Wi-Fi re-association coming out
 * of Doze. This is the measurement that tells us whether that budget is right: on every wake,
 * hit the Worker's /health with a bounded retry and record how long it actually took to get
 * connectivity.
 */
object NetworkCheck {

    data class Result(
        val success: Boolean,
        val attempts: Int,
        val totalLatencyMs: Long,
        val httpStatus: Int?,
        val error: String?
    )

    /**
     * Retries with backoff for up to [maxTotalMs] (default 90s, matching DESIGN §5 gate 3's own
     * 90s polling window for Wi-Fi re-association after Doze).
     */
    suspend fun checkHealth(
        baseUrl: String,
        maxTotalMs: Long = 90_000,
        initialBackoffMs: Long = 1_000
    ): Result {
        val start = System.currentTimeMillis()
        var attempt = 0
        var backoff = initialBackoffMs
        var lastError: String? = null
        var lastStatus: Int? = null

        while (System.currentTimeMillis() - start < maxTotalMs) {
            attempt++
            try {
                val url = URL("$baseUrl/health")
                val conn = url.openConnection() as HttpURLConnection
                conn.requestMethod = "GET"
                conn.connectTimeout = 8_000
                conn.readTimeout = 8_000
                try {
                    val code = conn.responseCode
                    lastStatus = code
                    if (code in 200..299) {
                        return Result(
                            success = true,
                            attempts = attempt,
                            totalLatencyMs = System.currentTimeMillis() - start,
                            httpStatus = code,
                            error = null
                        )
                    }
                    lastError = "HTTP $code"
                } finally {
                    conn.disconnect()
                }
            } catch (e: IOException) {
                lastError = e.javaClass.simpleName + ": " + (e.message ?: "")
            }
            val remaining = maxTotalMs - (System.currentTimeMillis() - start)
            if (remaining <= 0) break
            delay(min(backoff, remaining))
            backoff = min(backoff * 2, 15_000)
        }

        return Result(
            success = false,
            attempts = attempt,
            totalLatencyMs = System.currentTimeMillis() - start,
            httpStatus = lastStatus,
            error = lastError
        )
    }
}
