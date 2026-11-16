package com.clipbridge

import com.clipbridge.link.B64
import com.clipbridge.link.Crypto
import com.clipbridge.link.Envelope
import com.clipbridge.link.Envelopes
import com.clipbridge.link.FrameCipher
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.launch
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.io.IOException

/**
 * Test double for the network: a [WebSocket.Factory] whose sockets are driven by a [ServerScript]
 * (a hand-written PC). Everything the server does runs as a coroutine on [scope], so with a test
 * dispatcher it happens in virtual time and never re-enters the client's callbacks.
 */
class FakeNet(
    private val scope: CoroutineScope,
    private val now: () -> Long,
    var script: (host: String) -> ServerScript,
) : WebSocket.Factory {
    class Dial(val timeMs: Long, val host: String, val port: Int)

    val dials = mutableListOf<Dial>()
    val sockets = mutableListOf<FakeSocket>()

    override fun newWebSocket(request: Request, listener: WebSocketListener): WebSocket {
        val ws = FakeSocket(request, listener, scope)
        dials += Dial(now(), request.url.host, request.url.port)
        sockets += ws
        val s = script(request.url.host)
        ws.server = s
        scope.launch { s.onDial(ws) }
        return ws
    }
}

class FakeSocket(private val req: Request, val listener: WebSocketListener, private val scope: CoroutineScope) : WebSocket {
    lateinit var server: ServerScript
    var closeCode: Int? = null
    var cancelled = false
    /** False: the peer never completes a close handshake started by the client. */
    var ackClose = true
    private var finished = false
    val sentText = mutableListOf<String>()

    override fun request() = req
    override fun queueSize() = 0L

    override fun send(text: String): Boolean {
        if (closeCode != null || cancelled) return false
        sentText += text
        scope.launch { server.onText(this@FakeSocket, text) }
        return true
    }

    override fun send(bytes: ByteString): Boolean {
        if (closeCode != null || cancelled) return false
        val b = bytes.toByteArray()
        scope.launch { server.onBinary(this@FakeSocket, b) }
        return true
    }

    override fun close(code: Int, reason: String?): Boolean {
        if (closeCode != null || cancelled) return false
        closeCode = code
        if (ackClose) scope.launch { finish { listener.onClosed(this@FakeSocket, code, "") } }
        return true
    }

    override fun cancel() {
        if (cancelled) return
        cancelled = true
        scope.launch { finish { listener.onFailure(this@FakeSocket, IOException("canceled"), null) } }
    }

    private fun finish(f: () -> Unit) { if (!finished) { finished = true; f() } }

    fun response(subprotocol: String): Response =
        Response.Builder().request(req).protocol(Protocol.HTTP_1_1).code(101).message("Switching Protocols")
            .header("Sec-WebSocket-Protocol", subprotocol).build()

    // -- what the server does to the client ---------------------------------------------------

    fun deliverText(t: String) { if (!finished) listener.onMessage(this, t) }
    fun deliverBinary(b: ByteArray) { if (!finished) listener.onMessage(this, b.toByteString()) }
    fun fail() = finish { listener.onFailure(this, IOException("refused"), null) }

    /** The server closes the connection with [code]. */
    fun serverClose(code: Int) = finish {
        listener.onClosing(this, code, "")
        listener.onClosed(this, code, "")
    }
}

open class ServerScript {
    open fun onDial(ws: FakeSocket) {}
    open fun onText(ws: FakeSocket, text: String) {}
    open fun onBinary(ws: FakeSocket, bytes: ByteArray) {}
}

/** Nothing listens on this host. */
class DeadServer : ServerScript() {
    override fun onDial(ws: FakeSocket) = ws.fail()
}

/**
 * A PC that follows PROTOCOL.md section 3 and then speaks encrypted envelopes. Options are plain
 * vars so a test can break one step; [received] holds the client's decrypted envelopes.
 */
open class HandshakeServer(
    private val psk: ByteArray,
    private val device: String = "dev1",
) : ServerScript() {
    var protocol = "clipbridge.v1"
    var autoChallenge = true
    var autoAuthOk = true
    var corruptMac = false
    /** The server's first frame once the client is authenticated; null: never send it. */
    var hello: JSONObject? = com.clipbridge.link.Hello("PC", mapOf("echo" to 1)).header()
    var clientAuthValid: Boolean? = null

    val received = mutableListOf<Envelope>()
    lateinit var ws: FakeSocket
    private val sn = ByteArray(16) { (it + 3).toByte() }
    private var cn = ByteArray(0)
    private var cipher: FrameCipher? = null

    override fun onDial(ws: FakeSocket) {
        this.ws = ws
        ws.listener.onOpen(ws, ws.response(protocol))
        if (autoChallenge) challenge()
    }

    fun challenge(type: String = "challenge", v: Int = 1, sn: String = B64.encode(this.sn)) {
        ws.deliverText(JSONObject().put("type", type).put("v", v).put("sn", sn).toString())
    }

    override fun onText(ws: FakeSocket, text: String) {
        if (cipher != null) return
        val m = JSONObject(text)
        cn = B64.decode(m.getString("cn"), 16)!!
        val mac = B64.decode(m.getString("mac"), 32)!!
        clientAuthValid = m.getString("device") == device &&
            Crypto.constantTimeEquals(mac, Crypto.clientMac(psk, sn, cn, device))
        if (!autoAuthOk) return
        val smac = Crypto.serverMac(psk, sn, cn)
        if (corruptMac) smac[0] = (smac[0].toInt() xor 1).toByte()
        val (c2s, s2c) = Crypto.sessionKeys(psk, sn, cn)
        cipher = FrameCipher(s2c, c2s)
        ws.deliverText(JSONObject().put("type", "auth_ok").put("mac", B64.encode(smac)).toString())
        hello?.let { send(it) }
    }

    override fun onBinary(ws: FakeSocket, bytes: ByteArray) {
        received += Envelopes.parse(cipher!!.decrypt(bytes))
    }

    fun send(header: JSONObject, body: ByteArray = ByteArray(0)) =
        ws.deliverBinary(cipher!!.encrypt(Envelopes.encode(header, body)))
}

/** Signal that a known bug showed up: thrown only by [bugCheck]. Not an AssertionError, so nothing else can be mistaken for it. */
class BugReproduced(message: String) : Error(message)

/** The final check of a known bug, inside [expectBug]: [correct] is the behaviour once the bug is fixed. */
fun bugCheck(correct: Boolean, message: String) {
    if (!correct) throw BugReproduced(message)
}

/**
 * Strict expected failure (no `@Ignore`): [block] ends in a [bugCheck] of the correct behaviour, which fails today.
 * Put preconditions outside the block; any other failure inside it propagates. When the bug is fixed the check
 * passes and this fails, telling you to drop the wrapper.
 */
fun expectBug(description: String, block: () -> Unit) {
    try { block() } catch (e: BugReproduced) { return }
    throw AssertionError("bug: $description now behaves correctly; remove expectBug")
}
