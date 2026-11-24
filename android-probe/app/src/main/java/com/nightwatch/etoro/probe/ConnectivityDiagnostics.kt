package com.nightwatch.etoro.probe

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.PowerManager
import android.provider.Settings

/**
 * T085: why does DNS fail at 03:05? NETWORK_CHECK records only the exception. This adds the
 * phone's own view (default network, transport, validated, suspended, airplane, Doze, saver)
 * as short key=value tokens. No identifiers, addresses or SSIDs are read.
 */
internal object ConnectivityDiagnostics {
    private fun flag(v: Int) = when (v) { 1 -> "on"; 0 -> "off"; else -> "unknown" }

    fun summary(context: Context): String = try {
        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        val pm = context.getSystemService(Context.POWER_SERVICE) as PowerManager
        val caps = cm.activeNetwork?.let { cm.getNetworkCapabilities(it) }
        val transport = when {
            caps == null -> "none"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "wifi"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "cellular"
            else -> "other"
        }
        val validated = caps?.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED) == true
        val suspended = caps?.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_SUSPENDED) == false
        val airplane = Settings.Global.getInt(context.contentResolver, Settings.Global.AIRPLANE_MODE_ON, 0) == 1
        // The two user-facing switches, read from global settings (no permission needed).
        val wifiOn = Settings.Global.getInt(context.contentResolver, Settings.Global.WIFI_ON, -1)
        val dataOn = Settings.Global.getInt(context.contentResolver, "mobile_data", -1)
        @Suppress("DEPRECATION")
        val networks = cm.allNetworks.size
        "net=$transport validated=$validated suspended=$suspended airplane=$airplane " +
            "wifiSwitch=${flag(wifiOn)} dataSwitch=${flag(dataOn)} networks=$networks " +
            "idle=${pm.isDeviceIdleMode} saver=${pm.isPowerSaveMode}"
    } catch (e: Exception) {
        "net=unknown error=${e.javaClass.simpleName}"
    }
}
