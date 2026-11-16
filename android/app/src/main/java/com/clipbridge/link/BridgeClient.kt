package com.clipbridge.link

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.OkHttpClient
import okhttp3.WebSocket
import org.json.JSONObject
import java.util.concurrent.TimeUnit

enum class LinkState { NOT_PAIRED, SEARCHING, CONNECTING, CONNECTED }

data class Status(val state: LinkState, val detail: String = "")

/**
 * Client side of the link: connect loop over the control connection, `hello` exchange, routing by
 * `ch`, requests/replies and file transfer. Knows nothing about features; they register on
 * [router] and send via [LinkSender].
 */
class BridgeClient(
    private val scope: CoroutineScope,
    private val pairingProvider: () -> Pairing?,
    private val deviceName: String,
    private val endpointsProvider: () -> List<Endpoint>,
    private val router: FeatureRouter,
    private val onGoodEndpoint: (Endpoint) -> Unit,
    partials: PartialStore,
    onTransferBusy: (Boolean) -> Unit,
    private val log: (String) -> Unit = {},
    /** Where control sockets come from; [http] unless a test supplies a fake. */
    private val sockets: WebSocket.Factory? = null,
    private val newBackoff: () -> Backoff = { Backoff() },
) : LinkSender {

    private val http = OkHttpClient.Builder()
        .pingInterval(15, TimeUnit.SECONDS)
        .connectTimeout(4, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.SECONDS)
        .build()

    private val _status = MutableStateFlow(Status(LinkState.SEARCHING))
    val status: StateFlow<Status> = _status

    private val requests = Requests(scope)
    private val transfer = Transfer(
        parent = scope, http = http, router = router, store = partials,
        control = { sendEnvelope(it) }, dropLink = { networkLost() }, onBusy = onTransferBusy, log = log,
    )
    private val wake = Channel<Unit>(Channel.CONFLATED)
    @Volatile private var session: SecureSocket? = null
    /** The server, once its `hello` arrived on the current connection; null otherwise. */
    @Volatile private var peer: Peer? = null
    private var job: Job? = null

    fun start() {
        if (job != null) return
        job = scope.launch { loop() }
    }

    fun stop() {
        job?.cancel(); job = null
        session?.close(1000)
    }

    /** New network / new endpoint / new settings: retry now. */
    fun retryNow() { wake.trySend(Unit) }

    /** Settings changed (pairing/host): drop the current connection and reconnect. */
    fun reconnect() { session?.close(1000); retryNow() }

    /** Network gone: the socket is dead even if TCP has not noticed yet. */
    fun networkLost() { session?.cancel() }

    override fun send(peer: Peer?, header: JSONObject, body: ByteArray, onReply: ((Response?) -> Unit)?): Boolean {
        // Track before sending: the reply can arrive before send returns.
        val mid = onReply?.let { requests.track(it) }
        val h = if (mid == null) header else JSONObject(header.toString()).put("mid", mid)
        val sent = this.peer != null && sendEnvelope(h, body)
        if (!sent && mid != null) requests.resolve(mid, null)
        return sent
    }

    override fun sendFile(
        peer: Peer, feature: String, source: FileSource, meta: JSONObject,
        onProgress: (Long) -> Unit, onDone: (Boolean) -> Unit,
    ) = transfer.sendFile(peer, feature, source, meta, onProgress, onDone)

    /** Encrypts one envelope onto the control connection. @return false if not connected or too big. */
    private fun sendEnvelope(header: JSONObject, body: ByteArray = ByteArray(0)): Boolean {
        val plaintext = Envelopes.encode(header, body)
        if (plaintext.size > Envelopes.MAX_PLAINTEXT) { log("skip oversize envelope"); return false }
        return session?.send(plaintext) ?: false
    }

    private suspend fun loop() {
        val backoff = newBackoff()
        while (scope.isActive) {
            val pairing = pairingProvider()
            val eps = endpointsProvider()
            var ok = false
            if (pairing == null) {
                _status.value = Status(LinkState.NOT_PAIRED)
            } else if (eps.isEmpty()) {
                _status.value = Status(LinkState.SEARCHING, "no endpoint")
            } else {
                for (ep in eps) {
                    if (connectOnce(pairing, ep)) { ok = true; break }
                }
                if (!ok) _status.value = Status(LinkState.SEARCHING, "unreachable")
            }
            if (ok) backoff.reset()
            withTimeoutOrNull(backoff.nextMs()) { wake.receive() }
        }
    }

    /** Runs one connection to its end. @return true if the handshake succeeded. */
    private suspend fun connectOnce(pairing: Pairing, ep: Endpoint): Boolean {
        _status.value = Status(LinkState.CONNECTING, ep.toString())
        val s = SecureSocket(
            scope, pairing, ep, SUBPROTOCOL,
            onReady = { onReady(it) }, onPlaintext = { sock, p -> onPlaintext(sock, pairing, p) }, log = log,
        )
        session = s
        try {
            s.open(sockets ?: http)
        } catch (e: IllegalArgumentException) {
            log("bad endpoint"); session = null
            return false
        }
        val authed = s.closed.await()
        session = null
        val p = peer
        peer = null
        requests.failAll()
        if (p != null) {
            transfer.onLinkDown()
            router.peerDisconnected(p)
        }
        if (authed) _status.value = Status(LinkState.SEARCHING, "disconnected")
        return authed
    }

    /** Server MAC checked out: our `hello` is the first frame (PROTOCOL.md 6.1). */
    private fun onReady(s: SecureSocket) {
        onGoodEndpoint(s.endpoint)
        if (!s.send(Envelopes.encode(Hello(deviceName, router.versions()).header()))) s.close(1000)
    }

    private fun onPlaintext(s: SecureSocket, pairing: Pairing, plaintext: ByteArray) {
        val p = peer ?: return onFirstFrame(s, pairing, plaintext)
        when (val r = router.dispatch(p, plaintext)) {
            is Routed.Answer -> answer(r)
            is Routed.LinkControl -> when {
                Hello.isHello(r.header) -> s.close(4400) // a second hello
                r.header.optString("type") == Requests.TYPE -> requests.onControl(r.header, r.body)
                else -> transfer.onControl(p, r.header)
            }
            Routed.Done -> {}
        }
    }

    /** The server's first frame must be a valid `hello`; then the peer exists for features. */
    private fun onFirstFrame(s: SecureSocket, pairing: Pairing, plaintext: ByteArray) {
        val env = try { Envelopes.parse(plaintext) } catch (e: EnvelopeException) { null }
        val hello = env?.let { Hello.parse(it.header, it.body) }
        if (hello == null) { log("invalid hello"); return s.close(4400) }
        val p = Peer(PEER.id, hello.name, hello.features)
        peer = p
        _status.value = Status(LinkState.CONNECTED, s.endpoint.toString())
        transfer.onLinkUp(p, pairing, s.endpoint)
        router.peerConnected(p)
    }

    /** Replies to a request; reply content over the size limit means the feature failed (PROTOCOL.md 6.2). */
    private fun answer(r: Routed.Answer) {
        val res = r.response
        if (!sendEnvelope(Requests.replyHeader(r.mid, res), res.body) && res.ok) {
            sendEnvelope(Requests.replyHeader(r.mid, Response.FAILED))
        }
    }

    companion object {
        const val SUBPROTOCOL = "clipbridge.v1"
        /** The server, for sends queued before it connects; equal to the connected peer (by id). */
        val PEER = Peer("server")
    }
}
