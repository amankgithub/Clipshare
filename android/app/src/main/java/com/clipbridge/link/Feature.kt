package com.clipbridge.link

import org.json.JSONObject

/** A remote device on the link. Clients have exactly one (the server). */
data class Peer(val id: String)

/** Link services a feature may use. `peer == null` means all peers. */
interface LinkSender {
    /** @return false if not sent (not connected, too big). */
    fun send(peer: Peer?, header: JSONObject, body: ByteArray = ByteArray(0)): Boolean
}

/** One module per feature; registered by [name], routed by header "ch". */
interface Feature {
    val name: String
    fun onPeerConnected(peer: Peer)
    fun onPeerDisconnected(peer: Peer)
    fun onMessage(peer: Peer, header: JSONObject, body: ByteArray)
}

/** Delivers decrypted envelopes to the feature named by "ch". No feature knowledge. */
class FeatureRouter(private val log: (String) -> Unit = {}) {
    private val features = LinkedHashMap<String, Feature>()

    fun register(f: Feature) { features[f.name] = f }
    fun peerConnected(p: Peer) = features.values.forEach { safely(it) { it.onPeerConnected(p) } }
    fun peerDisconnected(p: Peer) = features.values.forEach { safely(it) { it.onPeerDisconnected(p) } }

    /** Malformed envelope: dropped and logged, never throws (frame was authenticated). */
    fun dispatch(peer: Peer, plaintext: ByteArray) {
        val env = try { Envelopes.parse(plaintext) } catch (e: EnvelopeException) {
            log("drop malformed envelope: ${e.message}"); return
        }
        val f = features[env.header.getString("ch")] ?: return
        safely(f) { f.onMessage(peer, env.header, env.body) }
    }

    private fun safely(f: Feature, block: () -> Unit) {
        try { block() } catch (e: Exception) { log("feature ${f.name} failed: ${e.javaClass.simpleName}") }
    }
}
