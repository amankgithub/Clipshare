package com.clipbridge.debug

import android.app.Activity
import android.net.Uri
import android.os.Bundle
import android.util.Log
import com.clipbridge.features.clipboard.sendUris
import java.io.File

/**
 * Debug-only test hook: shares the named files (relative to `getExternalFilesDir(null)/e2e/`) as one share,
 * through the same path as the share sheet. `am start -n com.clipbridge/.debug.E2eShareActivity --esa names a.jpg,b.pdf`
 */
class E2eShareActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val dir = File(getExternalFilesDir(null), "e2e")
        val names = intent.getStringArrayExtra("names")?.toList().orEmpty()
        val files = names.map { it to File(dir, it) }
        val missing = files.firstOrNull { (n, f) -> n.contains('/') || n.contains("\\") || !f.isFile }
        if (names.isEmpty() || missing != null) {
            Log.i("ClipBridge", "e2e share missing ${missing?.first ?: "(none)"}")
        } else {
            Log.i("ClipBridge", "e2e share n=${files.size} " + files.joinToString(" ") { (n, f) -> "$n:${f.length()}" })
            sendUris(this, files.map { Uri.fromFile(it.second) }, null)
        }
        finish()
        overridePendingTransition(0, 0)
    }
}
