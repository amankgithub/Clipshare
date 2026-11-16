package com.clipbridge

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
import com.clipbridge.link.Requests
import com.clipbridge.link.Response
import com.clipbridge.link.Status
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.currentTime
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.file.Files

/** The connect loop, in virtual time, against a scripted PC ([FakeNet]). */
@OptIn(ExperimentalCoroutinesApi::class)
class BridgeClientLoopTest {
    private val psk = ByteArray(32) { 5 }
    private val manual = Endpoint("10.0.0.1")
    private val mdns = Endpoint("10.0.0.2", 9000)
    private val cached = Endpoint("10.0.0.3")

    private class RecFeature(override val name: String = "echo") : Feature {
        override val version = 1
        val events = mutableListOf<String>()
        var reply: Reply? = null
        override fun onPeerConnected(peer: Peer) { events += "up" }
        override fun onPeerDisconnected(peer: Peer) { events += "down" }
        override fun onMessage(peer: Peer, header: JSONObject, body: ByteArray): Reply? { events += "msg"; return reply }
    }

    private inner class Env(val t: TestScope) {
        val scope = CoroutineScope(StandardTestDispatcher(t.testScheduler) + SupervisorJob())
        /** One entry per dial, in order; the host decides what answers. */
        val servers = mutableListOf<ServerScript>()
        val good = hashSetOf<String>()
        val net = FakeNet(scope, { t.currentTime }) { host ->
            (if (host in good) HandshakeServer(psk) else DeadServer()).also { servers += it }
        }
        private val tmp = Files.createTempDirectory("cb").toFile()
        var pairing: Pairing? = Pairing("dev1", psk)
        var eps: List<Endpoint> = emptyList()
        val feature = RecFeature()
        val router = FeatureRouter().also { it.register(feature) }
        val goodEndpoints = mutableListOf<Endpoint>()
        val statuses = mutableListOf<Status>()
        val client = BridgeClient(
            scope, { pairing }, "Phone", { eps }, router, { goodEndpoints += it },
            PartialStore(tmp), {},
            sockets = net, newBackoff = { Backoff { 0.5 } }, // jitter factor 1.0: exact delays
        )
        init {
            t.backgroundScope.launch(UnconfinedTestDispatcher(t.testScheduler)) { client.status.collect { statuses += it } }
        }
        val dialTimes get() = net.dials.map { it.timeMs }
        val dialHosts get() = net.dials.map { it.host }
        val server get() = servers.last() as HandshakeServer
        fun states() = statuses.map { it.state }
        fun close() { client.stop(); scope.cancel(); tmp.deleteRecursively() }
    }

    private suspend fun TestScope.env(body: suspend Env.() -> Unit) {
        val e = Env(this)
        try { body(e) } finally { e.close() }
    }

    @Test fun passTriesManualThenMdnsThenCachedAndRepeatsAfterBackoff() = runTest {
        env {
            eps = Endpoint.order(manual, mdns, cached)
            client.start(); runCurrent()
            assertEquals(listOf("10.0.0.1", "10.0.0.2", "10.0.0.3"), dialHosts)
            assertEquals(listOf(0L, 0L, 0L), dialTimes)
            assertEquals(9000, net.dials[1].port)
            advanceTimeBy(1_000); runCurrent()
            assertEquals(listOf(0L, 0L, 0L, 1000L, 1000L, 1000L), dialTimes)
        }
    }

    @Test fun deadManualAndMdnsFallThroughToCachedInTheSamePass() = runTest {
        env {
            good += "10.0.0.3"
            eps = Endpoint.order(manual, mdns, cached)
            client.start(); runCurrent()
            assertEquals(listOf("10.0.0.1", "10.0.0.2", "10.0.0.3"), dialHosts)
            assertEquals(setOf(0L), dialTimes.toSet())
            assertEquals(LinkState.CONNECTED, client.status.value.state)
            assertEquals("10.0.0.3:8765", client.status.value.detail)
            assertEquals(listOf(cached), goodEndpoints)
            assertEquals(listOf("up"), feature.events)
            // the hello carries our features
            val hello = server.received.single().header
            assertEquals("hello", hello.getString("type"))
            assertEquals(1, hello.getJSONObject("features").getInt("echo"))
        }
    }

    @Test fun workingManualHostStopsThePassAtOnce() = runTest {
        env {
            good += "10.0.0.1"
            eps = Endpoint.order(manual, mdns, cached)
            client.start(); runCurrent()
            assertEquals(listOf("10.0.0.1"), dialHosts)
        }
    }

