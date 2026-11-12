package com.clipbridge

import com.clipbridge.link.Envelopes
import com.clipbridge.link.Feature
import com.clipbridge.link.FeatureRouter
import com.clipbridge.link.Peer
import com.clipbridge.link.Receipts
import com.clipbridge.link.Routed
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class ReceiptsTest {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    @Test fun ackResolvesOnceTrue() {
        val r = Receipts(scope)
        val got = mutableListOf<Boolean>()
        val mid = r.track { got += it }
        r.onControl(Receipts.ack(mid))
        r.onControl(Receipts.ack(mid))
        r.failAll()
        assertEquals(listOf(true), got)
    }

    @Test fun timeoutAndFailAllReportFalse() {
        val r = Receipts(scope, timeoutMs = 50)
        val got = java.util.concurrent.LinkedBlockingQueue<Boolean>()
        r.track { got += it }
        assertEquals(false, got.poll(2, java.util.concurrent.TimeUnit.SECONDS))
        val r2 = Receipts(scope)
        r2.track { got += it }
        r2.failAll()
        assertEquals(false, got.poll(1, java.util.concurrent.TimeUnit.SECONDS))
    }

    @Test fun requestedMidValidation() {
        assertEquals("m0", Receipts.requestedMid(JSONObject().put("mid", "m0")))
        assertNull(Receipts.requestedMid(JSONObject().put("mid", "bad mid!")))
        assertNull(Receipts.requestedMid(JSONObject().put("mid", 5)))
        assertNull(Receipts.requestedMid(JSONObject()))
    }

    @Test fun routerReportsWhatTheLinkMustDo() {
        val router = FeatureRouter()
        router.register(object : Feature {
            override val name = "ok"
            override fun onPeerConnected(peer: Peer) {}
            override fun onPeerDisconnected(peer: Peer) {}
            override fun onMessage(peer: Peer, header: JSONObject, body: ByteArray) {
                if (header.optBoolean("boom")) error("boom")
            }
        })
        val p = Peer("server")
        fun env(h: JSONObject) = Envelopes.encode(h)
        assertEquals(Routed.Handled("a"), router.dispatch(p, env(JSONObject().put("ch", "ok").put("mid", "a"))))
        assertEquals(Routed.Handled(null), router.dispatch(p, env(JSONObject().put("ch", "ok"))))
        assertEquals(Routed.Dropped, router.dispatch(p, env(JSONObject().put("ch", "ok").put("boom", true).put("mid", "b"))))
        assertEquals(Routed.Dropped, router.dispatch(p, env(JSONObject().put("ch", "nope").put("mid", "c"))))
        assert(router.dispatch(p, env(Receipts.ack("d"))) is Routed.LinkControl)
    }
}
