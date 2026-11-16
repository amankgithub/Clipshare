package com.clipbridge.interop

import com.clipbridge.bugCheck
import com.clipbridge.expectBug
import com.clipbridge.features.clipboard.ClipSink
import com.clipbridge.features.clipboard.ClipboardFeature
import com.clipbridge.link.Backoff
import com.clipbridge.link.BridgeClient
import com.clipbridge.link.Endpoint
import com.clipbridge.link.Feature
import com.clipbridge.link.FeatureRouter
import com.clipbridge.link.LinkState
import com.clipbridge.link.PartialStore
import com.clipbridge.link.Pairing
import com.clipbridge.link.Peer
import com.clipbridge.link.Reply
import com.clipbridge.link.Response
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.net.ServerSocket
import java.nio.file.Files
import java.security.MessageDigest
import java.util.concurrent.CopyOnWriteArrayList

/**
 * The real Kotlin link (OkHttp, SecureSocket, BridgeClient) against the real Python server on
 * 127.0.0.1, in real time. The server is the process in `windows/tests/interop_server.py`.
 */
class InteropTest {
    private val psk = ByteArray(32) { (it + 1).toByte() }
    private val device = "phone-1"
    private var server: PythonServer? = null
    private var phone: Phone? = null

    @Before fun needsPython() { PythonServer.requirePython() }

    @After fun cleanUp() {
        phone?.close()
        server?.kill()
    }

    private class Sink : ClipSink {
        val texts = CopyOnWriteArrayList<String>()
        override fun write(text: String) { texts += text }
        override fun writeImage(png: ByteArray) {}
    }

    /** Records the server's [Peer] (its hello) when the link comes up. */
    private class Probe : Feature {
        override val name = "probe"
        override val version = 1
        @Volatile var peer: Peer? = null
        override fun onPeerConnected(peer: Peer) { this.peer = peer }
        override fun onPeerDisconnected(peer: Peer) { this.peer = null }
        override fun onMessage(peer: Peer, header: JSONObject, body: ByteArray): Reply? = null
    }

    private inner class Phone(
        pairing: Pairing?,
        endpoints: List<Endpoint>,
        backoff: () -> Backoff,
    ) {
        @Volatile var pairing = pairing
        @Volatile var endpoints = endpoints
        val scope = CoroutineScope(Dispatchers.Default + SupervisorJob())
        val sink = Sink()
        val probe = Probe()
        val router = FeatureRouter()
        val clipboard: ClipboardFeature
        val client: BridgeClient
        val states = CopyOnWriteArrayList<Pair<Long, LinkState>>()
        private val tmp = Files.createTempDirectory("cb-interop").toFile()

        init {
            client = BridgeClient(
                scope, { this.pairing }, "Phone", { this.endpoints }, router, {},
                PartialStore(tmp), {},
                newBackoff = backoff,
            )
            clipboard = ClipboardFeature(client, sink)
            router.register(clipboard)
            router.register(probe)
            scope.launch { client.status.collect { states += System.currentTimeMillis() to it.state } }
            client.start()
        }

        val state get() = client.status.value.state
        fun close() { client.stop(); scope.cancel(); tmp.deleteRecursively() }
    }

    // -- helpers ----------------------------------------------------------------------------------

    private fun serve(noHello: Boolean = false, kickOnConnect: Int? = null) =
        PythonServer.launch(mapOf(device to psk), noHello, kickOnConnect).also { server = it }

    private fun phoneFor(
        s: PythonServer,
        pairing: Pairing? = Pairing(device, psk),
        endpoints: List<Endpoint> = listOf(Endpoint("127.0.0.1", s.port)),
        backoff: () -> Backoff = { Backoff { -0.5 } }, // factor 0.6: 0.6 s, 1.2 s, ... cap 6 s
    ) = Phone(pairing, endpoints, backoff).also { phone = it }

    /** True as soon as [cond] holds, false after [timeoutMs]. */
    private fun poll(timeoutMs: Long, cond: () -> Boolean): Boolean {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (!cond()) {
            if (System.currentTimeMillis() > deadline) return false
            Thread.sleep(10)
        }
        return true
    }

