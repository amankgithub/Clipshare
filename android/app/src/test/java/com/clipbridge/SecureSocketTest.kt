package com.clipbridge

import com.clipbridge.link.Endpoint
import com.clipbridge.link.Envelopes
import com.clipbridge.link.Pairing
import com.clipbridge.link.SecureSocket
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
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

@OptIn(ExperimentalCoroutinesApi::class)
class SecureSocketTest {
    private val psk = ByteArray(32) { 9 }
    private val pairing = Pairing("dev1", psk)

    private class Rig(t: TestScope, val server: HandshakeServer, pairing: Pairing) {
        val scope = CoroutineScope(StandardTestDispatcher(t.testScheduler) + SupervisorJob())
        val net = FakeNet(scope, { t.currentTime }, { server })
        var ready = 0
        val plaintexts = mutableListOf<ByteArray>()
        val sock = SecureSocket(scope, pairing, Endpoint("10.0.0.2"), "clipbridge.v1",
            onReady = { ready++ }, onPlaintext = { _, p -> plaintexts += p }).also { it.open(net) }
        val ws get() = net.sockets.single()
    }

    private fun rig(t: TestScope, configure: HandshakeServer.() -> Unit = {}): Rig =
        Rig(t, HandshakeServer(psk).apply(configure), pairing)

    private suspend fun TestScope.check(configure: HandshakeServer.() -> Unit = {}, body: suspend Rig.() -> Unit) {
        val r = rig(this, configure)
        try { runCurrent(); r.body() } finally { r.scope.cancel() }
    }

    @Test fun goodHandshakeReachesReadyAndSendsEncryptedFrames() = runTest {
        check({ hello = null }) {
            runCurrent()
            assertEquals(1, ready)
            assertTrue(sock.isReady)
            assertEquals(true, server.clientAuthValid)
            val hdr = JSONObject().put("ch", "x")
            assertTrue(sock.send(Envelopes.encode(hdr)))
            runCurrent()
            assertEquals("x", server.received.single().header.getString("ch"))
            server.ws.serverClose(1001)
            runCurrent()
            assertTrue(sock.closed.await()) // handshake had succeeded
            assertFalse(sock.send(ByteArray(8)))
        }
    }

    @Test fun challengeWithWrongTypeVersionOrNonceClosesWith4400() = runTest {
        for (case in listOf<HandshakeServer.() -> Unit>(
            { challenge(type = "auth_ok") },
            { challenge(v = 2) },
            { challenge(sn = "AAAA") },            // valid base64, wrong length
            { challenge(sn = "not base64!!") },
        )) {
            check({ autoChallenge = false }) {
                runCurrent()
                server.case()
                runCurrent()
                assertEquals(4400, ws.closeCode)
                assertEquals(0, ready)
                assertTrue(ws.sentText.isEmpty()) // no auth sent to a bad challenge
            }
        }
    }

    @Test fun challengeThatIsNotJsonOrTooLongClosesWith4400() = runTest {
        for (text in listOf("not json", "{\"type\":\"challenge\",\"pad\":\"" + "a".repeat(5000) + "\"}")) {
            check({ autoChallenge = false }) {
                runCurrent()
                ws.deliverText(text)
                runCurrent()
                assertEquals(4400, ws.closeCode)
            }
        }
    }

    @Test fun badAuthOkMacClosesWith4401AndNeverReady() = runTest {
        check({ corruptMac = true }) {
            runCurrent()
            assertEquals(4401, ws.closeCode)
            assertEquals(0, ready)
            assertFalse(sock.isReady)
            assertFalse(sock.send(ByteArray(8)))
            assertFalse(sock.closed.await()) // handshake did not succeed
        }
    }

    @Test fun malformedAuthOkClosesWith4400() = runTest {
        for (text in listOf("{\"type\":\"challenge\"}", "{\"type\":\"auth_ok\",\"mac\":\"AAAA\"}", "[]")) {
            check({ autoAuthOk = false }) {
                runCurrent()
                ws.deliverText(text)
                runCurrent()
                assertEquals(4400, ws.closeCode)
                assertEquals(0, ready)
            }
        }
    }

    @Test fun wrongSubprotocolInOnOpenCloses4400() = runTest {
        check({ protocol = "other.v9"; autoChallenge = false }) {
            runCurrent()
            assertEquals(4400, ws.closeCode)
            assertEquals(0, ready)
        }
    }

    @Test fun textFrameAfterReadyClosesWith4400() = runTest {
        check({ hello = null }) {
            runCurrent()
            assertTrue(sock.isReady)
            ws.deliverText("{}")
            runCurrent()
            assertEquals(4400, ws.closeCode)
        }
    }

    @Test fun binaryFrameBeforeReadyClosesWith4400AndBadFrameWith4402() = runTest {
        check({ autoChallenge = false }) {
            runCurrent()
            ws.deliverBinary(ByteArray(40))
            runCurrent()
            assertEquals(4400, ws.closeCode)
        }
        check({ hello = null }) {
            runCurrent()
            ws.deliverBinary(ByteArray(40)) // not a valid frame
            runCurrent()
            assertEquals(4402, ws.closeCode)
        }
    }

    @Test fun silentServerTimesOutWith4408ThenCancelsAfterGrace() = runTest {
        check({ autoChallenge = false }) {
            ws.ackClose = false
            advanceTimeBy(9_999); runCurrent()
            assertNull(ws.closeCode)
            advanceTimeBy(2); runCurrent()
            assertEquals(4408, ws.closeCode)
            assertFalse(ws.cancelled)
            advanceTimeBy(1_001); runCurrent()
            assertTrue(ws.cancelled)
            assertFalse(sock.closed.await())
        }
    }

    @Test fun handshakeTimerIsCancelledOnceReady() = runTest {
        check({ hello = null }) {
            runCurrent()
            assertTrue(sock.isReady)
            advanceTimeBy(60_000); runCurrent()
            assertNull(ws.closeCode)
            assertTrue(sock.isReady)
        }
    }

    @Test fun serverClosingWith4401DuringHandshakeIsAFailedOutcome() = runTest {
        check({ autoAuthOk = false }) {
            runCurrent()
            assertEquals(true, server.clientAuthValid)
            server.ws.serverClose(4401)
            runCurrent()
            assertFalse(sock.closed.await())
            assertEquals(0, ready)
        }
    }
}
