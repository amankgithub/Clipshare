package com.clipbridge.features.clipboard

import android.app.Activity
import android.content.ClipboardManager
import android.os.Bundle
import android.util.Log
import com.clipbridge.BridgeService

/** Transparent activity: gains focus, reads the clipboard (~150 ms), hands text to the service, finishes. */
class ClipReadActivity : Activity() {
    private var done = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (!hasFocus || done) return
        done = true
        val clip = getSystemService(ClipboardManager::class.java).primaryClip
        val text = clip?.takeIf { it.itemCount > 0 }?.getItemAt(0)?.coerceToText(this)?.toString()
        Log.i("ClipBridge", "reader: focus, clip items=${clip?.itemCount} len=${text?.length}")
        if (!text.isNullOrEmpty()) BridgeService.submit(this, text)
        finish()
        overridePendingTransition(0, 0)
    }
}
