package com.clipbridge.link

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import java.net.Inet4Address

/** mDNS discovery of `_clipbridge._tcp`. Resolves are serialized (NsdManager limitation). */
class Discovery(context: Context) {
    private val nsd = context.getSystemService(NsdManager::class.java)
    private val _found = MutableStateFlow<Endpoint?>(null)
    val found: StateFlow<Endpoint?> = _found

    private val queue = ArrayDeque<NsdServiceInfo>()
    private var resolving = false
    private var running = false

    private val listener = object : NsdManager.DiscoveryListener {
        override fun onServiceFound(s: NsdServiceInfo) = synchronized(this@Discovery) { queue.add(s); next() }
        // Lost: keep the last endpoint; the client retries and falls back to the cached one anyway.
        override fun onServiceLost(s: NsdServiceInfo) {}
        override fun onDiscoveryStarted(t: String) {}
        override fun onDiscoveryStopped(t: String) {}
        override fun onStartDiscoveryFailed(t: String, e: Int) { synchronized(this@Discovery) { running = false } }
        override fun onStopDiscoveryFailed(t: String, e: Int) {}
    }

    fun start() = synchronized(this) {
        if (running) return
        running = true
        nsd.discoverServices(TYPE, NsdManager.PROTOCOL_DNS_SD, listener)
    }

    fun stop() = synchronized(this) {
        if (!running) return
        running = false
        try { nsd.stopServiceDiscovery(listener) } catch (_: IllegalArgumentException) {}
    }

    private fun next() {
        if (resolving) return
        val s = queue.removeFirstOrNull() ?: return
        resolving = true
        @Suppress("DEPRECATION")
        nsd.resolveService(s, object : NsdManager.ResolveListener {
            override fun onResolveFailed(i: NsdServiceInfo, e: Int) = done()
            override fun onServiceResolved(i: NsdServiceInfo) {
                @Suppress("DEPRECATION")
                val h = i.host
                val addr = (h as? Inet4Address)?.hostAddress
                if (addr != null) _found.value = Endpoint(addr, i.port)
                done()
            }
            private fun done() = synchronized(this@Discovery) { resolving = false; next() }
        })
    }

    companion object { const val TYPE = "_clipbridge._tcp" }
}
