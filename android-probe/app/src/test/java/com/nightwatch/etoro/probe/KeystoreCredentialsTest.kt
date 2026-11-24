package com.nightwatch.etoro.probe

import org.junit.Assert.*
import org.junit.Test
import java.io.File

class KeystoreCredentialsTest {
    @Test
    fun parsesOnlyKnownNonBlankStringKeys() {
        val m = KeystoreCredentials.parseProvision(
            """{"COMMAND_HMAC_SECRET":" synthetic ","TELEGRAM_BOT_TOKEN":"","TELEGRAM_CHAT_ID":123,"EXTRA":"x","PHONE_PUSH_SECRET":"p"}""")
        assertEquals(mapOf(RuntimeCredentials.Key.COMMAND_HMAC_SECRET to "synthetic",
            RuntimeCredentials.Key.PHONE_PUSH_SECRET to "p"), m)
        assertTrue(KeystoreCredentials.parseProvision("{}").isEmpty())
        assertThrows(Exception::class.java) { KeystoreCredentials.parseProvision("nope") }
    }

    @Test
    fun wiringInstallsProviderFirstAndNeverLogsValues() {
        val main = listOf(File("src/main"), File("app/src/main")).first { it.isDirectory }
        val manifest = File(main, "AndroidManifest.xml").readText()
        assertTrue(manifest.contains("android:name=\".NightwatchApp\""))
        val src = File(main, "java/com/nightwatch/etoro/probe/KeystoreCredentials.kt").readText()
        assertFalse(src.contains("\"CREDENTIALS_PROVISIONED\", \"\$v"))
        assertTrue(src.contains("keys=\${values.keys.map { it.name }.sorted()}"))
        assertTrue(src.contains("file.delete()"))
        assertTrue(src.indexOf("check(RuntimeCredentials.value(k) == v)") < src.indexOf("file.delete()"))
    }
}
