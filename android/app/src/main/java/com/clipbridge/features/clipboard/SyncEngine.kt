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

    /** [force]: explicit user send (tile/share), resent even if it matches lastHash so a lost send can be retried. */
    @Synchronized
    fun onLocal(clip: Clip, connected: Boolean, force: Boolean = false): Local {
        val h = hash(clip)
        if (!force && h == lastHash) return Local.Drop
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

    /** Count of [onConnected] calls; a send remembers it to tell whether the link reconnected before its failure was known. */
    @get:Synchronized
    var connects = 0
        private set

    /**
     * A sent clip got no delivery receipt ([sentAt] = [connects] when it was sent). Unless a newer clip replaced it:
     * if the link reconnected since and is [connected], @return the clip to send now; otherwise park it for the next connect.
     */
    @Synchronized
    fun onUndelivered(clip: Clip, sentAt: Int, connected: Boolean): Clip? {
        if (hash(clip) != lastHash || pending != null) return null
        if (connected && sentAt != connects) return clip
        pending = clip
        return null
    }

    /** On (re)connect: flush the pending clip only; never push full state. */
    @Synchronized
    fun onConnected(): Clip? { connects++; return pending.also { pending = null } }

    companion object {
        fun hash(clip: Clip): String = when (clip) {
            is Clip.Text -> sha256(clip.text.toByteArray(Charsets.UTF_8))
            is Clip.Image -> sha256(clip.png)
        }

        fun sha256(b: ByteArray): String =
            MessageDigest.getInstance("SHA-256").digest(b).joinToString("") { "%02x".format(it) }
    }
}
