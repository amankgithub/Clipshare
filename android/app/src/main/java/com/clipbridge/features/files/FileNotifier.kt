package com.clipbridge.features.files

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.ClipData
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.widget.Toast

/** Notifications and toasts for file transfers: progress, then "Received" with open + share. */
class FileNotifier(private val context: Context) {
    private val nm = context.getSystemService(NotificationManager::class.java)
    private val main = Handler(Looper.getMainLooper())

    init {
        nm.createNotificationChannel(NotificationChannel(PROGRESS, "File transfers", NotificationManager.IMPORTANCE_LOW).apply {
            setShowBadge(false); setSound(null, null); enableVibration(false)
        })
        nm.createNotificationChannel(NotificationChannel(RECEIVED, "Received files", NotificationManager.IMPORTANCE_DEFAULT))
    }

    private val lastUpdate = HashMap<String, Long>()

    private fun nid(id: String): Int = id.hashCode()

    /** Rate-limited to two updates a second per transfer (the system drops faster ones). */
    fun progress(id: String, title: String, done: Long, total: Long, upload: Boolean) {
        val now = System.currentTimeMillis()
        synchronized(lastUpdate) {
            if (done < total && now - (lastUpdate[id] ?: 0) < 500) return
            lastUpdate[id] = now
        }
        val pct = if (total > 0) (done * 100 / total).toInt() else 100
        val n = Notification.Builder(context, PROGRESS)
            .setSmallIcon(if (upload) android.R.drawable.stat_sys_upload else android.R.drawable.stat_sys_download)
            .setContentTitle(title)
            .setContentText("$pct % of ${human(total)}")
            .setProgress(100, pct, false)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .build()
        nm.notify(nid(id), n)
    }

    fun received(id: String, name: String, uri: Uri, mime: String) {
        val view = Intent(Intent.ACTION_VIEW).setDataAndType(uri, mime)
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        val send = Intent(Intent.ACTION_SEND).setType(mime).putExtra(Intent.EXTRA_STREAM, uri)
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            .apply { clipData = ClipData.newRawUri(name, uri) }
        val share = Intent.createChooser(send, name).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        val flags = PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        val n = Notification.Builder(context, RECEIVED)
            .setSmallIcon(android.R.drawable.stat_sys_download_done)
            .setContentTitle("Received $name")
            .setContentText("Downloads/ClipShare")
            .setContentIntent(PendingIntent.getActivity(context, nid(id), view, flags))
            .addAction(Notification.Action.Builder(null, "Share", PendingIntent.getActivity(context, nid(id) + 1, share, flags)).build())
            .setAutoCancel(true)
            .build()
        synchronized(lastUpdate) { lastUpdate.remove(id) }
        nm.notify(nid(id), n)
    }

    fun clear(id: String) {
        synchronized(lastUpdate) { lastUpdate.remove(id) }
        nm.cancel(nid(id))
    }

    fun failed(id: String, text: String) {
        clear(id)
        toast(text)
    }

    fun toast(text: String) = main.post { Toast.makeText(context, text, Toast.LENGTH_SHORT).show() }

    private fun human(b: Long): String = when {
        b >= 1L shl 30 -> "%.1f GB".format(b / (1L shl 30).toDouble())
        b >= 1L shl 20 -> "%.1f MB".format(b / (1L shl 20).toDouble())
        b >= 1L shl 10 -> "%.0f KB".format(b / (1L shl 10).toDouble())
        else -> "$b B"
    }

    private companion object {
        const val PROGRESS = "transfers"
        const val RECEIVED = "received"
    }
}
