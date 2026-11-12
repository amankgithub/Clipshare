package com.clipbridge.features.files

import android.content.ContentResolver
import android.content.ContentValues
import android.content.Context
import android.net.Uri
import android.os.Environment
import android.os.ParcelFileDescriptor
import android.provider.MediaStore
import android.system.ErrnoException
import android.system.Os
import android.webkit.MimeTypeMap
import com.clipbridge.link.FileSink
import java.io.FileDescriptor
import java.io.IOException

/**
 * A received file in `Downloads/ClipShare`, written in place as a MediaStore row with `IS_PENDING = 1`
 * (hidden from other apps) and published by [commit]. The row's URI is the resume [token].
 */
class MediaStoreSink private constructor(
    private val resolver: ContentResolver,
    val uri: Uri,
    private val size: Long,
    private var pfd: ParcelFileDescriptor?,
) : FileSink {
    override val token: String = uri.toString()

    private fun fd(): FileDescriptor = synchronized(this) {
        (pfd ?: (resolver.openFileDescriptor(uri, "rw") ?: throw IOException("cannot open")).also { pfd = it }).fileDescriptor
    }

    private inline fun <T> io(block: () -> T): T = try { block() } catch (e: ErrnoException) { throw IOException(e) }

    override fun write(position: Long, data: ByteArray) = io {
        var off = 0
        while (off < data.size) off += Os.pwrite(fd(), data, off, data.size - off, position + off)
    }

    override fun read(position: Long, into: ByteArray, length: Int): Int = io { Os.pread(fd(), into, 0, length, position) }

    override fun sync() = io { Os.fsync(fd()) }

    override fun commit() {
        io { Os.ftruncate(fd(), size); Os.fsync(fd()) }
        close()
        resolver.update(uri, ContentValues().apply { put(MediaStore.MediaColumns.IS_PENDING, 0) }, null, null)
    }

    override fun discard() {
        close()
        resolver.delete(uri, null, null)
    }

    private fun close() = synchronized(this) { pfd?.close(); pfd = null }

    companion object {
        private val DIR = Environment.DIRECTORY_DOWNLOADS + "/ClipShare"

        fun mimeOf(name: String): String =
            MimeTypeMap.getSingleton().getMimeTypeFromExtension(name.substringAfterLast('.', "").lowercase())
                ?: "application/octet-stream"

        /** The unfinished row named by [token], if it still exists and is ours to write. */
        fun reopen(context: Context, token: String?, size: Long): MediaStoreSink? {
            val uri = token?.let(Uri::parse) ?: return null
            val pfd = runCatching { context.contentResolver.openFileDescriptor(uri, "rw") }.getOrNull() ?: return null
            return MediaStoreSink(context.contentResolver, uri, size, pfd)
        }

        /** A new pending row; MediaStore adds " (1)", " (2)", ... on a name clash. */
        fun create(context: Context, name: String, size: Long): MediaStoreSink {
            val values = ContentValues().apply {
                put(MediaStore.MediaColumns.DISPLAY_NAME, name)
                put(MediaStore.MediaColumns.MIME_TYPE, mimeOf(name))
                put(MediaStore.MediaColumns.RELATIVE_PATH, DIR)
                put(MediaStore.MediaColumns.IS_PENDING, 1)
            }
            val uri = context.contentResolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                ?: throw IOException("MediaStore insert failed")
            return MediaStoreSink(context.contentResolver, uri, size, null)
        }
    }
}
