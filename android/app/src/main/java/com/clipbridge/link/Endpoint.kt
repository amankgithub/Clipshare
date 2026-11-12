package com.clipbridge.link

data class Endpoint(val host: String, val port: Int = DEFAULT_PORT) {
    override fun toString() = "$host:$port"

    companion object {
        const val DEFAULT_PORT = 8765

        /** "host" or "host:port"; null if blank/invalid. */
        fun parse(s: String?): Endpoint? {
            val t = s?.trim().orEmpty()
            if (t.isEmpty()) return null
            val i = t.lastIndexOf(':')
            if (i < 0) return Endpoint(t)
            val port = t.substring(i + 1).toIntOrNull()?.takeIf { it in 1..65535 } ?: return null
            val host = t.substring(0, i).takeIf { it.isNotEmpty() } ?: return null
            return Endpoint(host, port)
        }

        /** Connection order: manual > mDNS > cached. */
        fun order(manual: Endpoint?, mdns: Endpoint?, cached: Endpoint?): List<Endpoint> =
            listOfNotNull(manual, mdns, cached).distinct()
    }
}

/** Reconnect delay: 1 s doubling, capped at 10 s, +-20 % jitter. */
class Backoff(private val rnd: () -> Double = { Math.random() }) {
    private var base = 1000L
    fun reset() { base = 1000L }
    fun nextMs(): Long {
        val d = (base * (0.8 + 0.4 * rnd())).toLong()
        base = minOf(base * 2, 10_000L)
        return d
    }
}
