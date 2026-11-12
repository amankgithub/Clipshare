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

    override fun onPeerConnected(peer: Peer) {
        synchronized(peers) { peers.add(peer) }
        engine.onConnected()?.let { send(it) }
    }

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
        when (clip) {
            is Clip.Text -> writer.write(clip.text)
            is Clip.Image -> writer.writeImage(clip.png)
        }
    }

    /** Entry point for any trigger (reader activity, share target, ...). */
    fun onLocalClip(text: String) { if (text.isNotEmpty()) onLocal(Clip.Text(text)) }

    fun onLocalImage(png: ByteArray) { if (png.isNotEmpty()) onLocal(Clip.Image(png)) }

    private fun onLocal(clip: Clip) {
        val connected = synchronized(peers) { peers.isNotEmpty() }
        when (val r = engine.onLocal(clip, connected)) {
            is SyncEngine.Local.Send -> send(r.clip)
            else -> {}
        }
    }

    private fun send(clip: Clip) {
        when (clip) {
            is Clip.Text -> {
                logClip("send ${describe(clip)}")
                val h = JSONObject().put("ch", name).put("type", "clip").put("kind", "text")
                    .put("data", clip.text).put("ts", System.currentTimeMillis())
                sender.send(null, h)
            }
            is Clip.Image -> {
                if (clip.png.size > ImageUtil.MAX_BYTES - 1024) {
                    logClip("skip image too big len=${clip.png.size}"); return
                }
                logClip("send ${describe(clip)}")
                val h = JSONObject().put("ch", name).put("type", "clip").put("kind", "image")
                    .put("mime", "image/png").put("ts", System.currentTimeMillis())
                sender.send(null, h, clip.png)
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
