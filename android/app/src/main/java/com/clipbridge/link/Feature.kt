package com.clipbridge.link

import org.json.JSONObject

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
}

/** One module per feature; registered by [name], routed by header "ch". */
interface Feature {
    val name: String
    fun onPeerConnected(peer: Peer)
    fun onPeerDisconnected(peer: Peer)
    fun onMessage(peer: Peer, header: JSONObject, body: ByteArray)
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
