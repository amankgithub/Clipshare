package com.clipbridge

import com.clipbridge.features.clipboard.ClipSink
import com.clipbridge.features.clipboard.ClipboardFeature
import com.clipbridge.features.clipboard.ImageUtil
import com.clipbridge.link.Envelopes
import com.clipbridge.link.FeatureRouter
import com.clipbridge.link.FileSource
import com.clipbridge.link.LinkSender
import com.clipbridge.link.Peer
import com.clipbridge.link.Requests
import com.clipbridge.link.Response
import com.clipbridge.link.Routed
import org.json.JSONObject
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ClipboardFeatureTest {
    private val peer = Peer("server")
    private val png = byteArrayOf(0x89.toByte(), 0x50, 0x4E, 0x47, 1, 2, 3)

    private class Sent(val header: JSONObject, val body: ByteArray, val onReply: ((Response?) -> Unit)?)

    private class FakeSender : LinkSender {
        val sent = mutableListOf<Sent>()
        override fun send(peer: Peer?, header: JSONObject, body: ByteArray, onReply: ((Response?) -> Unit)?): Boolean {
            sent += Sent(header, body, onReply); return true
        }
        override fun sendFile(peer: Peer, feature: String, source: FileSource, meta: JSONObject, onProgress: (Long) -> Unit, onDone: (Boolean) -> Unit) {}
    }

    private class FakeSink : ClipSink {
        val texts = mutableListOf<String>()
        val images = mutableListOf<ByteArray>()
        override fun write(text: String) { texts += text }
        override fun writeImage(png: ByteArray) { images += png }
    }

    private val sender = FakeSender()
    private val sink = FakeSink()
    private val feature = ClipboardFeature(sender, sink)

    private fun clip(kind: String, build: JSONObject.() -> Unit = {}) =
        JSONObject().put("ch", "clipboard").put("type", "clip").put("kind", kind).apply(build)

    @Test fun remoteTextIsWrittenOnceAndDuplicatesAreIgnored() {
        assertNull(feature.onMessage(peer, clip("text") { put("data", "hi") }, ByteArray(0)))
        assertNull(feature.onMessage(peer, clip("text") { put("data", "hi") }, ByteArray(0)))
        assertEquals(listOf("hi"), sink.texts)
    }

    @Test fun remoteImageIsWrittenAndEchoOfItIsNotSentBack() {
        feature.onPeerConnected(peer)
        feature.onMessage(peer, clip("image") { put("mime", "image/png") }, png)
        assertEquals(1, sink.images.size)
        assertArrayEquals(png, sink.images[0])
        feature.onLocalImage(png) // force: an explicit send is never dropped
        assertEquals(1, sender.sent.size)
    }

    @Test fun invalidMessagesAreNotApplied() {
        feature.onMessage(peer, JSONObject().put("type", "other"), ByteArray(0))
        feature.onMessage(peer, clip("text"), ByteArray(0)) // no data
        feature.onMessage(peer, clip("text") { put("data", 5) }, ByteArray(0))
        feature.onMessage(peer, clip("image") { put("mime", "image/jpeg") }, png)
        feature.onMessage(peer, clip("image") { put("mime", "image/png") }, ByteArray(0))
        feature.onMessage(peer, clip("video"), png)
        assertTrue(sink.texts.isEmpty() && sink.images.isEmpty())
    }

    @Test fun localTextSendsWhenConnectedHoldsWhenNotAndFlushesOnConnect() {
        feature.onLocalClip("")
        assertTrue(sender.sent.isEmpty())
        feature.onLocalClip("offline") // held
        assertTrue(sender.sent.isEmpty())
        feature.onPeerConnected(peer)
        assertEquals("offline", sender.sent.single().header.getString("data"))
        feature.onLocalClip("offline") // already shared
        assertEquals(1, sender.sent.size)
        feature.onLocalClip("live")
        assertEquals(2, sender.sent.size)
        feature.onPeerDisconnected(peer)
        feature.onLocalClip("later")
        assertEquals(2, sender.sent.size)
    }

    @Test fun offlineImageIsHeldAndReceiptReportedOnReconnect() {
        val got = mutableListOf<Boolean>()
        feature.onLocalImage(png) { got += it }
        assertTrue(got.isEmpty() && sender.sent.isEmpty())
        feature.onPeerConnected(peer)
        val s = sender.sent.single()
        assertEquals("image", s.header.getString("kind"))
        assertArrayEquals(png, s.body)
        assertTrue(got.isEmpty()) // until the PC's receipt arrives
        s.onReply!!(Response(true))
        assertEquals(listOf(true), got)
    }

    @Test fun receiptNullOrFailedMeansNotDelivered() {
        feature.onPeerConnected(peer)
        val got = mutableListOf<Boolean>()
        feature.onLocalImage(png) { got += it }
        sender.sent[0].onReply!!(null)
        feature.onLocalImage(png) { got += it }
        sender.sent[1].onReply!!(Response.FAILED)
        assertEquals(listOf(false, false), got)
    }

    @Test fun secondHeldImageReplacesTheFirstWhichReportsFalse() {
        val a = mutableListOf<Boolean>()
        val b = mutableListOf<Boolean>()
        feature.onLocalImage(png) { a += it }
        feature.onLocalImage(png + byteArrayOf(9)) { b += it }
        assertEquals(listOf(false), a)
        assertTrue(b.isEmpty())
        feature.onPeerConnected(peer)
        assertEquals(1, sender.sent.size)
        sender.sent[0].onReply!!(Response(true))
        assertEquals(listOf(true), b)
        assertEquals(listOf(false), a)
    }

    @Test fun remoteClipReplacingAHeldImageReportsFalse() {
        val got = mutableListOf<Boolean>()
        feature.onLocalImage(png) { got += it }
        feature.onMessage(peer, clip("text") { put("data", "newer") }, ByteArray(0))
        assertEquals(listOf(false), got)
        feature.onPeerConnected(peer)
        assertTrue(sender.sent.isEmpty()) // the held image is gone
    }

    @Test fun oversizeImageIsSkippedAndReportedFalse() {
        feature.onPeerConnected(peer)
        val got = mutableListOf<Boolean>()
        feature.onLocalImage(ByteArray(ImageUtil.MAX_BYTES - 1024 + 1)) { got += it }
        assertEquals(listOf(false), got)
        assertTrue(sender.sent.isEmpty())
        feature.onLocalImage(ByteArray(ImageUtil.MAX_BYTES - 1024)) { got += it } // exactly at the limit is sent
        assertEquals(1, sender.sent.size)
    }

    @Test fun emptyImageReportsFalse() {
        val got = mutableListOf<Boolean>()
        feature.onLocalImage(ByteArray(0)) { got += it }
        assertEquals(listOf(false), got)
    }

    @Test fun bugMalformedClipRequestIsAnsweredOkTrue() {
        expectBug("a malformed clip request gets a delivery receipt (ok:true) although nothing was applied") {
            val router = FeatureRouter().also { it.register(feature) }
            val bad = listOf(
                clip("text"), // no data
                clip("text") { put("data", 5) },
                clip("image") { put("mime", "image/jpeg") },
                clip("video"),
            )
            for (h in bad) {
                val r = router.dispatch(peer, Envelopes.encode(h.put("mid", "m1"), png)) as Routed.Answer
                bugCheck(!r.response.ok, "malformed ${h.getString("kind")} clip acknowledged as ok")
            }
        }
    }
}
