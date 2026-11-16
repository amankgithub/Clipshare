package com.clipbridge

import android.content.SharedPreferences
import com.clipbridge.link.B64
import com.clipbridge.link.Endpoint
import com.clipbridge.link.Requests
import com.clipbridge.link.Response
import com.clipbridge.ui.PairExtras
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** In-memory SharedPreferences (only what [Settings] uses). */
class FakePrefs : SharedPreferences {
    val map = HashMap<String, Any?>()
    override fun getString(key: String, defValue: String?): String? = map[key] as String? ?: defValue
    override fun edit(): SharedPreferences.Editor = object : SharedPreferences.Editor {
        override fun putString(key: String, value: String?) = apply { map[key] = value }
        override fun apply() {}
        override fun commit() = true
        override fun putStringSet(key: String, values: MutableSet<String>?) = this
        override fun putInt(key: String, value: Int) = this
        override fun putLong(key: String, value: Long) = this
        override fun putFloat(key: String, value: Float) = this
        override fun putBoolean(key: String, value: Boolean) = this
        override fun remove(key: String) = apply { map.remove(key) }
        override fun clear() = apply { map.clear() }
    }
    override fun getAll(): MutableMap<String, *> = map
    override fun getStringSet(key: String, defValues: MutableSet<String>?) = defValues
    override fun getInt(key: String, defValue: Int) = defValue
    override fun getLong(key: String, defValue: Long) = defValue
    override fun getFloat(key: String, defValue: Float) = defValue
    override fun getBoolean(key: String, defValue: Boolean) = defValue
    override fun contains(key: String) = map.containsKey(key)
    override fun registerOnSharedPreferenceChangeListener(l: SharedPreferences.OnSharedPreferenceChangeListener) {}
    override fun unregisterOnSharedPreferenceChangeListener(l: SharedPreferences.OnSharedPreferenceChangeListener) {}
}

class PairingSettingsTest {
    private val psk = B64.encode(ByteArray(32) { 1 })
    private val settings = Settings(FakePrefs())

    @Test fun validDeviceAndPskPair() {
        val r = PairExtras.apply(settings, "dev-1", " $psk\n", null)
        assertTrue(r.changed); assertFalse(r.invalidPairing)
        assertEquals("dev-1", settings.pairing!!.device)
        assertEquals("", settings.manualHost)
    }

    @Test fun invalidDeviceOrPskChangesNothing() {
        for ((d, p) in listOf("bad id!" to psk, "dev-1" to "AAAA", "dev-1" to null, null to psk)) {
            val r = PairExtras.apply(settings, d, p, null)
            assertFalse(r.changed); assertTrue(r.invalidPairing)
            assertNull(settings.pairing)
        }
    }

    @Test fun invalidPairingStillAppliesAValidHost() {
        val r = PairExtras.apply(settings, "bad id", psk, "10.0.0.9")
        assertTrue(r.changed); assertTrue(r.invalidPairing)
        assertEquals(Endpoint("10.0.0.9"), settings.manual)
        assertNull(settings.pairing)
    }

    @Test fun hostOnlyKeepsPairingAndTrims() {
        PairExtras.apply(settings, "dev-1", psk, null)
        val r = PairExtras.apply(settings, null, null, "  192.168.1.5:9001 ")
        assertTrue(r.changed); assertFalse(r.invalidPairing)
        assertEquals("dev-1", settings.pairing!!.device)
        assertEquals(Endpoint("192.168.1.5", 9001), settings.manual)
    }

    @Test fun noExtrasChangesNothing() {
        val r = PairExtras.apply(settings, null, null, null)
        assertFalse(r.changed); assertFalse(r.invalidPairing)
    }

    @Test fun rePairWithAHostAlwaysOverwritesTheManualHostEvenWithABlankOne() {
        PairExtras.apply(settings, "dev-1", psk, "10.0.0.9")
        assertEquals(Endpoint("10.0.0.9"), settings.manual)
        PairExtras.apply(settings, "dev-2", psk, "10.0.0.10")
        assertEquals(Endpoint("10.0.0.10"), settings.manual)
        PairExtras.apply(settings, "dev-2", psk, "") // an empty host extra clears it
        assertNull(settings.manual)
        PairExtras.apply(settings, "dev-3", psk, null) // no host extra: kept
        PairExtras.apply(settings, null, null, "10.0.0.11")
        PairExtras.apply(settings, "dev-4", psk, null)
        assertEquals(Endpoint("10.0.0.11"), settings.manual)
    }

    @Test fun invalidManualHostIsStoredButParsesToNull() {
        settings.manualHost = " not a host "
        assertEquals("not a host", settings.manualHost)
        assertNull(settings.manual)
    }

    @Test fun cachedEndpointRoundTripsAndGarbageReadsAsNull() {
        settings.cached = Endpoint("10.1.1.1", 9)
        assertEquals(Endpoint("10.1.1.1", 9), settings.cached)
        settings.cached = null
        assertNull(settings.cached)
    }

    @Test fun storedGarbagePairingReadsAsNull() {
        val p = FakePrefs().also { it.map["pairing"] = "no-colon" }
        assertNull(Settings(p).pairing)
    }

    @Test fun requestsIgnoreRepliesWithANonBooleanOk() {
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
        try {
            val r = Requests(scope)
            val got = mutableListOf<Response?>()
            val mid = r.track { got += it }
            for (ok in listOf<Any>("true", 1, JSONObject.NULL)) {
                r.onControl(JSONObject().put("ch", "link").put("type", "reply").put("mid", mid).put("ok", ok), ByteArray(0))
            }
            r.onControl(JSONObject().put("ch", "link").put("type", "reply").put("mid", mid), ByteArray(0)) // no ok
            r.onControl(JSONObject().put("ch", "link").put("type", "reply").put("mid", 5).put("ok", true), ByteArray(0))
            assertTrue(got.isEmpty()) // still pending; resolves when the link drops
            r.failAll()
            assertEquals(listOf<Response?>(null), got)
        } finally { scope.cancel() }
    }
}