    private fun waitFor(what: String, timeoutMs: Long = 8_000, cond: () -> Boolean) {
        if (!poll(timeoutMs, cond)) throw AssertionError("timeout ($timeoutMs ms) waiting for $what")
    }

    private fun Phone.awaitConnected(timeoutMs: Long = 8_000) = waitFor("CONNECTED", timeoutMs) { state == LinkState.CONNECTED }

    private fun hash(text: String) = MessageDigest.getInstance("SHA-256").digest(text.toByteArray())
        .joinToString("") { "%02x".format(it) }.take(8)

    private fun freePort() = ServerSocket(0).use { it.localPort }

    // -- scenarios --------------------------------------------------------------------------------

    @Test fun goodHandshakeAndHello() {
        val s = serve()
        val p = phoneFor(s)
        p.awaitConnected()
        assertEquals(device, s.await("connected").json.getString("device"))
        waitFor("probe peer") { p.probe.peer != null }
        assertTrue(p.probe.peer!!.supports("clipboard"))
        assertEquals("Interop PC", p.probe.peer!!.name)
    }

    @Test fun clipboardBothWaysAndReceipt() {
        val s = serve()
        val p = phoneFor(s)
        p.awaitConnected(); s.await("connected")
        // PC -> phone through the real Kotlin ClipboardFeature
        s.sendClip("from the pc ü")
        waitFor("clip on phone") { p.sink.texts.contains("from the pc ü") }
        // phone -> PC through the real Python ClipboardFeature
        p.clipboard.onLocalClip("from the phone 😀")
        val ev = s.await("clip")
        assertEquals("from the phone 😀".let { it.codePointCount(0, it.length) }, ev.json.getInt("len")) // Python counts code points
        assertEquals(hash("from the phone 😀"), ev.json.getString("hash"))
        // request/reply: delivery receipt is the empty ok reply
        val got = CopyOnWriteArrayList<Response?>()
        val header = JSONObject().put("ch", "clipboard").put("type", "clip").put("kind", "text").put("data", "needs a receipt").put("ts", 1)
        assertTrue(p.client.send(null, header) { got += it })
        waitFor("receipt") { got.isNotEmpty() }
        assertTrue(got[0]!!.ok)
        assertEquals(0, got[0]!!.body.size)
        assertEquals(null, got[0]!!.data)
        assertEquals(2, s.count("clip"))
    }

    @Test fun wrongPskIsRejectedWith4401AndRetriedWithBackoffThenFixedPskConnects() {
        val s = serve()
        val p = phoneFor(s, pairing = Pairing(device, ByteArray(32) { 9 }), backoff = { Backoff { 0.5 } })
        s.await("auth_failed", 3)
        assertTrue(p.states.none { it.second == LinkState.CONNECTED })
        assertEquals(0, s.count("connected"))
        val t = s.events.filter { it.name == "auth_failed" }.map { it.atMs }
        val gaps = t.zipWithNext { a, b -> b - a }
        println("interop wrongPsk: auth_failed gaps ms = $gaps")
        assertTrue("gaps should grow (designed 1 s, 2 s): $gaps", gaps[0] in 500..4_000 && gaps[1] > gaps[0])
        p.pairing = Pairing(device, psk)
        p.client.reconnect()
        p.awaitConnected()
        s.await("connected")
    }

    @Test fun unknownDeviceIsRejectedWith4401() {
        val s = serve()
        val p = phoneFor(s, pairing = Pairing("nobody", psk))
        s.await("auth_failed", 2)
        assertTrue(p.states.none { it.second == LinkState.CONNECTED })
        assertEquals(0, s.count("connected"))
        assertFalse(p.state == LinkState.CONNECTED)
    }

