package com.nightwatch.etoro.probe

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Check 2 (task Problem 4): "the real app alerts from the device, not the laptop." Sends a real
 * Telegram message directly from the phone's own network connection using the bot token / chat
 * id supplied by a trusted runtime provider (never hardcoded here).
 * This also gives the user tangible proof the phone has working internet independent of the
 * Worker.
 */
object TelegramClient {
    private val http = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .build()

    data class Result(val success: Boolean, val httpStatus: Int?, val detail: String)

    fun sendMessage(text: String): Result {
        val token = RuntimeCredentials.value(RuntimeCredentials.Key.TELEGRAM_BOT_TOKEN)
        val chatId = RuntimeCredentials.value(RuntimeCredentials.Key.TELEGRAM_CHAT_ID)
        if (token == null || chatId == null) {
            return Result(
                success = false,
                httpStatus = null,
                detail = "Telegram authentication unavailable: runtime provisioning required"
            )
        }
        return try {
            val body = JSONObject().apply {
                put("chat_id", chatId)
                put("text", text)
            }.toString().toRequestBody("application/json".toMediaType())

            val request = Request.Builder()
                .url("https://api.telegram.org/bot$token/sendMessage")
                .post(body)
                .build()

            http.newCall(request).execute().use { response ->
                val respBody = response.body?.string()?.take(500) ?: ""
                Result(
                    success = response.isSuccessful,
                    httpStatus = response.code,
                    detail = "status=${response.code} body=$respBody"
                )
            }
        } catch (e: IOException) {
            Result(success = false, httpStatus = null, detail = "${e.javaClass.simpleName}: ${e.message}")
        }
    }
}
