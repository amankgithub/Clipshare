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
import org.json.JSONObject
import java.util.concurrent.TimeUnit

enum class LinkState { NOT_PAIRED, SEARCHING, CONNECTING, CONNECTED }

data class Status(val state: LinkState, val detail: String = "")

/**
 * Client side of the link: connect loop over the control connection, routing by `ch`, delivery
 * receipts and file transfer. Knows nothing about features; they register on [router] and send via [LinkSender].
 */
class BridgeClient(
    private val scope: CoroutineScope,
    private val deviceId: String,
    private val pskProvider: () -> ByteArray?,
    private val endpointsProvider: () -> List<Endpoint>,
    private val router: FeatureRouter,
    private val onGoodEndpoint: (Endpoint) -> Unit,
    partials: PartialStore,
    onTransferBusy: (Boolean) -> Unit,
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
    private val transfer = Transfer(
        parent = scope, http = http, deviceId = deviceId, router = router, store = partials,
        control = { send(PEER, it) }, dropLink = { networkLost() }, onBusy = onTransferBusy, log = log,
    )
    private val wake = Channel<Unit>(Channel.CONFLATED)
    @Volatile private var session: SecureSocket? = null
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

    /** Settings changed (PSK/host): drop the current connection and reconnect. */
    fun reconnect() { session?.close(1000); retryNow() }

    /** Network gone: the socket is dead even if TCP has not noticed yet. */
    fun networkLost() { session?.cancel() }

    override fun send(peer: Peer?, header: JSONObject, body: ByteArray, onDelivered: ((Boolean) -> Unit)?): Boolean {
        // Track before sending: the ack can arrive before send returns.
        val mid = onDelivered?.let { receipts.track(it) }
        val h = if (mid == null) header else JSONObject(header.toString()).put("mid", mid)
        val plaintext = Envelopes.encode(h, body)
        val sent = if (plaintext.size > Envelopes.MAX_PLAINTEXT) {
            log("skip oversize envelope"); false
        } else session?.send(plaintext) ?: false
        if (!sent && mid != null) receipts.resolve(mid, false)
        return sent
    }

    override fun sendFile(
        peer: Peer, feature: String, source: FileSource, meta: JSONObject,
        onProgress: (Long) -> Unit, onDone: (Boolean) -> Unit,
    ) = transfer.sendFile(peer, feature, source, meta, onProgress, onDone)

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
        val s = SecureSocket(scope, psk, deviceId, ep, SUBPROTOCOL, onReady = { onReady(it, psk) }, onPlaintext = { _, p -> onPlaintext(p) }, log = log)
        session = s
        s.open(http)
        val authed = s.closed.await()
        session = null
        receipts.failAll()
        if (authed) {
            transfer.onLinkDown()
            router.peerDisconnected(PEER)
            _status.value = Status(LinkState.SEARCHING, "disconnected")
        }
        return authed
    }

    private fun onReady(s: SecureSocket, psk: ByteArray) {
        onGoodEndpoint(s.endpoint)
        _status.value = Status(LinkState.CONNECTED, s.endpoint.toString())
        transfer.onLinkUp(PEER, psk, s.endpoint)
        router.peerConnected(PEER)
    }

    private fun onPlaintext(plaintext: ByteArray) {
        when (val r = router.dispatch(PEER, plaintext)) {
            is Routed.Handled -> r.mid?.let { send(PEER, Receipts.ack(it)) }
            is Routed.LinkControl -> { receipts.onControl(r.header); transfer.onControl(PEER, r.header) }
            Routed.Dropped -> {}
        }
    }

    companion object {
        const val SUBPROTOCOL = "clipbridge.v1"
        val PEER = Peer("server")
    }
}
