package com.clipbridge.link

import org.json.JSONObject
import java.io.InputStream

/** A remote device on the link. Clients have exactly one (the server). */
data class Peer(val id: String)

/** Link services a feature may use. `peer == null` means all peers. */
interface LinkSender {
    /**
     * @return false if not sent (not connected, too big).
     * [onDelivered], when given, asks the peer for a delivery receipt (PROTOCOL.md 6.1) and is called
     * exactly once: true when the peer handled the message, false if it was not sent, timed out, or
     * the link dropped first.
     */
    fun send(
        peer: Peer?,
        header: JSONObject,
        body: ByteArray = ByteArray(0),
        onDelivered: ((Boolean) -> Unit)? = null,
    ): Boolean

    /**
     * Queues [source] for the peer's [feature] (PROTOCOL.md 6.2). Unfinished files survive reconnects.
     * [onProgress] reports bytes the peer has (from any thread); [onDone] is called exactly once:
     * true when the peer has the whole file in its final place, false if rejected, cancelled or corrupt.
     */
    fun sendFile(
        peer: Peer,
        feature: String,
        source: FileSource,
        meta: JSONObject = JSONObject(),
        onProgress: (Long) -> Unit = {},
        onDone: (Boolean) -> Unit = {},
    )
}

/** One module per feature; registered by [name], routed by header "ch". */
interface Feature {
    val name: String
    fun onPeerConnected(peer: Peer)
    fun onPeerDisconnected(peer: Peer)
    fun onMessage(peer: Peer, header: JSONObject, body: ByteArray)
}

/** A file to send. */
interface FileSource {
    val name: String
    val size: Long
    /** A fresh stream positioned at byte 0. */
    fun open(): InputStream
}

/** Where a received file is written; chosen by [FileFeature.onFileOffer]. */
interface FileSink {
    /** Names this destination across app restarts; given back to [FileFeature.onFileOffer] as `resume`. */
    val token: String
    fun write(position: Long, data: ByteArray)
    /** Reads up to [length] bytes at [position] into the start of [into]; returns the count (0 or less at end of file). */
    fun read(position: Long, into: ByteArray, length: Int): Int
    /** Makes written bytes durable (the link then records them as resumable). */
    fun sync()
    /** Every byte arrived and matched: put the file in its final, visible place. */
    fun commit()
    /** Deletes the partial file. */
    fun discard()
}

/** A feature that takes files. */
interface FileFeature : Feature {
    /**
     * Where to write [offer], or null to reject it. [resume] is the [FileSink.token] this feature
     * returned for the same offer before (unfinished transfer), if the link still has it.
     */
    fun onFileOffer(peer: Peer, offer: FileOffer, resume: String?): FileSink?
    /** Bytes of [offer] on disk so far. */
    fun onFileProgress(peer: Peer, offer: FileOffer, received: Long) {}
    fun onFileReceived(peer: Peer, offer: FileOffer, sink: FileSink)
    /** Hash mismatch or cancelled; the partial file is already discarded. */
    fun onFileFailed(peer: Peer, offer: FileOffer) {}
}

/** What [FeatureRouter.dispatch] did with a message; the link acts on it (receipts). */
sealed interface Routed {
    /** Malformed, unknown `ch`, or the feature failed: no receipt. */
    data object Dropped : Routed
    /** A feature handled it; [mid] is the receipt the sender asked for, if any. */
    data class Handled(val mid: String?) : Routed
    /** A `ch = "link"` control message, for the link itself. */
    data class LinkControl(val header: JSONObject) : Routed
}

/** Delivers decrypted envelopes to the feature named by "ch". No feature knowledge. */
class FeatureRouter(private val log: (String) -> Unit = {}) {
    private val features = LinkedHashMap<String, Feature>()

    fun register(f: Feature) { features[f.name] = f }
    fun fileFeature(name: String): FileFeature? = features[name] as? FileFeature
    fun peerConnected(p: Peer) = features.values.forEach { safely(it) { it.onPeerConnected(p) } }
    fun peerDisconnected(p: Peer) = features.values.forEach { safely(it) { it.onPeerDisconnected(p) } }

    /** Malformed envelope: dropped and logged, never throws (frame was authenticated). */
    fun dispatch(peer: Peer, plaintext: ByteArray): Routed {
        val env = try { Envelopes.parse(plaintext) } catch (e: EnvelopeException) {
            log("drop malformed envelope: ${e.message}"); return Routed.Dropped
        }
        val ch = env.header.getString("ch")
        if (ch.startsWith(Receipts.LINK_CH)) return Routed.LinkControl(env.header)
        val f = features[ch] ?: return Routed.Dropped
        if (!safely(f) { f.onMessage(peer, env.header, env.body) }) return Routed.Dropped
        return Routed.Handled(Receipts.requestedMid(env.header))
    }

    /** @return false if the feature threw. */
    private fun safely(f: Feature, block: () -> Unit): Boolean =
        try { block(); true } catch (e: Exception) { log("feature ${f.name} failed: ${e.javaClass.simpleName}"); false }
}
