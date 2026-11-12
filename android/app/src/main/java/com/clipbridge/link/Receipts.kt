package com.clipbridge.link

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.json.JSONObject

/**
 * Delivery receipts (PROTOCOL.md section 6.1), sender side: issues `mid`s, matches incoming acks,
 * and reports each tracked message exactly once (true = peer handled it; false = timeout or link lost).
 */
class Receipts(private val scope: CoroutineScope, private val timeoutMs: Long = TIMEOUT_MS) {
    private class Pending(val onDelivered: (Boolean) -> Unit, val timer: Job)

    private val pending = HashMap<String, Pending>()
    private var next = 0L

    /** @return the `mid` to put in the outgoing header. */
    fun track(onDelivered: (Boolean) -> Unit): String = synchronized(pending) {
        val mid = "m${next++}"
        pending[mid] = Pending(onDelivered, scope.launch { delay(timeoutMs); resolve(mid, false) })
        mid
    }

    fun resolve(mid: String, delivered: Boolean) {
        val p = synchronized(pending) { pending.remove(mid) } ?: return
        p.timer.cancel()
        p.onDelivered(delivered)
    }

    /** A `ch = "link"` message from the peer. */
    fun onControl(header: JSONObject) {
        if (header.optString("type") == "ack") resolve(header.optString("mid"), true)
    }

    /** Connection ended: nothing still pending can be acknowledged any more. */
    fun failAll() {
        val all = synchronized(pending) { pending.keys.toList() }
        all.forEach { resolve(it, false) }
    }

    companion object {
        const val LINK_CH = "link"
        const val TIMEOUT_MS = 10_000L
        private val MID = Regex("[A-Za-z0-9._-]{1,64}")

        /** The receipt id a received feature message asks for, or null (absent or invalid). */
        fun requestedMid(header: JSONObject): String? =
            (header.opt("mid") as? String)?.takeIf { MID.matches(it) }

        fun ack(mid: String): JSONObject = JSONObject().put("ch", LINK_CH).put("type", "ack").put("mid", mid)
    }
}
