package com.clipbridge.features.clipboard

import com.clipbridge.link.Feature
import com.clipbridge.link.LinkSender
import com.clipbridge.link.Peer
import android.util.Log
import org.json.JSONObject

/** Clipboard feature (`ch = "clipboard"`): text and PNG image clips, both directions. */
class ClipboardFeature(
    private val sender: LinkSender,
    private val writer: ClipboardWriter,
    private val engine: SyncEngine = SyncEngine(),
) : Feature {
    override val name = "clipboard"

    private val peers = HashSet<Peer>()

    /** Delivery callback of the clip held in the engine's pending slot (explicit image sends only). */
    private var heldReport: ((Boolean) -> Unit)? = null

    override fun onPeerConnected(peer: Peer) {
        synchronized(peers) { peers.add(peer) }
        engine.onConnected()?.let { send(it, takeHeldReport()) }
    }

    private fun takeHeldReport(): ((Boolean) -> Unit)? = synchronized(peers) { heldReport.also { heldReport = null } }

    override fun onPeerDisconnected(peer: Peer) { synchronized(peers) { peers.remove(peer) } }

    override fun onMessage(peer: Peer, header: JSONObject, body: ByteArray) {
        if (header.optString("type") != "clip") return
        val clip: Clip = when (header.optString("kind")) {
            "text" -> Clip.Text(header.opt("data") as? String ?: return)
            "image" -> {
                if (header.optString("mime") != "image/png" || body.isEmpty()) return
                Clip.Image(body)
            }
            else -> return
        }
        val applied = engine.onRemote(clip)
        logClip("remote ${describe(clip)} applied=$applied")
        if (!applied) return
        takeHeldReport()?.invoke(false) // remote clip replaced our unsent one
        when (clip) {
            is Clip.Text -> writer.write(clip.text)
            is Clip.Image -> writer.writeImage(clip.png)
        }
    }

    /** Entry point for any trigger (reader activity, share target, ...). */
    fun onLocalClip(text: String) { if (text.isNotEmpty()) onLocal(Clip.Text(text)) }

    /**
     * Images only come from explicit user actions, so a repeat tap always resends (retry after a lost send).
     * [onDelivered] reports the PC's delivery receipt; while offline the image is held and reported on reconnect.
     */
    fun onLocalImage(png: ByteArray, onDelivered: (Boolean) -> Unit = {}) {
        if (png.isEmpty()) return onDelivered(false)
        onLocal(Clip.Image(png), force = true, onDelivered)
    }

    private fun onLocal(clip: Clip, force: Boolean = false, onDelivered: ((Boolean) -> Unit)? = null) {
        val connected = synchronized(peers) { peers.isNotEmpty() }
        when (val r = engine.onLocal(clip, connected, force)) {
            is SyncEngine.Local.Send -> send(r.clip, onDelivered)
            SyncEngine.Local.Hold -> {
                val replaced = synchronized(peers) { heldReport.also { heldReport = onDelivered } }
                replaced?.invoke(false)
            }
            SyncEngine.Local.Drop -> onDelivered?.invoke(true) // peer already has it
        }
    }

    private fun send(clip: Clip, onDelivered: ((Boolean) -> Unit)? = null) {
        when (clip) {
            is Clip.Text -> {
                logClip("send ${describe(clip)}")
                val h = JSONObject().put("ch", name).put("type", "clip").put("kind", "text")
                    .put("data", clip.text).put("ts", System.currentTimeMillis())
                sender.send(null, h)
            }
            is Clip.Image -> {
                if (clip.png.size > ImageUtil.MAX_BYTES - 1024) {
                    logClip("skip image too big len=${clip.png.size}"); onDelivered?.invoke(false); return
                }
                logClip("send ${describe(clip)}")
                val h = JSONObject().put("ch", name).put("type", "clip").put("kind", "image")
                    .put("mime", "image/png").put("ts", System.currentTimeMillis())
                sender.send(null, h, clip.png) { ok ->
                    logClip("image delivered=$ok")
                    onDelivered?.invoke(ok)
                }
            }
        }
    }

    private fun describe(clip: Clip): String = when (clip) {
        is Clip.Text -> "clip len=${clip.text.length} hash=${clip.text.hashCode()}"
        is Clip.Image -> "image len=${clip.png.size} hash=${SyncEngine.hash(clip).take(8)}"
    }

    /** Lengths/hashes only, never contents. No-op under JVM unit tests. */
    private fun logClip(msg: String) { runCatching { Log.i("ClipBridge", msg) } }
}