    @Test fun statusTransitions() = runTest {
        env {
            pairing = null
            client.start(); runCurrent()
            assertEquals(LinkState.NOT_PAIRED, client.status.value.state)

            pairing = Pairing("dev1", psk)
            eps = emptyList()
            client.retryNow(); runCurrent()
            assertEquals(Status(LinkState.SEARCHING, "no endpoint"), client.status.value)

            good += "10.0.0.3"
            eps = listOf(manual, cached)
            statuses.clear()
            client.retryNow(); runCurrent()
            assertEquals(
                listOf(
                    Status(LinkState.CONNECTING, "10.0.0.1:8765"),
                    Status(LinkState.CONNECTING, "10.0.0.3:8765"),
                    Status(LinkState.CONNECTED, "10.0.0.3:8765"),
                ),
                statuses,
            )
            statuses.clear()
            server.ws.serverClose(1001); runCurrent()
            assertEquals(Status(LinkState.SEARCHING, "disconnected"), client.status.value)
            assertEquals(listOf("up", "down"), feature.events)
        }
    }

    @Test fun allDeadEndsThePassAsUnreachable() = runTest {
        env {
            eps = listOf(manual)
            client.start(); runCurrent()
            assertEquals(Status(LinkState.SEARCHING, "unreachable"), client.status.value)
            assertEquals(listOf(LinkState.SEARCHING, LinkState.CONNECTING, LinkState.SEARCHING), states())
        }
    }

    @Test fun backoffDoublesFromOneSecondAndCapsAtTen() = runTest {
        env {
            eps = listOf(manual)
            client.start()
            advanceTimeBy(40_000); runCurrent()
            assertEquals(listOf(0L, 1_000L, 3_000L, 7_000L, 15_000L, 25_000L, 35_000L), dialTimes)
        }
    }

    @Test fun backoffResetsAfterASuccessfulConnection() = runTest {
        env {
            eps = listOf(manual)
            client.start()
            advanceTimeBy(3_000); runCurrent() // dials at 0, 1000, 3000 fail; next wait would be 4000
            assertEquals(listOf(0L, 1_000L, 3_000L), dialTimes)
            good += "10.0.0.1"
            advanceTimeBy(4_000); runCurrent() // dial at 7000 succeeds
            assertEquals(7_000L, dialTimes.last())
            assertEquals(LinkState.CONNECTED, client.status.value.state)
            advanceTimeBy(2_000) // t = 9000
            server.ws.serverClose(1001); runCurrent()
            good.clear()
            advanceTimeBy(1_000); runCurrent() // reset: next dial 1 s after the drop, not 8 s
            assertEquals(10_000L, dialTimes.last())
            advanceTimeBy(2_000); runCurrent() // and failures start again at 1 s, 2 s
            assertEquals(listOf(0L, 1_000L, 3_000L, 7_000L, 10_000L, 12_000L), dialTimes)
        }
    }

    @Test fun retryNowCutsTheBackoffShort() = runTest {
        env {
            eps = listOf(manual)
            client.start()
            advanceTimeBy(500); runCurrent()
            client.retryNow(); runCurrent()
            assertEquals(listOf(0L, 500L), dialTimes)
        }
    }

    @Test fun reconnectClosesTheConnectionWith1000AndRedialsAtOnce() = runTest {
        env {
            good += "10.0.0.1"
            eps = listOf(manual)
            client.start(); runCurrent()
            val first = net.sockets.single()
            advanceTimeBy(5_000)
            client.reconnect(); runCurrent()
            assertEquals(1000, first.closeCode)
            assertEquals(listOf(0L, 5_000L), dialTimes)
            assertEquals(LinkState.CONNECTED, client.status.value.state)
            assertEquals(listOf("up", "down", "up"), feature.events)
        }
    }

    @Test fun networkLostDropsAtOnceThenRedialsAfterOneSecond() = runTest {
        env {
            good += "10.0.0.1"
            eps = listOf(manual)
            client.start(); runCurrent()
            client.networkLost(); runCurrent()
            assertTrue(net.sockets.single().cancelled)
            assertEquals(Status(LinkState.SEARCHING, "disconnected"), client.status.value)
            assertEquals(listOf("up", "down"), feature.events)
            advanceTimeBy(1_000); runCurrent()
            assertEquals(listOf(0L, 1_000L), dialTimes)
        }
    }

    @Test fun stopEndsTheLoopAndClosesTheSession() = runTest {
        env {
            good += "10.0.0.1"
            eps = listOf(manual)
            client.start(); runCurrent()
            client.stop(); runCurrent()
            assertEquals(1000, net.sockets.single().closeCode)
            advanceTimeBy(60_000); runCurrent()
            assertEquals(1, net.dials.size)
        }
    }

    @Test fun serverRejectingTheDeviceWith4401IsRetriedWithBackoff() = runTest {
        env {
            eps = listOf(manual)
            net.script = { HandshakeServer(psk).apply { autoAuthOk = false }.also { servers += it } }
            client.start(); runCurrent()
            server.ws.serverClose(4401); runCurrent()
            assertEquals(Status(LinkState.SEARCHING, "unreachable"), client.status.value)
            assertTrue(feature.events.isEmpty()) // never connected
            advanceTimeBy(1_000); runCurrent()
            assertEquals(listOf(0L, 1_000L), dialTimes)
        }
    }

