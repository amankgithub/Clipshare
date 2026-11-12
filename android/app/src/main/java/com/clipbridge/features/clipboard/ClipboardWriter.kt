package com.clipbridge.features.clipboard

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context

/** Writes text to the system clipboard (background writes are allowed). */
class ClipboardWriter(context: Context) {
    private val cm = context.getSystemService(ClipboardManager::class.java)
    fun write(text: String) { cm.setPrimaryClip(ClipData.newPlainText("ClipBridge", text)) }
}
