package com.clipbridge.features.clipboard

import android.content.Context
import android.os.Handler
import android.os.Looper
import android.widget.Toast
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import java.util.concurrent.atomic.AtomicBoolean

/**
 * User-visible outcome of an explicit image send (tile, share target): one toast per send, plus an
 * optional [listener] (the screenshot tile) that mirrors it on its subtitle. Same process only.
 */
object ImageDelivery {
    /** Overall wait, covering time held offline plus the link's receipt timeout. */
    private const val DEADLINE_MS = 20_000L

    @Volatile var listener: ((Boolean) -> Unit)? = null

    private val main = Handler(Looper.getMainLooper())

    /** @return a callback for [ClipboardFeature.onLocalImage] that reports once, or "not delivered" at the deadline. */
    fun reporter(context: Context, scope: CoroutineScope): (Boolean) -> Unit {
        val done = AtomicBoolean(false)
        val report: (Boolean) -> Unit = { ok ->
            if (done.compareAndSet(false, true)) main.post {
                Toast.makeText(context, if (ok) "Image delivered to PC" else "Image not delivered to PC", Toast.LENGTH_SHORT).show()
                listener?.invoke(ok)
            }
        }
        scope.launch { delay(DEADLINE_MS); report(false) }
        return report
    }
}