    @Test fun reconnectsAfterServerKickAndAfterServerStopStart() {
        val s = serve()
        val p = phoneFor(s, backoff = { Backoff { 0.5 } }) // exact 1 s, 2 s, 4 s ... cap 10 s
        p.awaitConnected(); s.await("connected")
        // kick: the drop resets the backoff, so the redial comes ~1 s later
        val t0 = System.currentTimeMillis()
        s.kick(1001)
        s.await("disconnected")
        s.await("connected", 2, 10_000 + 3_000)
        val kickMs = System.currentTimeMillis() - t0
        p.awaitConnected()
        println("interop kick: reconnected after $kickMs ms")
        assertTrue("reconnect after kick took $kickMs ms", kickMs < 10_000 + 3_000) // backoff cap + margin (reset gives ~1 s)
        // stop / start on the same port: dials at +1 s and +3 s fail until the restart
        val port = s.port
        s.stop(); s.await("stopped")
        waitFor("phone notices") { p.state != LinkState.CONNECTED }
        waitFor("a failed dial against the stopped server") { p.client.status.value.detail == "unreachable" }
        val t1 = System.currentTimeMillis()
        s.start(); s.await("listening", 2)
        assertEquals(port, s.port)
        s.await("connected", 3, 10_000 + 5_000)
        val restartMs = System.currentTimeMillis() - t1
        println("interop stop/start: reconnected $restartMs ms after the server came back")
        assertTrue("reconnect after restart took $restartMs ms", restartMs < 10_000 + 3_000) // backoff cap + margin
    }

    @Test fun endpointOrderManualRefusedThenMdnsConnectsInTheSamePass() {
        val s = serve()
        val manual = Endpoint("127.0.0.1", freePort())
        val t0 = System.currentTimeMillis()
        val p = phoneFor(s, endpoints = Endpoint.order(manual, Endpoint("127.0.0.1", s.port), null))
        p.awaitConnected(6_000)
        val ms = System.currentTimeMillis() - t0
        println("interop endpoint order (refused manual -> mdns): connected after $ms ms")
        assertTrue("took $ms ms", ms < 5_000)
        assertEquals(1, p.states.count { it.second == LinkState.CONNECTED })
    }

    @Test fun endpointOrderBlackholeManualTimesOutAfterFourSecondsThenMdnsConnects() {
        val s = serve()
        val manual = Endpoint("10.255.255.1")
        val t0 = System.currentTimeMillis()
        val p = phoneFor(s, endpoints = Endpoint.order(manual, Endpoint("127.0.0.1", s.port), null))
        p.awaitConnected(20_000)
        val ms = System.currentTimeMillis() - t0
        println("interop endpoint order (blackhole manual -> mdns): connected after $ms ms")
        assertTrue("took $ms ms", ms < 18_000) // 4 s connect timeout + one pass; the 10 s handshake timer is the outer bound
    }

    // -- known bugs (strict expected failures) ---------------------------------------------------------

    @Test fun bugClientStaysConnectingWhenServerNeverSendsHello() {
        val s = serve(noHello = true)
        val p = phoneFor(s)
        s.await("connected") // precondition: the server has our hello; its own hello is withheld
        assertEquals(LinkState.CONNECTING, p.state)
        expectBug("client waits forever for the server hello after READY (status stuck CONNECTING)") {
            bugCheck(poll(10_000) { p.state != LinkState.CONNECTING }, "still CONNECTING 10 s after auth without a server hello")
        }
    }

    @Test fun bugConnectThenImmediateKickRedialsEveryOneSecond() {
        val s = serve(kickOnConnect = 1001)
        phoneFor(s, backoff = { Backoff { 0.5 } })
        val first = s.await("connected") // precondition: the handshake completes, then the server kicks
        s.await("disconnected")
        val windowEnd = first.atMs + 8_000
        while (System.currentTimeMillis() < windowEnd) Thread.sleep(20) // a count over a fixed window needs the window
        val n = s.events.count { it.name == "connected" && it.atMs <= windowEnd }
        println("interop accept-then-kick: $n connects in 8 s")
        expectBug("accept-then-kick redials about every 1 s instead of backing off") {
            bugCheck(n <= 5, "$n connects in 8 s (backoff would give about 4)")
        }
    }
}
