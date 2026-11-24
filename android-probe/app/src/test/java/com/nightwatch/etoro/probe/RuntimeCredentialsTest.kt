package com.nightwatch.etoro.probe

import org.junit.After
import org.junit.Assert.*
import org.junit.Test

class RuntimeCredentialsTest {
    @After
    fun clearProvider() {
        RuntimeCredentials.provider = null
    }

    @Test
    fun missingBlankAndFailedProvidersAreUnavailableForEveryKey() {
        val providers = listOf<RuntimeCredentials.Provider?>(
            null,
            RuntimeCredentials.Provider { null },
            RuntimeCredentials.Provider { " \n\t" },
            RuntimeCredentials.Provider { error("synthetic provider failure") }
        )
        for (provider in providers) {
            RuntimeCredentials.provider = provider
            for (key in RuntimeCredentials.Key.entries) {
                assertNull(RuntimeCredentials.value(key))
            }
        }
    }

    @Test
    fun providerReceivesExactKeyAndValuesAreNotCached() {
        val seen = mutableListOf<RuntimeCredentials.Key>()
        RuntimeCredentials.provider = RuntimeCredentials.Provider {
            seen.add(it)
            if (it == RuntimeCredentials.Key.COMMAND_HMAC_SECRET) "synthetic-only" else null
        }
        assertEquals("synthetic-only", RuntimeCredentials.value(RuntimeCredentials.Key.COMMAND_HMAC_SECRET))
        assertNull(RuntimeCredentials.value(RuntimeCredentials.Key.PHONE_PUSH_SECRET))
        assertEquals(listOf(RuntimeCredentials.Key.COMMAND_HMAC_SECRET,
            RuntimeCredentials.Key.PHONE_PUSH_SECRET), seen)
        RuntimeCredentials.provider = null
        assertNull(RuntimeCredentials.value(RuntimeCredentials.Key.COMMAND_HMAC_SECRET))
    }

    @Test
    fun telegramDoesNotTransmitWithoutBothRuntimeInputs() {
        for (available in listOf<RuntimeCredentials.Key?>(null,
                RuntimeCredentials.Key.TELEGRAM_BOT_TOKEN, RuntimeCredentials.Key.TELEGRAM_CHAT_ID)) {
            RuntimeCredentials.provider = RuntimeCredentials.Provider {
                if (it == available) "synthetic-only" else null
            }
            val result = TelegramClient.sendMessage("synthetic message")
            assertFalse(result.success)
            assertNull(result.httpStatus)
            assertEquals("Telegram authentication unavailable: runtime provisioning required", result.detail)
        }
    }
}
