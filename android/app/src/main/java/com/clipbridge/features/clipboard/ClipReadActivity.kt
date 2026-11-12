package com.clipbridge.features.clipboard

import android.app.Activity
import android.content.ClipboardManager
import android.os.Bundle
import android.util.Log
import com.clipbridge.BridgeService

/** Transparent activity: gains focus, reads the clipboard (~150 ms), hands text/image to the service, finishes. */
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
        val item = clip?.takeIf { it.itemCount > 0 }?.getItemAt(0)
        val uri = item?.uri
        val isImage = uri != null && runCatching { contentResolver.getType(uri) }.getOrNull()?.startsWith("image/") == true
        if (isImage && uri != null) {
            val png = ImageUtil.readPng(this, uri)
            Log.i("ClipBridge", "reader: image clip len=${png?.size}")
            if (png != null && png.isNotEmpty()) BridgeService.submitImage(this, png)
        } else {
            val text = item?.coerceToText(this)?.toString()
            Log.i("ClipBridge", "reader: focus, clip items=${clip?.itemCount} len=${text?.length}")
            if (!text.isNullOrEmpty()) BridgeService.submit(this, text)
        }
        finish()
        overridePendingTransition(0, 0)
    }
}
