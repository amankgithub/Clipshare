package com.clipbridge

import android.content.Context
import com.clipbridge.link.B64
import com.clipbridge.link.Endpoint
import java.security.SecureRandom

/** App-private prefs: PSK (b64), device id, manual host, last good endpoint. Never logged. */
class Settings(context: Context) {
    private val p = context.getSharedPreferences("clipbridge", Context.MODE_PRIVATE)

    val deviceId: String
        get() = p.getString("device", null) ?: ("android-" + ByteArray(4).also { SecureRandom().nextBytes(it) }
            .joinToString("") { "%02x".format(it) }).also { p.edit().putString("device", it).apply() }

    val psk: ByteArray? get() = p.getString("psk", null)?.let { B64.decode(it, 32) }

    /** @return false if [b64] is not a valid 32-byte PSK. */
    fun setPsk(b64: String): Boolean {
        val t = b64.trim()
        if (B64.decode(t, 32) == null) return false
        p.edit().putString("psk", t).apply()
        return true
    }

    var manualHost: String
        get() = p.getString("manual", "") ?: ""
        set(v) { p.edit().putString("manual", v.trim()).apply() }

    val manual: Endpoint? get() = Endpoint.parse(manualHost)

    var cached: Endpoint?
        get() = Endpoint.parse(p.getString("cached", null))
        set(v) { p.edit().putString("cached", v?.toString()).apply() }
}
