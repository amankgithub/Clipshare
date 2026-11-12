package com.clipbridge.features.clipboard

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.widget.Toast
import com.clipbridge.BridgeService

// Share target: SEND for text and image types, SEND_MULTIPLE for images. Images are converted to PNG.
class ShareTargetActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val i = intent
        when (i?.action) {
            Intent.ACTION_SEND -> {
                val uri = uriExtra(i)
                if (uri != null && i.type?.startsWith("image/") == true) sendImages(listOf(uri))
                else i.getCharSequenceExtra(Intent.EXTRA_TEXT)?.toString()
                    ?.takeIf { it.isNotEmpty() }?.let { BridgeService.submit(this, it) }
            }
            Intent.ACTION_SEND_MULTIPLE -> sendImages(uriList(i))
        }
        finish()
        overridePendingTransition(0, 0)
    }

    /** Toasts "Sending…" now; [ImageDelivery] toasts each image's outcome when the PC confirms. */
    private fun sendImages(uris: List<Uri>) {
        val sent = uris.count { uri ->
            ImageUtil.readPng(this, uri)?.takeIf { it.isNotEmpty() }?.let { BridgeService.submitImage(this, it) } != null
        }
        val msg = when {
            sent == 0 -> "Couldn't read image"
            sent == 1 -> "Sending image to PC…"
            else -> "Sending $sent images to PC…"
        }
        Toast.makeText(applicationContext, msg, Toast.LENGTH_SHORT).show()
    }

    @Suppress("DEPRECATION")
    private fun uriExtra(i: Intent): Uri? =
        if (Build.VERSION.SDK_INT >= 33) i.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)
        else i.getParcelableExtra(Intent.EXTRA_STREAM)

    @Suppress("DEPRECATION")
    private fun uriList(i: Intent): List<Uri> =
        (if (Build.VERSION.SDK_INT >= 33) i.getParcelableArrayListExtra(Intent.EXTRA_STREAM, Uri::class.java)
        else i.getParcelableArrayListExtra<Uri>(Intent.EXTRA_STREAM)) ?: emptyList()
}
