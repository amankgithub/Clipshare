package com.clipbridge

import com.clipbridge.link.B64
import com.clipbridge.link.Backoff
import com.clipbridge.link.Crypto
import com.clipbridge.link.Endpoint
import com.clipbridge.link.EnvelopeException
import com.clipbridge.link.Envelopes
import com.clipbridge.link.Feature
import com.clipbridge.link.FeatureRouter
import com.clipbridge.link.FrameCipher
import com.clipbridge.link.FrameException
import com.clipbridge.link.Hello
import com.clipbridge.link.Reply
import com.clipbridge.link.Requests
import com.clipbridge.link.Response
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import com.clipbridge.link.Peer
import org.json.JSONObject
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File

class ProtocolTest {
    private val v: JSONObject = run {
        val f = listOf("../../protocol/test_vectors.json", "../protocol/test_vectors.json")
            .map(::File).first { it.exists() }
        JSONObject(f.readText())
    }

    private fun hex(s: String): ByteArray = ByteArray(s.length / 2) { s.substring(2 * it, 2 * it + 2).toInt(16).toByte() }
    private fun h(k: String) = hex(v.getString(k))

    @Test fun macs() {
        val psk = h("psk"); val sn = h("sn"); val cn = h("cn")
        assertArrayEquals(h("client_mac"), Crypto.clientMac(psk, sn, cn, v.getString("device")))
        assertArrayEquals(h("server_mac"), Crypto.serverMac(psk, sn, cn))
        assertArrayEquals(h("psk"), B64.decode(v.getString("psk_b64"), 32))
        assertArrayEquals(h("client_mac"), B64.decode(v.getString("client_mac_b64"), 32))
    }

    @Test fun keys() {
        val (c2s, s2c) = Crypto.sessionKeys(h("psk"), h("sn"), h("cn"))
        assertArrayEquals(h("key_c2s"), c2s)
        assertArrayEquals(h("key_s2c"), s2c)
    }

    @Test fun frames() {
        val frames = v.getJSONArray("frames")
        assertTrue(frames.length() > 0)
        for (i in 0 until frames.length()) {
            val f = frames.getJSONObject(i)
            val key = h(if (f.getString("direction") == "c2s") "key_c2s" else "key_s2c")
            val counter = f.getLong("counter")
            val plaintext = hex(f.getString("plaintext"))
            val frame = hex(f.getString("frame"))
            assertArrayEquals(frame, Crypto.encryptFrame(key, counter, plaintext))
            assertArrayEquals(plaintext, Crypto.decryptFrame(key, counter, frame))
            val env = Envelopes.parse(plaintext)
            assertEquals(f.getJSONObject("header").toString(), JSONObject(f.getJSONObject("header").toString()).toString())
            assertEquals(f.getJSONObject("header").getString("ch"), env.header.getString("ch"))
            assertArrayEquals(hex(f.getString("body")), env.body)
        }
    }

    /** Envelopes of the vector frames whose header has `type` == [type]. */
    private fun vectorEnvelopes(type: String) = (0 until v.getJSONArray("frames").length())
        .map { v.getJSONArray("frames").getJSONObject(it) }
        .filter { it.getJSONObject("header").optString("type") == type }
        .map { Envelopes.parse(hex(it.getString("plaintext"))) }

    @Test fun helloFrames() {
        val hellos = vectorEnvelopes("hello")
        assertEquals(2, hellos.size)
        for (e in hellos) {
            val hello = Hello.parse(e.header, e.body)!!
            assertTrue(hello.name.isNotEmpty())
            assertTrue(hello.features.isNotEmpty())
            // Our own hello for the same content round-trips through the parser.
            val again = Hello.parse(Envelopes.parse(Envelopes.encode(hello.header())).header, ByteArray(0))!!
            assertEquals(hello.name, again.name)
            assertEquals(hello.features, again.features)
        }
    }

    @Test fun replyFrames() {
        val replies = vectorEnvelopes("reply")
        assertEquals(2, replies.size)
        val scope = CoroutineScope(Dispatchers.Default)
        val got = ArrayList<Response?>()
        for (e in replies) {
            val r = Requests(scope)
            val mid = r.track { got += it }
            e.header.put("mid", mid) // the vector's mid belongs to the vector's sender
            r.onControl(e.header, e.body)
        }
        assertEquals(2, got.size)
        assertTrue(got.all { it!!.ok })
        val empty = got.first { it!!.data == null }!!
        assertEquals(0, empty.body.size)
        val withData = got.first { it!!.data != null }!!
        assertEquals(8, withData.body.size)
    }

