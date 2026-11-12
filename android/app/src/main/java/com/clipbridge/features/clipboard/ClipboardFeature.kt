package com.clipbridge.features.clipboard

import com.clipbridge.link.Feature
import com.clipbridge.link.LinkSender
import com.clipbridge.link.Peer
import org.json.JSONObject

/** Clipboard feature (`ch = "clipboard"`): text clips, both directions. */
class ClipboardFeature(
    private val sender: LinkSender,
    private val writer: ClipboardWriter,
    private val engine: SyncEngine = SyncEngine(),
) : Feature {
    override val name = "clipboard"

    private val peers = HashSet<Peer>()

    override fun onPeerConnected(peer: Peer) {
        synchronized(peers) { peers.add(peer) }
        engine.onConnected()?.let { sendClip(it) }
    }

    override fun onPeerDisconnected(peer: Peer) { synchronized(peers) { peers.remove(peer) } }

    override fun onMessage(peer: Peer, header: JSONObject, body: ByteArray) {
        if (header.optString("type") != "clip" || header.optString("kind") != "text") return
        val data = header.opt("data") as? String ?: return
        if (engine.onRemote(data)) writer.write(data)
    }

    /** Entry point for any trigger (reader activity, share target, ...). */
    fun onLocalClip(text: String) {
        if (text.isEmpty()) return
        val connected = synchronized(peers) { peers.isNotEmpty() }
        when (val r = engine.onLocal(text, connected)) {
            is SyncEngine.Local.Send -> sendClip(r.text)
            else -> {}
        }
    }

    private fun sendClip(text: String) {
        val h = JSONObject().put("ch", name).put("type", "clip").put("kind", "text")
            .put("data", text).put("ts", System.currentTimeMillis())
        sender.send(null, h)
    }
}
