package com.clipbridge

import com.clipbridge.link.Envelopes
import com.clipbridge.link.Feature
import com.clipbridge.link.FeatureRouter
import com.clipbridge.link.Peer
import com.clipbridge.link.Reply
import com.clipbridge.link.Requests
import com.clipbridge.link.Response
import com.clipbridge.link.Routed
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import org.json.JSONObject
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

class RequestsTest {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    private fun reply(mid: String, ok: Boolean, data: JSONObject? = null): JSONObject {
        val h = JSONObject().put("ch", "link").put("type", "reply").put("mid", mid).put("ok", ok)
        if (data != null) h.put("data", data)
        return h
    }

    @Test fun okReplyResolvesOnceWithContent() {
        val r = Requests(scope)
        val got = mutableListOf<Response?>()
        val mid = r.track { got += it }
        r.onControl(reply(mid, true, JSONObject().put("n", 3)), byteArrayOf(1, 2))
        r.onControl(reply(mid, true), ByteArray(0))
        r.failAll()
        assertEquals(1, got.size)
        assertTrue(got[0]!!.ok)
        assertEquals(3, got[0]!!.data!!.getInt("n"))
        assertArrayEquals(byteArrayOf(1, 2), got[0]!!.body)
    }

    @Test fun failedReplyIsNotOkAndCarriesNothing() {
        val r = Requests(scope)
        val got = mutableListOf<Response?>()
        val mid = r.track { got += it }
        r.onControl(reply(mid, false, JSONObject().put("x", 1)), byteArrayOf(9))
        assertFalse(got.single()!!.ok)
        assertNull(got.single()!!.data)
        assertEquals(0, got.single()!!.body.size)
    }

    @Test fun ignoresUnknownMidAndOtherTypes() {
        val r = Requests(scope)
        val got = mutableListOf<Response?>()
        val mid = r.track { got += it }
        r.onControl(reply("other", true), ByteArray(0))
        r.onControl(JSONObject().put("ch", "link").put("type", "ack").put("mid", mid), ByteArray(0))
        r.onControl(JSONObject().put("ch", "link").put("type", "reply").put("mid", mid), ByteArray(0)) // no ok
        assertTrue(got.isEmpty())
    }

    @Test fun timeoutAndFailAllReportNull() {
        val got = LinkedBlockingQueue<Any>()
        val r = Requests(scope, timeoutMs = 50)
        r.track { got += it ?: "none" }
        assertEquals("none", got.poll(2, TimeUnit.SECONDS))
        val r2 = Requests(scope)
        r2.track { got += it ?: "none" }
        r2.track { got += it ?: "none" }
        r2.failAll()
        assertEquals("none", got.poll(1, TimeUnit.SECONDS))
        assertEquals("none", got.poll(1, TimeUnit.SECONDS))
    }

    @Test fun midsAreUniqueAndValid() {
        val r = Requests(scope)
        val a = r.track {}; val b = r.track {}
        assertTrue(a != b)
        assertEquals(a, Requests.requestedMid(JSONObject().put("mid", a)))
        r.failAll()
    }

    @Test fun requestedMidValidation() {
        assertEquals("m0", Requests.requestedMid(JSONObject().put("mid", "m0")))
        assertNull(Requests.requestedMid(JSONObject().put("mid", "bad mid!")))
        assertNull(Requests.requestedMid(JSONObject().put("mid", "x".repeat(65))))
        assertNull(Requests.requestedMid(JSONObject().put("mid", 5)))
        assertNull(Requests.requestedMid(JSONObject()))
    }

    @Test fun replyHeaderShape() {
        val ok = Requests.replyHeader("a", Response(true, JSONObject().put("k", 1)))
        assertEquals("link", ok.getString("ch")); assertEquals("reply", ok.getString("type"))
        assertEquals("a", ok.getString("mid")); assertTrue(ok.getBoolean("ok"))
        assertEquals(1, ok.getJSONObject("data").getInt("k"))
        assertFalse(Requests.replyHeader("a", Response(true)).has("data"))
        val bad = Requests.replyHeader("b", Response.FAILED)
        assertFalse(bad.getBoolean("ok")); assertFalse(bad.has("data"))
    }

    @Test fun routerReportsWhatTheLinkMustDo() {
        val logs = mutableListOf<String>()
        val router = FeatureRouter { logs += it }
        router.register(object : Feature {
            override val name = "ok"
            override val version = 3
            override fun onPeerConnected(peer: Peer) {}
            override fun onPeerDisconnected(peer: Peer) {}
            override fun onMessage(peer: Peer, header: JSONObject, body: ByteArray): Reply? {
                if (header.optBoolean("boom")) error("boom")
                return if (header.optBoolean("answer")) Reply(JSONObject().put("v", 1), byteArrayOf(7)) else null
            }
        })
        assertEquals(mapOf("ok" to 3), router.versions())
        val p = Peer("server")
        fun go(h: JSONObject) = router.dispatch(p, Envelopes.encode(h))
        fun answer(h: JSONObject) = go(h) as Routed.Answer

        val receipt = answer(JSONObject().put("ch", "ok").put("mid", "a"))
        assertEquals("a", receipt.mid); assertTrue(receipt.response.ok)
        assertNull(receipt.response.data); assertEquals(0, receipt.response.body.size)

        val data = answer(JSONObject().put("ch", "ok").put("answer", true).put("mid", "b"))
        assertEquals(1, data.response.data!!.getInt("v")); assertArrayEquals(byteArrayOf(7), data.response.body)

        assertSame(Response.FAILED, answer(JSONObject().put("ch", "ok").put("boom", true).put("mid", "c")).response)
        assertSame(Response.FAILED, answer(JSONObject().put("ch", "nope").put("mid", "d")).response)

        assertSame(Routed.Done, go(JSONObject().put("ch", "ok")))
        assertSame(Routed.Done, go(JSONObject().put("ch", "nope")))
        assertSame(Routed.Done, go(JSONObject().put("ch", "ok").put("mid", "bad mid")))
        assertSame(Routed.Done, router.dispatch(p, byteArrayOf(1)))
        assertTrue(go(Requests.replyHeader("e", Response(true))) is Routed.LinkControl)
    }
}
