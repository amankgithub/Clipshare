package com.clipbridge.link

import org.json.JSONObject
import java.io.InputStream

/**
 * A remote device on the link, known from its `hello` (PROTOCOL.md section 6.1). Clients have
 * exactly one (the server). Equal by [id].
 */
class Peer(val id: String, val name: String = "", private val features: Map<String, Int> = emptyMap()) {
    /** True when the peer's hello lists feature [name] with a version of at least [version]. */
    fun supports(name: String, version: Int = 1): Boolean = (features[name] ?: 0) >= version

    override fun equals(other: Any?): Boolean = other is Peer && other.id == id
    override fun hashCode(): Int = id.hashCode()
    override fun toString(): String = "Peer($id)"
}

/** Link services a feature may use. `peer == null` means all peers. */
interface LinkSender {
    /**
     * @return false if not sent (not connected, too big).
     * [onReply], when given, sends the message as a request (PROTOCOL.md 6.2) and is called exactly
     * once: with the peer's [Response] (`ok` true when the peer handled it), or null if it was not
     * sent, no reply came in time, or the link dropped first.
     */
    fun send(
        peer: Peer?,
        header: JSONObject,
        body: ByteArray = ByteArray(0),
        onReply: ((Response?) -> Unit)? = null,
    ): Boolean

    /**
     * Queues [source] for the peer's [feature] (PROTOCOL.md 6.3). Unfinished files survive reconnects.
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

/** One module per feature; registered by [name], routed by header "ch", announced in `hello` with [version]. */
interface Feature {
    val name: String
    /** Highest version of this feature's messages this side handles (1 to 2^31-1). */
    val version: Int
    fun onPeerConnected(peer: Peer)
    fun onPeerDisconnected(peer: Peer)
    /**
     * Handles one message. Returning normally means handled: if the peer sent it as a request, the
     * link replies `ok` with the returned [Reply] content (null = plain delivery receipt). Throwing
     * means failed (`ok: false`).
     */
    fun onMessage(peer: Peer, header: JSONObject, body: ByteArray): Reply?
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

/** What [FeatureRouter.dispatch] did with a message; the link acts on it. */
sealed interface Routed {
    /** Nothing more to do: malformed (dropped), or not a request. */
    data object Done : Routed
    /** A request: the link sends [response] back as the reply to [mid]. */
    class Answer(val mid: String, val response: Response) : Routed
    /** A `ch = "link"` message, for the link itself. */
    class LinkControl(val header: JSONObject, val body: ByteArray) : Routed
}

/** Delivers decrypted envelopes to the feature named by "ch". No feature knowledge. */
class FeatureRouter(private val log: (String) -> Unit = {}) {
    private val features = LinkedHashMap<String, Feature>()

    fun register(f: Feature) {
        require(FeatureNames.isFeature(f.name)) { "invalid feature name" }
        require(f.version >= 1) { "invalid feature version" }
        features[f.name] = f
    }

    /** Registered feature names and versions, as announced in `hello`. */
    fun versions(): Map<String, Int> = features.mapValues { it.value.version }

    fun fileFeature(name: String): FileFeature? = features[name] as? FileFeature
    fun peerConnected(p: Peer) = features.values.forEach { safely(it) { it.onPeerConnected(p) } }
    fun peerDisconnected(p: Peer) = features.values.forEach { safely(it) { it.onPeerDisconnected(p) } }

    /** Malformed envelope or invalid `mid`: dropped and logged, never throws (frame was authenticated). */
    fun dispatch(peer: Peer, plaintext: ByteArray): Routed {
        val env = try { Envelopes.parse(plaintext) } catch (e: EnvelopeException) {
            log("drop malformed envelope: ${e.message}"); return Routed.Done
        }
        val ch = env.header.getString("ch")
        if (ch.startsWith(LINK_CH)) return Routed.LinkControl(env.header, env.body)
        val mid = Requests.requestedMid(env.header)
        if (mid == null && Requests.hasMid(env.header)) { log("drop message with invalid mid"); return Routed.Done }
        val f = features[ch]
        val response = if (f == null) Response.FAILED else {
            var reply: Reply? = null
            if (safely(f) { reply = f.onMessage(peer, env.header, env.body) }) Response(true, reply?.data, reply?.body ?: ByteArray(0))
            else Response.FAILED
        }
        return if (mid == null) Routed.Done else Routed.Answer(mid, response)
    }

    /** @return false if the feature threw. */
    private fun safely(f: Feature, block: () -> Unit): Boolean =
        try { block(); true } catch (e: Exception) { log("feature ${f.name} failed: ${e.javaClass.simpleName}"); false }
}