    @Test fun invalidFirstFrameFromServerClosesWith4400() = runTest {
        env {
            good += "10.0.0.1"
            eps = listOf(manual)
            net.script = { HandshakeServer(psk).apply { hello = JSONObject().put("ch", "link").put("type", "hello") }.also { servers += it } }
            client.start(); runCurrent()
            assertEquals(4400, net.sockets.single().closeCode)
            assertTrue(feature.events.isEmpty())
        }
    }

    // -- send / replies ----------------------------------------------------------------------

    @Test fun sendWithNoPeerReturnsFalseAndReportsNullOnce() = runTest {
        env {
            val got = mutableListOf<Response?>()
            assertFalse(client.send(null, JSONObject().put("ch", "echo"), ByteArray(0)) { got += it })
            assertEquals(listOf<Response?>(null), got)
            assertFalse(client.send(null, JSONObject().put("ch", "echo")))
            advanceTimeBy(30_000); runCurrent()
            assertEquals(1, got.size) // the request timer was cancelled by the resolve
        }
    }

    @Test fun requestReplyRoundTripAndLinkDropFailsPending() = runTest {
        env {
            good += "10.0.0.1"
            eps = listOf(manual)
            client.start(); runCurrent()
            val got = mutableListOf<Response?>()
            assertTrue(client.send(null, JSONObject().put("ch", "echo").put("type", "x")) { got += it })
            assertTrue(client.send(null, JSONObject().put("ch", "echo").put("type", "y")) { got += it })
            runCurrent()
            val mids = server.received.drop(1).map { it.header.getString("mid") } // [0] is our hello
            assertEquals(2, mids.size)
            server.send(JSONObject().put("ch", "link").put("type", "reply").put("mid", mids[0]).put("ok", true).put("data", JSONObject().put("n", 1)))
            runCurrent()
            assertEquals(1, got.size)
            assertTrue(got[0]!!.ok)
            server.ws.serverClose(1001); runCurrent()
            assertEquals(2, got.size)
            assertNull(got[1]) // dropped before the second reply
        }
    }

    @Test fun oversizeReplyContentFallsBackToOkFalse() = runTest {
        env {
            good += "10.0.0.1"
            eps = listOf(manual)
            client.start(); runCurrent()
            feature.reply = Reply(body = ByteArray(16 * 1024 * 1024 + 1))
            server.send(JSONObject().put("ch", "echo").put("mid", "r1"))
            runCurrent()
            val reply = server.received.last().header
            assertEquals("reply", reply.getString("type"))
            assertEquals("r1", reply.getString("mid"))
            assertEquals(false, reply.getBoolean("ok"))
            // a normal reply still goes out ok
            feature.reply = null
            server.send(JSONObject().put("ch", "echo").put("mid", "r2"))
            runCurrent()
            assertEquals(true, server.received.last().header.getBoolean("ok"))
        }
    }

    @Test fun secondHelloClosesWith4400() = runTest {
        env {
            good += "10.0.0.1"
            eps = listOf(manual)
            client.start(); runCurrent()
            server.send(com.clipbridge.link.Hello("PC", mapOf("echo" to 1)).header())
            runCurrent()
            assertEquals(4400, net.sockets.single().closeCode)
        }
    }

    // -- known bugs (strict expected failures) ---------------------------------------------------

    @Test fun bugNoClientTimeoutBetweenReadyAndServerHello() {
        runTest {
            env {
                good += "10.0.0.1"
                eps = listOf(manual)
                net.script = { HandshakeServer(psk).apply { hello = null }.also { servers += it } }
                client.start()
                advanceTimeBy(120_000); runCurrent()
                // precondition: one connection, authenticated (the server has our hello), no server hello sent
                assertEquals(1, net.dials.size)
                assertEquals("hello", server.received.single().header.getString("type"))
                expectBug("client waits forever for the server hello after READY (status stuck CONNECTING)") {
                    bugCheck(client.status.value.state != LinkState.CONNECTING,
                        "still CONNECTING after 2 minutes without a hello")
                }
            }
        }
    }

    @Test fun bugBackoffResetsOnReadyEvenWhenTheLinkDropsAtOnce() {
        runTest {
            env {
                good += "10.0.0.1"
                eps = listOf(manual)
                // accepts, completes the handshake, then drops at once
                net.script = {
                    object : HandshakeServer(psk) {
                        override fun onText(ws: FakeSocket, text: String) { super.onText(ws, text); ws.serverClose(1001) }
                    }.also { servers += it }
                }
                client.start()
                advanceTimeBy(20_000); runCurrent()
                assertTrue("precondition: several dials (${dialTimes.size})", dialTimes.size >= 4)
                val gaps = dialTimes.zipWithNext { a, b -> b - a }
                expectBug("connect-then-drop retries every ~1 s forever instead of backing off") {
                    bugCheck(gaps.last() > gaps.first(), "dial gaps $gaps should grow")
                }
            }
        }
    }
}
