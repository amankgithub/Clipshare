package com.clipbridge.link

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONException
import org.json.JSONObject
import java.security.SecureRandom

private enum class Phase { CHALLENGE, AUTH_OK, READY }

/**
 * One client WebSocket to the server: handshake (PROTOCOL.md section 3), session keys, encrypted
 * frames. Used for the control connection and for data connections (they differ only in [subprotocol]).
 * [onReady] and [onPlaintext] run on OkHttp's reader thread; [closed] completes with whether the
 * handshake had succeeded.
 */
class SecureSocket(
    private val scope: CoroutineScope,
    private val pairing: Pairing,
    val endpoint: Endpoint,
    private val subprotocol: String,
    private val onReady: (SecureSocket) -> Unit,
    private val onPlaintext: (SecureSocket, ByteArray) -> Unit,
    private val log: (String) -> Unit = {},
) : WebSocketListener() {
    val closed = CompletableDeferred<Boolean>()
    @Volatile private var phase = Phase.CHALLENGE
    private lateinit var ws: WebSocket
    private var sn = ByteArray(0)
    private var cn = ByteArray(0)
    private var cipher: FrameCipher? = null
    private var timer: Job? = null
    private val sendLock = Any()

    val isReady: Boolean get() = phase == Phase.READY && !closed.isCompleted

    fun open(http: WebSocket.Factory): SecureSocket {
        val req = Request.Builder().url("ws://${endpoint.host}:${endpoint.port}/")
            .header("Sec-WebSocket-Protocol", subprotocol).build()
        ws = http.newWebSocket(req, this)
        // Covers TCP, upgrade and handshake: a peer that never answers must not hold the endpoint list.
        // Close 4408 (PROTOCOL.md section 3); cancel if the close handshake does not finish promptly.
        timer = scope.launch {
            delay(HANDSHAKE_TIMEOUT_MS)
            if (phase == Phase.READY) return@launch
            close(4408)
            delay(CLOSE_GRACE_MS)
            if (!closed.isCompleted) this@SecureSocket.cancel()
        }
        return this
    }

    /** Encrypts and queues one envelope. Encrypt and enqueue stay in one order (frame counters). */
    fun send(plaintext: ByteArray): Boolean = synchronized(sendLock) {
        if (phase != Phase.READY) return false
        val c = cipher ?: return false
        try { ws.send(c.encrypt(plaintext).toByteString()) } catch (e: FrameException) { ws.close(1000, null); false }
    }

    /** Bytes queued in OkHttp but not yet written to the socket (for send backpressure). */
    fun queueSize(): Long = ws.queueSize()

    fun close(code: Int) { ws.close(code, null) }

    /** Drop at once (network gone; TCP may not have noticed). */
    fun cancel() { ws.cancel() }

    override fun onOpen(webSocket: WebSocket, response: Response) {
        ws = webSocket
        if (response.header("Sec-WebSocket-Protocol") != subprotocol) close(4400)
    }

    override fun onMessage(webSocket: WebSocket, text: String) {
        if (text.length > MAX_HANDSHAKE_TEXT) return close(4400)
        try {
            when (phase) {
                Phase.CHALLENGE -> onChallenge(JSONObject(text))
                Phase.AUTH_OK -> onAuthOk(JSONObject(text))
                Phase.READY -> close(4400)
            }
        } catch (e: JSONException) { close(4400) }
    }

    private fun onChallenge(m: JSONObject) {
        if (m.optString("type") != "challenge") return close(4400)
        if (m.optInt("v", -1) != 1) return close(4400)
        sn = B64.decode(m.optString("sn"), 16) ?: return close(4400)
        cn = ByteArray(16).also { SecureRandom().nextBytes(it) }
        val mac = Crypto.clientMac(pairing.psk, sn, cn, pairing.device)
        val auth = JSONObject().put("type", "auth").put("v", 1).put("cn", B64.encode(cn))
            .put("device", pairing.device).put("mac", B64.encode(mac))
        phase = Phase.AUTH_OK
        ws.send(auth.toString())
    }

    private fun onAuthOk(m: JSONObject) {
        if (m.optString("type") != "auth_ok") return close(4400)
        val mac = B64.decode(m.optString("mac"), 32) ?: return close(4400)
        if (!Crypto.constantTimeEquals(mac, Crypto.serverMac(pairing.psk, sn, cn))) return close(4401)
        val (c2s, s2c) = Crypto.sessionKeys(pairing.psk, sn, cn)
        cipher = FrameCipher(c2s, s2c)
        timer?.cancel()
        phase = Phase.READY
        onReady(this)
    }

    override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
        if (phase != Phase.READY || bytes.size > MAX_FRAME) return close(4400)
        val plaintext = try { cipher!!.decrypt(bytes.toByteArray()) } catch (e: FrameException) { return close(4402) }
        onPlaintext(this, plaintext)
    }

    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) { webSocket.close(code, null) }

    override fun onClosed(webSocket: WebSocket, code: Int, reason: String) = finish()

    override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
        log("$subprotocol failure: ${t.javaClass.simpleName}")
        finish()
    }

    private fun finish() { timer?.cancel(); closed.complete(phase == Phase.READY) }

    private companion object {
        const val HANDSHAKE_TIMEOUT_MS = 10_000L
        /** How long a timed-out handshake waits for the 4408 close to complete before dropping the socket. */
        const val CLOSE_GRACE_MS = 1_000L
        const val MAX_HANDSHAKE_TEXT = 4096
        /** Largest plaintext envelope plus frame overhead (counter, tag) with headroom. */
        const val MAX_FRAME = Envelopes.MAX_PLAINTEXT + 1024
    }
}
