package com.clipbridge.link

import android.content.Context
import android.net.wifi.WifiManager
import android.os.PowerManager

/**
 * Held while file bytes flow: a low-latency Wi-Fi lock (Wi-Fi power save is the main throughput
 * killer) and a partial wakelock (the CPU keeps streaming with the screen off).
 */
class TransferLock(context: Context) {
    private val wifi = context.getSystemService(WifiManager::class.java)
        .createWifiLock(WifiManager.WIFI_MODE_FULL_LOW_LATENCY, "ClipBridge:transfer")
        .apply { setReferenceCounted(false) }
    private val wake = context.getSystemService(PowerManager::class.java)
        .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "ClipBridge:transfer")
        .apply { setReferenceCounted(false) }

    fun hold(on: Boolean) {
        if (on) {
            wake.acquire(MAX_HOLD_MS)
            wifi.acquire()
        } else {
            if (wifi.isHeld) wifi.release()
            if (wake.isHeld) wake.release()
        }
    }

    private companion object {
        /** Safety net; each range re-acquires, so long transfers keep it. */
        const val MAX_HOLD_MS = 60L * 60 * 1000
    }
}
