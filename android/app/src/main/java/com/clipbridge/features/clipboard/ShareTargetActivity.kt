package com.clipbridge.features.clipboard

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.widget.Toast
import com.clipbridge.BridgeService
import com.clipbridge.features.files.ContentSource

/**
 * Share target: SEND for text, SEND / SEND_MULTIPLE for any files. A share of exactly one image that fits a clip goes
 * through the clipboard image path (converted to PNG); anything else goes through file transfer as the original
 * files, so one share never splits between the PC clipboard and Downloads\ClipShare.
 */
class ShareTargetActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val i = intent
        when (i?.action) {
            Intent.ACTION_SEND -> {
                val uri = uriExtra(i)
                if (uri != null) sendUris(listOf(uri))
                else i.getCharSequenceExtra(Intent.EXTRA_TEXT)?.toString()
                    ?.takeIf { it.isNotEmpty() }?.let { BridgeService.submit(this, it) }
            }
            Intent.ACTION_SEND_MULTIPLE -> sendUris(uriList(i))
        }
        finish()
        overridePendingTransition(0, 0)
    }

    /**
     * Toasts "Sending…" now; the result comes later ([ImageDelivery] per image, the files feature per file).
     * Files are opened here, while this activity holds the share grant.
     */
    private fun sendUris(uris: List<Uri>) {
        var images = 0
        val files = ArrayList<ContentSource>()
        val single = uris.size == 1
        for (uri in uris) {
            val file = ContentSource.open(this, uri)
            val png = if (single && isImage(uri) && (file == null || file.size <= ImageUtil.MAX_BYTES)) {
                ImageUtil.readPng(this, uri)?.takeIf { it.isNotEmpty() && it.size <= ImageUtil.MAX_BYTES }
            } else null
            if (png != null) { file?.close(); BridgeService.submitImage(this, png); images++ }
            else if (file != null) files += file
        }
        BridgeService.submitFiles(this, files)
        val msg = when {
            images + files.size == 0 -> "Couldn't read " + if (uris.size == 1) "file" else "files"
            files.isEmpty() -> if (images == 1) "Sending image to PC…" else "Sending $images images to PC…"
            images == 0 && files.size == 1 -> "Sending ${files[0].name} to PC…"
            else -> "Sending ${images + files.size} files to PC…"
        }
        Toast.makeText(applicationContext, msg, Toast.LENGTH_SHORT).show()
    }

    private fun isImage(uri: Uri): Boolean = (contentResolver.getType(uri) ?: intent.type)?.startsWith("image/") == true

    @Suppress("DEPRECATION")
    private fun uriExtra(i: Intent): Uri? =
        if (Build.VERSION.SDK_INT >= 33) i.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)
        else i.getParcelableExtra(Intent.EXTRA_STREAM)

    @Suppress("DEPRECATION")
    private fun uriList(i: Intent): List<Uri> =
        (if (Build.VERSION.SDK_INT >= 33) i.getParcelableArrayListExtra(Intent.EXTRA_STREAM, Uri::class.java)
        else i.getParcelableArrayListExtra<Uri>(Intent.EXTRA_STREAM)) ?: emptyList()
}
