package com.clipbridge.ui

import android.app.Activity
import android.os.Bundle
import android.util.Log
import com.clipbridge.BridgeService
import com.clipbridge.Settings
import com.clipbridge.link.Pairing

/**
 * Exported but guarded by android.permission.DUMP in the manifest, so only `adb shell` can call it
 * (`clipbridge pair` on the PC does): device id and PSK come together, the host is optional:
 * adb shell am start -n com.clipbridge/.ui.PairActivity --es device <id> --es psk <b64> --es host <ip[:port]>
 */
class PairActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val s = Settings(this)
        val device = intent.getStringExtra("device")
        val psk = intent.getStringExtra("psk")
        val host = intent.getStringExtra("host")
        var changed = false
        if (device != null || psk != null) {
            val pairing = if (device != null && psk != null) Pairing.of(device, psk.trim()) else null
            if (pairing != null) { s.pairing = pairing; changed = true } else Log.w("ClipBridge", "pair: invalid device/psk")
        }
        if (host != null) { s.manualHost = host; changed = true }
        if (changed) BridgeService.reconfigure(this)
        finish()
    }
}
