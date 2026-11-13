package com.clipbridge.link

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.json.JSONObject

/** What a feature returns for a message it handled: reply content for a request (PROTOCOL.md section 6.2). */
class Reply(val data: JSONObject? = null, val body: ByteArray = ByteArray(0))

/**
 * The peer's reply to a request. [ok] false: the peer is alive but did not handle the message
 * (no such feature, or the feature failed); [data] is then null and [body] empty.
 */
class Response(val ok: Boolean, val data: JSONObject? = null, val body: ByteArray = ByteArray(0)) {
    companion object {
        val FAILED = Response(false)
    }
}

/**
 * Requests and replies (PROTOCOL.md section 6.2), sender side: issues `mid`s, matches incoming
 * replies, and reports each request exactly once: the [Response], or null when no reply came
 * within the timeout or the connection closed first.
 */
class Requests(private val scope: CoroutineScope, private val timeoutMs: Long = TIMEOUT_MS) {
    private class Pending(val onReply: (Response?) -> Unit, val timer: Job)

    private val pending = HashMap<String, Pending>()
    private var next = 0L

    /** @return the `mid` to put in the outgoing header. */
    fun track(onReply: (Response?) -> Unit): String = synchronized(pending) {
        val mid = "m${next++}"
        pending[mid] = Pending(onReply, scope.launch { delay(timeoutMs); resolve(mid, null) })
        mid
    }

    fun resolve(mid: String, response: Response?) {
        val p = synchronized(pending) { pending.remove(mid) } ?: return
        p.timer.cancel()
        p.onReply(response)
    }

    /** A `ch = "link"` message from the peer; replies resolve their request, anything else is ignored. */
    fun onControl(header: JSONObject, body: ByteArray) {
        if (header.optString("type") != TYPE) return
        val mid = header.opt("mid") as? String ?: return
        val ok = header.opt("ok") as? Boolean ?: return
        resolve(mid, if (ok) Response(true, header.opt("data") as? JSONObject, body) else Response.FAILED)
    }

    /** Connection ended: nothing still pending can be answered any more. */
    fun failAll() {
        val all = synchronized(pending) { pending.keys.toList() }
        all.forEach { resolve(it, null) }
    }

    companion object {
        const val TYPE = "reply"
        const val TIMEOUT_MS = 10_000L
        private val MID = Regex("[A-Za-z0-9._-]{1,64}")

        /** Whether [header] carries a `mid` key at all (valid or not). */
        fun hasMid(header: JSONObject): Boolean = header.has("mid")

        /** The request id of a received feature message, or null (absent or invalid). */
        fun requestedMid(header: JSONObject): String? =
            (header.opt("mid") as? String)?.takeIf { MID.matches(it) }

        /** The reply envelope header for request [mid]; data is sent only when [r] is ok. */
        fun replyHeader(mid: String, r: Response): JSONObject {
            val h = JSONObject().put("ch", LINK_CH).put("type", TYPE).put("mid", mid).put("ok", r.ok)
            if (r.ok && r.data != null) h.put("data", r.data)
            return h
        }
    }
}
