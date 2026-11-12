package com.clipbridge.link

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network

/** Reports default-network changes so the client can retry immediately. */
class NetworkWatcher(context: Context, private val onAvailable: () -> Unit, private val onLost: () -> Unit) {
    private val cm = context.getSystemService(ConnectivityManager::class.java)
    private val cb = object : ConnectivityManager.NetworkCallback() {
        override fun onAvailable(network: Network) = onAvailable()
        override fun onLost(network: Network) = onLost()
    }
    fun start() = cm.registerDefaultNetworkCallback(cb)
    fun stop() { try { cm.unregisterNetworkCallback(cb) } catch (_: IllegalArgumentException) {} }
}
