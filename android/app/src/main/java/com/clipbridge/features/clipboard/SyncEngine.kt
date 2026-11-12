package com.clipbridge.features.clipboard

import java.security.MessageDigest

/** A clipboard value: text, or PNG bytes. */
sealed interface Clip {
    data class Text(val text: String) : Clip
    class Image(val png: ByteArray) : Clip {
        override fun equals(other: Any?) = other is Image && png.contentEquals(other.png)
        override fun hashCode() = png.contentHashCode()
    }
}

/**
 * Pure sync rules (docs/PLAN.md). State: lastHash of the current shared clip (text and images
 * share one slot), and a single pending slot for the latest unsent local clip. Last writer wins.
 */
class SyncEngine {
    sealed interface Local {
        data object Drop : Local
        data object Hold : Local
        data class Send(val clip: Clip) : Local
    }

    private var lastHash: String? = null
    private var pending: Clip? = null

    @Synchronized
    fun onLocal(clip: Clip, connected: Boolean): Local {
        val h = hash(clip)
        if (h == lastHash) return Local.Drop
        lastHash = h
        if (connected) { pending = null; return Local.Send(clip) }
        pending = clip
        return Local.Hold
    }

    /** @return true if the caller should write [clip] to the local clipboard. */
    @Synchronized
    fun onRemote(clip: Clip): Boolean {
        val h = hash(clip)
        if (h == lastHash) return false
        lastHash = h
        pending = null
        return true
    }

    /** On (re)connect: flush the pending clip only; never push full state. */
    @Synchronized
    fun onConnected(): Clip? = pending.also { pending = null }

    companion object {
        fun hash(clip: Clip): String = when (clip) {
            is Clip.Text -> sha256(clip.text.toByteArray(Charsets.UTF_8))
            is Clip.Image -> sha256(clip.png)
        }

        fun sha256(b: ByteArray): String =
            MessageDigest.getInstance("SHA-256").digest(b).joinToString("") { "%02x".format(it) }
    }
}
