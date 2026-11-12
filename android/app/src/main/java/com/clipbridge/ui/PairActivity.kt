package com.clipbridge.ui

import android.app.Activity
import android.os.Bundle
import android.util.Log
import com.clipbridge.BridgeService
import com.clipbridge.Settings

/**
 * Exported but guarded by android.permission.DUMP in the manifest, so only `adb shell` can call it:
 * adb shell am start -n com.clipbridge/.ui.PairActivity --es psk <b64> --es host <ip[:port]>
 */
class PairActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val s = Settings(this)
        val psk = intent.getStringExtra("psk")
        val host = intent.getStringExtra("host")
        var changed = false
        if (psk != null) {
            if (s.setPsk(psk)) changed = true else Log.w("ClipBridge", "pair: invalid psk")
        }
        if (host != null) { s.manualHost = host; changed = true }
        if (changed) BridgeService.reconfigure(this)
        finish()
    }
}
