package com.clipbridge.features.files

import android.content.Context
import com.clipbridge.link.BridgeClient
import com.clipbridge.link.FileFeature
import com.clipbridge.link.FileOffer
import com.clipbridge.link.FileSink
import com.clipbridge.link.LinkSender
import com.clipbridge.link.Peer
import org.json.JSONObject

/**
 * Files feature (`ch = "files"`): uses only the link's file transfer. Received files land in
 * `Downloads/ClipShare` automatically; shared files are queued for the PC.
 */
class FilesFeature(context: Context, private val sender: LinkSender) : FileFeature {
    override val name = "files"

    private val app = context.applicationContext
    private val notifier = FileNotifier(app)

    override fun onPeerConnected(peer: Peer) {}
    override fun onPeerDisconnected(peer: Peer) {}
    override fun onMessage(peer: Peer, header: JSONObject, body: ByteArray) {}

    override fun onFileOffer(peer: Peer, offer: FileOffer, resume: String?): FileSink =
        MediaStoreSink.reopen(app, resume, offer.size) ?: MediaStoreSink.create(app, offer.name, offer.size)

    override fun onFileProgress(peer: Peer, offer: FileOffer, received: Long) =
        notifier.progress(offer.id, "Receiving ${offer.name}", received, offer.size, upload = false)

    override fun onFileReceived(peer: Peer, offer: FileOffer, sink: FileSink) {
        val uri = (sink as MediaStoreSink).uri
        notifier.received(offer.id, offer.name, uri, MediaStoreSink.mimeOf(offer.name))
    }

    override fun onFileFailed(peer: Peer, offer: FileOffer) = notifier.failed(offer.id, "Couldn't receive ${offer.name}")

    /** Queue a shared file for the PC; progress notification, then a delivered/not delivered toast. */
    fun send(source: ContentSource) {
        val key = "out:" + System.nanoTime()
        sender.sendFile(
            peer = BridgeClient.PEER, feature = name, source = source,
            onProgress = { notifier.progress(key, "Sending ${source.name}", it, source.size, upload = true) },
            onDone = { ok ->
                source.close()
                notifier.clear(key)
                notifier.toast(if (ok) "${source.name} delivered to PC" else "${source.name} not delivered to PC")
            },
        )
    }
}
