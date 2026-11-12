package com.clipbridge.features.clipboard

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import java.io.ByteArrayOutputStream
import java.io.File
import java.util.UUID

/** Image helpers: any readable image URI -> PNG bytes, and staging bytes for the service. */
object ImageUtil {
    const val MAX_BYTES = 16 * 1024 * 1024

    private val PNG_MAGIC = byteArrayOf(0x89.toByte(), 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A)

    fun isPng(b: ByteArray) = b.size >= 8 && (0 until 8).all { b[it] == PNG_MAGIC[it] }

    /** Reads [uri] and returns PNG bytes (converting if needed), or null if unreadable/not an image. */
    fun readPng(context: Context, uri: Uri): ByteArray? = runCatching {
        val raw = context.contentResolver.openInputStream(uri)?.use { it.readBytes() } ?: return null
        if (isPng(raw)) raw else {
            val bmp = BitmapFactory.decodeByteArray(raw, 0, raw.size) ?: return null
            ByteArrayOutputStream().use { out -> bmp.compress(Bitmap.CompressFormat.PNG, 100, out); out.toByteArray() }
        }
    }.getOrNull()

    /** Writes PNG bytes to a private temp file (intents cannot carry big payloads). Service deletes it. */
    fun stage(context: Context, png: ByteArray): File {
        val dir = File(context.cacheDir, "outgoing").apply { mkdirs() }
        return File(dir, "${UUID.randomUUID()}.png").also { it.writeBytes(png) }
    }
}
