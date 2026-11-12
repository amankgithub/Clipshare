package com.clipbridge.features.files

import android.content.Context
import android.net.Uri
import android.os.ParcelFileDescriptor
import android.provider.OpenableColumns
import android.system.ErrnoException
import android.system.Os
import com.clipbridge.link.FileSource
import java.io.IOException
import java.io.InputStream

/**
 * A shared file (any `content://` or `file://` URI). The descriptor is opened at once, while the
 * share grant is valid: it outlives the share activity, so a queued or resumed send can still read it.
 */
class ContentSource private constructor(
    private val pfd: ParcelFileDescriptor,
    override val name: String,
    override val size: Long,
) : FileSource {

    /** Positional reads (pread), so every stream starts at byte 0 independently of the others. */
    override fun open(): InputStream = object : InputStream() {
        private var pos = 0L
        override fun read(): Int {
            val b = ByteArray(1)
            return if (read(b, 0, 1) <= 0) -1 else b[0].toInt() and 0xff
        }
        override fun read(b: ByteArray, off: Int, len: Int): Int {
            if (len == 0) return 0
            if (pos >= size) return -1
            val n = try { Os.pread(pfd.fileDescriptor, b, off, len, pos) } catch (e: ErrnoException) { throw IOException(e) }
            if (n <= 0) return -1
            pos += n
            return n
        }
        override fun skip(n: Long): Long {
            val s = n.coerceIn(0, size - pos)
            pos += s
            return s
        }
    }

    fun close() { runCatching { pfd.close() } }

    companion object {
        /** @return null if [uri] cannot be opened or its size is unknown (a stream, not a file). */
        fun open(context: Context, uri: Uri): ContentSource? {
            val pfd = runCatching { context.contentResolver.openFileDescriptor(uri, "r") }.getOrNull() ?: return null
            var name: String? = null
            var size = pfd.statSize
            runCatching {
                context.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE), null, null, null)?.use { c ->
                    if (c.moveToFirst()) {
                        if (!c.isNull(0)) name = c.getString(0)
                        if (size < 0 && !c.isNull(1)) size = c.getLong(1)
                    }
                }
            }
            if (size < 0) { pfd.close(); return null }
            return ContentSource(pfd, name?.takeIf { it.isNotBlank() } ?: uri.lastPathSegment ?: "file", size)
        }
    }
}
