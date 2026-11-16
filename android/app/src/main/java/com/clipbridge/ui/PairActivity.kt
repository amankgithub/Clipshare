package com.clipbridge.ui

import android.app.Activity
import android.os.Bundle
import android.util.Log
import com.clipbridge.BridgeService
import com.clipbridge.Settings

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
        val r = PairExtras.apply(s, device, psk, host)
        if (r.invalidPairing) Log.w("ClipBridge", "pair: invalid device/psk")
        if (r.changed) BridgeService.reconfigure(this)
        finish()
    }
}
