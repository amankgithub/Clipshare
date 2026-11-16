package com.clipbridge.features.clipboard

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import androidx.core.content.FileProvider
import java.io.File

/** Where clips from the peer are put. */
interface ClipSink {
    fun write(text: String)
    fun writeImage(png: ByteArray)
}

/** Writes to the system clipboard (background writes are allowed). */
class ClipboardWriter(private val context: Context) : ClipSink {
    private val cm = context.getSystemService(ClipboardManager::class.java)

    override fun write(text: String) { cm.setPrimaryClip(ClipData.newPlainText("ClipBridge", text)) }

    /** Saves PNG to cacheDir/clips/latest.png and puts a FileProvider URI on the clipboard. */
    override fun writeImage(png: ByteArray) {
        val dir = File(context.cacheDir, "clips").apply { mkdirs() }
        val f = File(dir, "latest.png").also { it.writeBytes(png) }
        val uri = FileProvider.getUriForFile(context, "${context.packageName}.files", f)
        cm.setPrimaryClip(ClipData.newUri(context.contentResolver, "ClipBridge", uri))
    }
}
