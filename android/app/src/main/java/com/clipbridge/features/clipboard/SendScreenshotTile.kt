package com.clipbridge.features.clipboard

import android.Manifest
import android.app.PendingIntent
import android.content.ContentResolver
import android.content.ContentUris
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.MediaStore
import android.service.quicksettings.Tile
import android.service.quicksettings.TileService
import android.util.Log
import android.widget.Toast
import com.clipbridge.BridgeService
import com.clipbridge.MainActivity

/**
 * Quick Settings tile: one tap sends the newest screenshot as an image clip. The tile stays on
 * ("Sending…") until the PC's delivery receipt arrives, then shows the outcome briefly.
 * [ImageDelivery] shows the toast, so the outcome is visible even after the shade closes.
 */
class SendScreenshotTile : TileService() {
    override fun onStartListening() {
        if (ImageDelivery.listener == null) show(Tile.STATE_INACTIVE, null)
    }

    override fun onClick() {
        if (checkSelfPermission(permission()) != PackageManager.PERMISSION_GRANTED) {
            openApp(); return
        }
        val ctx = applicationContext
        show(Tile.STATE_ACTIVE, "Sending…")
        Thread {
            val uri = newest("Screenshots") ?: newest(null)
            val png = uri?.let { ImageUtil.readPng(ctx, it) }
            Log.i("ClipBridge", "screenshot tile: found=${uri != null} len=${png?.size}")
            if (png != null && png.isNotEmpty()) {
                ImageDelivery.listener = { ok -> ImageDelivery.listener = null; settle(if (ok) "Delivered" else "Not delivered") }
                BridgeService.submitImage(ctx, png)
            } else main.post {
                Toast.makeText(ctx, "No screenshot found", Toast.LENGTH_SHORT).show()
                settle("None found")
            }
        }.start()
    }

    private val main = Handler(Looper.getMainLooper())

    /** Final state: subtitle for 2 s (toasts can hide behind the shade), then back to idle. */
    private fun settle(subtitle: String) {
        show(Tile.STATE_INACTIVE, subtitle)
        main.postDelayed({ show(Tile.STATE_INACTIVE, null) }, 2000)
    }

    private fun show(state: Int, subtitle: String?) {
        qsTile?.apply { this.state = state; this.subtitle = subtitle; updateTile() }
    }

    private fun newest(pathContains: String?): Uri? {
        val args = Bundle().apply {
            if (pathContains != null) {
                putString(ContentResolver.QUERY_ARG_SQL_SELECTION, "${MediaStore.Images.Media.RELATIVE_PATH} LIKE ?")
                putStringArray(ContentResolver.QUERY_ARG_SQL_SELECTION_ARGS, arrayOf("%$pathContains%"))
            }
            putStringArray(ContentResolver.QUERY_ARG_SORT_COLUMNS, arrayOf(MediaStore.Images.Media.DATE_ADDED))
            putInt(ContentResolver.QUERY_ARG_SORT_DIRECTION, ContentResolver.QUERY_SORT_DIRECTION_DESCENDING)
            putInt(ContentResolver.QUERY_ARG_LIMIT, 1)
        }
        return runCatching {
            contentResolver.query(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, arrayOf(MediaStore.Images.Media._ID), args, null)?.use {
                if (it.moveToFirst()) ContentUris.withAppendedId(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, it.getLong(0)) else null
            }
        }.getOrNull()
    }

    private fun openApp() {
        val intent = Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        if (Build.VERSION.SDK_INT >= 34) {
            startActivityAndCollapse(
                PendingIntent.getActivity(this, 0, intent, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
            )
        } else {
            @Suppress("DEPRECATION", "StartActivityAndCollapseDeprecated")
            startActivityAndCollapse(intent)
        }
    }

    companion object {
        fun permission(): String =
            if (Build.VERSION.SDK_INT >= 33) Manifest.permission.READ_MEDIA_IMAGES else Manifest.permission.READ_EXTERNAL_STORAGE
    }
}
