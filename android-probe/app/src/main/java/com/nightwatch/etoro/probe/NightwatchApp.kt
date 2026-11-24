package com.nightwatch.etoro.probe

import android.app.Application
import java.io.File

/** Runs before any receiver or service, so every entry point sees the credential provider. */
class NightwatchApp : Application() {
    override fun onCreate() {
        super.onCreate()
        KeystoreCredentials.install(this)
        // Public half only, so it can be pinned on the Worker; never the private key. The event
        // log redacts long values, so the same public text is also kept in a plain file.
        Thread {
            DemoControlIdentity.publicKeySpkiBase64()?.let { spki ->
                File(filesDir, "demo_control_pubkey.txt")
                    .writeText(DemoControlIdentity.KEY_ID + "\n" + spki + "\n")
                EventStore.get(this).log("DEMO_CONTROL_PUBKEY", "keyId=${DemoControlIdentity.KEY_ID} published")
            }
        }.start()
    }
}
