package com.clipbridge.features.clipboard

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import com.clipbridge.BridgeService

/** Share target (ACTION_SEND text/plain): the text is in the intent, no clipboard read needed. */
class ShareTargetActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val text = intent?.getCharSequenceExtra(Intent.EXTRA_TEXT)?.toString()
        if (!text.isNullOrEmpty()) BridgeService.submit(this, text)
        finish()
        overridePendingTransition(0, 0)
    }
}
