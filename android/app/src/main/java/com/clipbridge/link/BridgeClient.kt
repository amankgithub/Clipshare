package com.clipbridge.link

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONException
import org.json.JSONObject
import java.security.SecureRandom
import java.util.concurrent.TimeUnit

enum class LinkState { NOT_PAIRED, SEARCHING, CONNECTING, CONNECTED }

data class Status(val state: LinkState, val detail: String = "")

private enum class Phase { CHALLENGE, AUTH_OK, READY }

/**
 * Client side of the link: connect loop, handshake, encrypted framing, routing by `ch`.
 * Knows nothing about features; they register on [router] and send via [LinkSender].
 */
class BridgeClient(
    private val scope: CoroutineScope,
    private val deviceId: String,
    private val pskProvider: () -> ByteArray?,
    private val endpointsProvider: () -> List<Endpoint>,
    private val router: FeatureRouter,
    private val onGoodEndpoint: (Endpoint) -> Unit,
    private val log: (String) -> Unit = {},
) : LinkSender {

    private val http = OkHttpClient.Builder()
        .pingInterval(15, TimeUnit.SECONDS)
        .connectTimeout(4, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.SECONDS)
        .build()

    private val _status = MutableStateFlow(Status(LinkState.SEARCHING))
    val status: StateFlow<Status> = _status

    private val receipts = Receipts(scope)
    private val wake = Channel<Unit>(Channel.CONFLATED)
    private val sendLock = Any()
    @Volatile private var session: Session? = null
    private var job: Job? = null

    fun start() {
        if (job != null) return
        job = scope.launch { loop() }
    }

    fun stop() {
        job?.cancel(); job = null
        session?.ws?.close(1000, null)
    }

    /** New network / new endpoint / new settings: retry now. */
    fun retryNow() { wake.trySend(Unit) }

    /** Settings changed (PSK/host): drop the current connection and reconnect. */
    fun reconnect() { session?.ws?.close(1000, null); retryNow() }

    /** Network gone: the socket is dead even if TCP has not noticed yet. */
    fun networkLost() { session?.ws?.cancel() }

    override fun send(peer: Peer?, header: JSONObject, body: ByteArray, onDelivered: ((Boolean) -> Unit)?): Boolean {
        // Track before sending: the ack can arrive before sendEncrypted returns.
        val mid = onDelivered?.let { receipts.track(it) }
        val h = if (mid == null) header else JSONObject(header.toString()).put("mid", mid)
        val plaintext = Envelopes.encode(h, body)
        val sent = if (plaintext.size > Envelopes.MAX_PLAINTEXT) {
            log("skip oversize envelope"); false
        } else synchronized(sendLock) { session?.sendEncrypted(plaintext) ?: false }
        if (!sent && mid != null) receipts.resolve(mid, false)
        return sent
    }

    private suspend fun loop() {
        val backoff = Backoff()
        while (scope.isActive) {
            val psk = pskProvider()
            val eps = endpointsProvider()
            var ok = false
            if (psk == null) {
                _status.value = Status(LinkState.NOT_PAIRED)
            } else if (eps.isEmpty()) {
                _status.value = Status(LinkState.SEARCHING, "no endpoint")
            } else {
                for (ep in eps) {
                    if (connectOnce(psk, ep)) { ok = true; break }
                }
                if (!ok) _status.value = Status(LinkState.SEARCHING, "unreachable")
            }
            if (ok) backoff.reset()
            withTimeoutOrNull(backoff.nextMs()) { wake.receive() }
        }
    }

    /** Runs one connection to its end. @return true if the handshake succeeded. */
    private suspend fun connectOnce(psk: ByteArray, ep: Endpoint): Boolean {
        _status.value = Status(LinkState.CONNECTING, ep.toString())
        val s = Session(psk, ep)
        session = s
        val req = Request.Builder().url("ws://${ep.host}:${ep.port}/")
            .header("Sec-WebSocket-Protocol", SUBPROTOCOL).build()
        s.ws = http.newWebSocket(req, s)
        val authed = s.closed.await()
        synchronized(sendLock) { session = null }
        receipts.failAll()
        if (authed) {
            router.peerDisconnected(PEER)
            _status.value = Status(LinkState.SEARCHING, "disconnected")
        }
        return authed
    }

    private inner class Session(private val psk: ByteArray, private val ep: Endpoint) : WebSocketListener() {
        lateinit var ws: WebSocket
        val closed = CompletableDeferred<Boolean>()
        @Volatile private var phase = Phase.CHALLENGE
        private var sn = ByteArray(0)
        private var cn = ByteArray(0)
        private var cipher: FrameCipher? = null
        private var timer: Job? = null

        fun sendEncrypted(plaintext: ByteArray): Boolean {
            if (phase != Phase.READY) return false
            val c = cipher ?: return false
            return try { ws.send(c.encrypt(plaintext).toByteString()) } catch (e: FrameException) { ws.close(1000, null); false }
        }

        override fun onOpen(webSocket: WebSocket, response: Response) {
            if (response.header("Sec-WebSocket-Protocol") != SUBPROTOCOL) fail(4400)
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            try {
                when (phase) {
                    Phase.CHALLENGE -> onChallenge(JSONObject(text))
                    Phase.AUTH_OK -> onAuthOk(JSONObject(text))
                    Phase.READY -> fail(4400)
                }
            } catch (e: JSONException) { fail(4400) }
        }

        private fun onChallenge(m: JSONObject) {
            if (m.optString("type") != "challenge") return fail(4400)
            if (m.optInt("v", -1) != 1) return fail(4400)
            sn = B64.decode(m.optString("sn"), 16) ?: return fail(4400)
            cn = ByteArray(16).also { SecureRandom().nextBytes(it) }
            val mac = Crypto.clientMac(psk, sn, cn, deviceId)
            val auth = JSONObject().put("type", "auth").put("v", 1).put("cn", B64.encode(cn))
                .put("device", deviceId).put("mac", B64.encode(mac))
            phase = Phase.AUTH_OK
            ws.send(auth.toString())
            timer = scope.launch { delay(10_000); if (phase != Phase.READY) fail(4408) }
        }

        private fun onAuthOk(m: JSONObject) {
            if (m.optString("type") != "auth_ok") return fail(4400)
            val mac = B64.decode(m.optString("mac"), 32) ?: return fail(4400)
            if (!Crypto.constantTimeEquals(mac, Crypto.serverMac(psk, sn, cn))) return fail(4401)
            val (c2s, s2c) = Crypto.sessionKeys(psk, sn, cn)
            cipher = FrameCipher(c2s, s2c)
            timer?.cancel()
            phase = Phase.READY
            onGoodEndpoint(ep)
            _status.value = Status(LinkState.CONNECTED, ep.toString())
            router.peerConnected(PEER)
        }

        override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
            if (phase != Phase.READY) return fail(4400)
            val plaintext = try { cipher!!.decrypt(bytes.toByteArray()) } catch (e: FrameException) { return fail(4402) }
            when (val r = router.dispatch(PEER, plaintext)) {
                is Routed.Handled -> r.mid?.let { send(PEER, Receipts.ack(it)) }
                is Routed.LinkControl -> receipts.onControl(r.header)
                Routed.Dropped -> {}
            }
        }

        private fun fail(code: Int) { ws.close(code, null) }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) { webSocket.close(code, null) }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) = finish()

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            log("ws failure: ${t.javaClass.simpleName}")
            finish()
        }

        private fun finish() { timer?.cancel(); closed.complete(phase == Phase.READY) }
    }

    companion object {
        const val SUBPROTOCOL = "clipbridge.v1"
        val PEER = Peer("server")
    }
}
