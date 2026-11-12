package com.clipbridge.features.clipboard

import android.content.Context
import android.content.Intent

/**
 * Seam for "read the phone clipboard now and send it". Android 10+ only lets the
 * focused app read the clipboard, so the implementation shows a transparent activity.
 * Sources (QS tile, notification action, a future AccessibilityService auto-trigger)
 * all go through this; adding one needs no change elsewhere.
 */
fun interface ClipTrigger {
    fun requestRead()
}

class ReaderActivityTrigger(private val context: Context) : ClipTrigger {
    override fun requestRead() {
        context.startActivity(readerIntent(context))
    }

    companion object {
        fun readerIntent(context: Context): Intent =
            Intent(context, ClipReadActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    }
}
