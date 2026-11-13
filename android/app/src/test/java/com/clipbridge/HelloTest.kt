package com.clipbridge

import com.clipbridge.link.B64
import com.clipbridge.link.Feature
import com.clipbridge.link.FeatureRouter
import com.clipbridge.link.Hello
import com.clipbridge.link.Pairing
import com.clipbridge.link.Peer
import com.clipbridge.link.Reply
import org.json.JSONObject
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class HelloTest {
    private fun hello(name: Any = "Pixel 8", features: Any = JSONObject().put("clipboard", 1).put("files", 1)): JSONObject =
        JSONObject().put("ch", "link").put("type", "hello").put("name", name).put("features", features)

    private fun valid(h: JSONObject, body: ByteArray = ByteArray(0)) = Hello.parse(h, body) != null

    @Test fun parsesValidHello() {
        val h = Hello.parse(hello(), ByteArray(0))!!
        assertEquals("Pixel 8", h.name)
        assertEquals(mapOf("clipboard" to 1, "files" to 1), h.features)
        assertTrue(valid(hello(features = JSONObject())))
        assertTrue(valid(hello(name = "x".repeat(64))))
        assertTrue(valid(hello(name = "😀".repeat(64)))) // 64 code points, 128 UTF-16 units
        assertTrue(valid(hello(features = JSONObject().put("a.b-c_9", Int.MAX_VALUE))))
        assertTrue(valid(hello(features = JSONObject().put("a", 2147483647L))))
    }

    @Test fun rejectsInvalidHello() {
        assertFalse(valid(hello(), byteArrayOf(0)))
        assertFalse(valid(hello().put("type", "helo")))
        assertFalse(valid(hello().put("ch", "clipboard")))
        assertFalse(valid(hello().apply { remove("name") }))
        assertFalse(valid(hello().apply { remove("features") }))
        assertFalse(valid(hello(name = "")))
        assertFalse(valid(hello(name = 5)))
        assertFalse(valid(hello(name = "x".repeat(65))))
        assertFalse(valid(hello(name = "😀".repeat(65))))
        assertFalse(valid(hello(name = "a\nb")))
        assertFalse(valid(hello(name = "a\u007fb")))
        assertFalse(valid(hello(features = "clipboard")))
        val many = JSONObject().also { o -> repeat(65) { o.put("f$it", 1) } }
        assertFalse(valid(hello(features = many)))
        assertTrue(valid(hello(features = JSONObject().also { o -> repeat(64) { o.put("f$it", 1) } })))
        for (bad in listOf<Any>(0, -1, 2147483648L, 1.5, "1", true)) {
            assertFalse("$bad", valid(hello(features = JSONObject().put("clipboard", bad))))
        }
        for (key in listOf("", "link", "linkx", "a b", "x".repeat(65), "é")) {
            assertFalse(key, valid(hello(features = JSONObject().put(key, 1))))
        }
    }

    @Test fun deviceNameIsAlwaysValid() {
        assertEquals("Pixel 8", Hello.deviceName(" Pixel 8 "))
        assertEquals("Android", Hello.deviceName(null))
        assertEquals("Android", Hello.deviceName("\n\t"))
        assertEquals(64, Hello.deviceName("x".repeat(100)).length)
        val emoji = Hello.deviceName("😀".repeat(100))
        assertEquals(64, emoji.codePointCount(0, emoji.length))
        assertTrue(Hello.isValidName(Hello.deviceName("a\u0000b")))
    }

    @Test fun ownHelloListsRegisteredFeatures() {
        val r = FeatureRouter()
        fun feature(n: String, v: Int) = object : Feature {
            override val name = n
            override val version = v
            override fun onPeerConnected(peer: Peer) {}
            override fun onPeerDisconnected(peer: Peer) {}
            override fun onMessage(peer: Peer, header: JSONObject, body: ByteArray): Reply? = null
        }
        r.register(feature("clipboard", 1)); r.register(feature("files", 2))
        val parsed = Hello.parse(Hello("Pixel", r.versions()).header(), ByteArray(0))!!
        assertEquals(mapOf("clipboard" to 1, "files" to 2), parsed.features)
        for (bad in listOf(feature("link", 1), feature("a b", 1), feature("ok", 0))) {
            try { r.register(bad); fail(bad.name) } catch (_: IllegalArgumentException) {}
        }
    }

    @Test fun peerSupports() {
        val p = Peer("server", "PC", mapOf("clipboard" to 2))
        assertTrue(p.supports("clipboard"))
        assertTrue(p.supports("clipboard", 2))
        assertFalse(p.supports("clipboard", 3))
        assertFalse(p.supports("files"))
        assertEquals(Peer("server"), p)
    }

    @Test fun pairingCode() {
        val key = ByteArray(32) { it.toByte() }
        val b64 = B64.encode(key)
        val p = Pairing.parseCode("  phone-1.a_b:$b64 \n")!!
        assertEquals("phone-1.a_b", p.device)
        assertArrayEquals(key, p.psk)
        assertEquals("phone-1.a_b:$b64", p.code())
        assertNotNull(Pairing.parseCode(Pairing.parseCode("d:$b64")!!.code()))
        assertNull(Pairing.parseCode(b64))                       // no device
        assertNull(Pairing.parseCode(":$b64"))
        assertNull(Pairing.parseCode("d:"))
        assertNull(Pairing.parseCode("a b:$b64"))
        assertNull(Pairing.parseCode("x".repeat(65) + ":$b64"))
        assertNull(Pairing.parseCode("d:" + B64.encode(ByteArray(31))))
        assertNull(Pairing.parseCode("d:" + b64.trimEnd('=')))  // padding required
        assertNull(Pairing.parseCode("d:$b64:x"))
        assertNull(Pairing.of("d", "not base64"))
    }
}