    @Test fun frameRejections() {
        val key = h("key_c2s")
        val good = Crypto.encryptFrame(key, 0, "x".toByteArray())
        fun bad(frame: ByteArray, counter: Long = 0) {
            try { Crypto.decryptFrame(key, counter, frame); fail() } catch (_: FrameException) {}
        }
        bad(ByteArray(27))
        bad(good, counter = 1)
        bad(good.copyOf().also { it[9] = 1 })
        bad(good.copyOf().also { it[it.size - 1] = (it[it.size - 1] + 1).toByte() })
        val c = FrameCipher(key, key)
        val a = c.encrypt("a".toByteArray()); val b = c.encrypt("b".toByteArray())
        try { c.decrypt(b); fail() } catch (_: FrameException) {}
        assertArrayEquals("a".toByteArray(), c.decrypt(a))
        assertArrayEquals("b".toByteArray(), c.decrypt(b))
    }

    @Test fun envelopeEmojiAndBinary() {
        val text = "hi 😀 é 中"
        val body = ByteArray(256) { it.toByte() }
        val env = Envelopes.parse(Envelopes.encode(JSONObject().put("ch", "clipboard").put("data", text), body))
        assertEquals(text, env.header.getString("data"))
        assertArrayEquals(body, env.body)
        // \u-escaped surrogate pair from another sender
        val raw = """{"ch":"x","d":"😀"}""".toByteArray()
        val pt = java.nio.ByteBuffer.allocate(4 + raw.size).putInt(raw.size).put(raw).array()
        assertEquals("😀", Envelopes.parse(pt).header.getString("d"))
    }

    @Test fun envelopeMalformed() {
        fun bad(b: ByteArray) { try { Envelopes.parse(b); fail() } catch (_: EnvelopeException) {} }
        bad(byteArrayOf(0, 0))
        bad(byteArrayOf(0, 0, 0, 9, 1))
        bad(byteArrayOf(0, 0, 0, 2, '['.code.toByte(), ']'.code.toByte()))
        bad(byteArrayOf(0, 0, 0, 2, '{'.code.toByte(), '}'.code.toByte()))
        bad(byteArrayOf(0, 0, 0, 1, 0xff.toByte()))
    }

    @Test fun routerRoutesByChAndIgnoresUnknown() {
        val got = mutableListOf<String>()
        val logs = mutableListOf<String>()
        val r = FeatureRouter { logs.add(it) }
        r.register(object : Feature {
            override val name = "a"
            override val version = 1
            override fun onPeerConnected(peer: Peer) {}
            override fun onPeerDisconnected(peer: Peer) {}
            override fun onMessage(peer: Peer, header: JSONObject, body: ByteArray): Reply? { got.add(header.getString("k")); return null }
        })
        r.dispatch(Peer("p"), Envelopes.encode(JSONObject().put("ch", "a").put("k", "1")))
        r.dispatch(Peer("p"), Envelopes.encode(JSONObject().put("ch", "zzz").put("k", "2")))
        r.dispatch(Peer("p"), byteArrayOf(1))
        assertEquals(listOf("1"), got)
        assertEquals(1, logs.size)
    }

    @Test fun backupIsDisabledAndPairingKeyExcluded() {
        val manifest = java.io.File("src/main/AndroidManifest.xml").readText()
        assertTrue(manifest.contains("android:allowBackup=\"false\""))
        assertTrue(manifest.contains("android:dataExtractionRules=\"@xml/data_extraction_rules\""))
        val rules = java.io.File("src/main/res/xml/data_extraction_rules.xml").readText()
        for (section in listOf("cloud-backup", "device-transfer")) {
            val body = rules.substringAfter("<$section>").substringBefore("</$section>")
            assertTrue(section, body.contains("<exclude domain=\"sharedpref\""))
            assertTrue(section, !body.contains("<include"))
        }
    }

    @Test fun endpointsAndBackoff() {
        assertEquals(Endpoint("1.2.3.4", 8765), Endpoint.parse("1.2.3.4"))
        assertEquals(Endpoint("1.2.3.4", 9), Endpoint.parse("1.2.3.4:9"))
        assertNull(Endpoint.parse(" "))
        assertNull(Endpoint.parse("h:99999"))
        assertNull(Endpoint.parse("fe80::1"))
        assertNull(Endpoint.parse("a b"))
        assertNull(Endpoint.parse("a b:9"))
        assertNull(Endpoint.parse("h:"))
        assertNull(Endpoint.parse("h:0"))
        assertNull(Endpoint.parse("h:+9"))
        assertNull(Endpoint.parse(":9"))
        assertNull(Endpoint.parse("h/x"))
        assertEquals(Endpoint("pc.local", 9), Endpoint.parse("pc.local:9"))
        val m = Endpoint("m"); val d = Endpoint("d"); val c = Endpoint("c")
        assertEquals(listOf(m, d, c), Endpoint.order(m, d, c))
        assertEquals(listOf(d, c), Endpoint.order(null, d, c))
        assertEquals(listOf(m, c), Endpoint.order(m, m, c))
        val b = Backoff { 0.5 }
        val seq = (1..6).map { b.nextMs() }
        assertEquals(listOf(1000L, 2000L, 4000L, 8000L, 10000L, 10000L), seq)
        b.reset(); assertEquals(1000L, b.nextMs())
    }
}
