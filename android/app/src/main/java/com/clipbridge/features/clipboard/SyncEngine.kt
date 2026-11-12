package com.clipbridge.features.clipboard

import java.security.MessageDigest

/**
 * Pure sync rules (docs/PLAN.md). State: lastHash of the current shared text,
 * and a single pending slot for the latest unsent local clip. Last writer wins.
 */
class SyncEngine {
    sealed interface Local {
        data object Drop : Local
        data object Hold : Local
        data class Send(val text: String) : Local
    }

    private var lastHash: String? = null
    private var pending: String? = null

    @Synchronized
    fun onLocal(text: String, connected: Boolean): Local {
        val h = hash(text)
        if (h == lastHash) return Local.Drop
        lastHash = h
        if (connected) { pending = null; return Local.Send(text) }
        pending = text
        return Local.Hold
    }

    /** @return true if the caller should write [text] to the local clipboard. */
    @Synchronized
    fun onRemote(text: String): Boolean {
        val h = hash(text)
        if (h == lastHash) return false
        lastHash = h
        pending = null
        return true
    }

    /** On (re)connect: flush the pending clip only; never push full state. */
    @Synchronized
    fun onConnected(): String? = pending.also { pending = null }

    companion object {
        fun hash(text: String): String =
            MessageDigest.getInstance("SHA-256").digest(text.toByteArray(Charsets.UTF_8))
                .joinToString("") { "%02x".format(it) }
    }
}
