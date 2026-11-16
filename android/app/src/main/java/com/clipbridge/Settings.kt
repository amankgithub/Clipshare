package com.clipbridge

import android.content.Context
import android.content.SharedPreferences
import com.clipbridge.link.Endpoint
import com.clipbridge.link.Pairing

/** App-private prefs: pairing (device id + PSK), manual host, last good endpoint. Never logged. */
class Settings(private val p: SharedPreferences) {
    constructor(context: Context) : this(context.getSharedPreferences("clipbridge", Context.MODE_PRIVATE))

    /** Device id and PSK assigned by the PC, stored as one pairing code; null until paired. */
    var pairing: Pairing?
        get() = p.getString("pairing", null)?.let { Pairing.parseCode(it) }
        set(v) { p.edit().putString("pairing", v?.code()).apply() }

    var manualHost: String
        get() = p.getString("manual", "") ?: ""
        set(v) { p.edit().putString("manual", v.trim()).apply() }

    val manual: Endpoint? get() = Endpoint.parse(manualHost)

    var cached: Endpoint?
        get() = Endpoint.parse(p.getString("cached", null))
        set(v) { p.edit().putString("cached", v?.toString()).apply() }
}
