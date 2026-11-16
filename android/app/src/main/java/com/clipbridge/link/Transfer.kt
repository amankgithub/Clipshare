package com.clipbridge.link

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.async
import kotlinx.coroutines.cancel
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.channels.ReceiveChannel
import kotlinx.coroutines.channels.produce
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.OkHttpClient
import org.json.JSONObject
import java.io.EOFException
import java.io.IOException
import java.io.InputStream
import java.security.MessageDigest
import kotlin.coroutines.CoroutineContext

/**
 * File transfer service (PROTOCOL.md section 6.2), socket and disk side. Pure rules live in [TransferCore].
 *
 * - Control messages go over the control connection via [control]; the client (this side) dials every
 *   data connection, whichever side sends the file.
 * - Outgoing files wait in an in-memory FIFO; up to [TransferCore.FILES_IN_FLIGHT] are offered at once and the
 *   rest wait their turn. An unfinished file is re-offered with the same `id` after a reconnect.
 * - Range work runs on [TransferCore.FILES_IN_FLIGHT] workers per direction (send, receive) per control connection,
 *   each keeping one data connection open while there is work, so a small file never waits behind a big one.
 * - Received ranges are checkpointed (synced bytes + state file) so a reconnect resumes them.
 */
class Transfer(
    parent: CoroutineScope,
    private val http: OkHttpClient,
    private val router: FeatureRouter,
    private val store: PartialStore,
    private val control: (JSONObject) -> Boolean,
    private val dropLink: () -> Unit,
    private val onBusy: (Boolean) -> Unit,
    private val log: (String) -> Unit = {},
) {
    private enum class Outcome { DELIVERED, REJECTED, CORRUPT, CANCELLED }

    private class Outgoing(
        val peer: Peer,
        var offer: FileOffer,
        val source: FileSource,
        val onProgress: (Long) -> Unit,
        val onDone: (Boolean) -> Unit,
    ) {
        var retried = false
        /** Being offered on the current link; guarded by [Transfer.lock]. */
        var sending = false
        /** Monotonic time of the last reply or range progress; the offer is given up after [STALL_MS] without either. */
        @Volatile var activeMs = 0L
        @Volatile var reply = CompletableDeferred<JSONObject>()
        @Volatile var done = CompletableDeferred<Outcome>()
    }

    private class Incoming(val peer: Peer, val offer: FileOffer, val feature: FileFeature, val sink: FileSink, val ranges: List<Range>) {
        val ended = HashSet<Long>()
        @Volatile var over = false
        @Volatile var lastProgressMs = 0L
        fun received(): Long = ranges.sumOf { it.have }
        fun state() = PartialState(offer, sink.token, ranges.map { it.copy() }, System.currentTimeMillis())
    }

    private sealed class RangeJob(val id: String, val range: Range) {
        class Send(val out: Outgoing, range: Range) : RangeJob(out.offer.id, range)
        class Recv(val inc: Incoming, range: Range) : RangeJob(inc.offer.id, range)
    }

    /** One control connection's lifetime: its peer, credentials for data connections, and its workers. */
    private class Link(val peer: Peer, val pairing: Pairing, val endpoint: Endpoint, parent: CoroutineContext) {
        val job = SupervisorJob(parent[Job])
        /** Keeps the parent's context (its CoroutineExceptionHandler included), with its own job. */
        val scope = CoroutineScope(parent + job + Dispatchers.IO)
        /** Sends and receives queue apart, so a large file in one direction never holds up the other. */
        val sends = Channel<RangeJob>(Channel.UNLIMITED)
        val recvs = Channel<RangeJob>(Channel.UNLIMITED)
    }

    /** The parent's context (its CoroutineExceptionHandler included) with a child supervisor job. */
    private val scope = CoroutineScope(parent.coroutineContext + SupervisorJob(parent.coroutineContext[Job]) + Dispatchers.IO)
    private val lock = Any()
    private val outbox = ArrayDeque<Outgoing>()
    private val incoming = HashMap<String, Incoming>()
    /** Loaded on first use (on an IO thread), guarded by [lock]. */
    private val completed by lazy { store.loadCompleted(System.currentTimeMillis()) }
    private val wake = Channel<Unit>(Channel.CONFLATED)
    @Volatile private var link: Link? = null
    private var busy = 0

    init {
        scope.launch { store.prune(System.currentTimeMillis(), PARTIAL_MAX_AGE_MS) }
        scope.launch { sendLoop() }
    }

    // -- link lifecycle (called by the control connection) ---------------------------------------

    fun onLinkUp(peer: Peer, pairing: Pairing, endpoint: Endpoint) {
        val l = Link(peer, pairing, endpoint, scope.coroutineContext)
        link = l
        repeat(TransferCore.FILES_IN_FLIGHT) {
            l.scope.launch { worker(l, l.sends) }
            l.scope.launch { worker(l, l.recvs) }
        }
        wake.trySend(Unit)
    }

    /** Control connection gone: data connections close, received ranges are checkpointed, sends wait. */
    fun onLinkDown() {
        val l = link ?: return
        link = null
        l.sends.close()
        l.recvs.close()
        l.scope.cancel() // receive jobs checkpoint as they unwind
        synchronized(lock) { incoming.clear() }
    }

    // -- sender side -----------------------------------------------------------------------------

    fun sendFile(peer: Peer, feature: String, source: FileSource, meta: JSONObject, onProgress: (Long) -> Unit, onDone: (Boolean) -> Unit) {
        val offer = FileOffer(TransferCore.newId(), feature, TransferCore.safeName(source.name), source.size, meta)
        synchronized(lock) { outbox.addLast(Outgoing(peer, offer, source, onProgress, onDone)) }
        wake.trySend(Unit)
    }

    private suspend fun sendLoop() {
        while (scope.isActive) {
            val l = link
            val out = synchronized(lock) {
                if (l == null || outbox.count { it.sending } >= TransferCore.FILES_IN_FLIGHT) null
                else outbox.firstOrNull { !it.sending }?.also { it.sending = true }
            }
            if (out == null || l == null) { wake.receive(); continue }
            scope.launch {
                try { sendOne(l, out) } finally {
                    synchronized(lock) { out.sending = false }
                    wake.trySend(Unit) // a slot is free, or the link was lost and the file waits for the next one
                }
            }
        }
    }

    /** Offer [out] until it is finished; returns early if the link goes away (it is re-offered with the same id on the next one). */
    private suspend fun sendOne(l: Link, out: Outgoing) {
        while (true) {
            val attempt = l.scope.async { offerOnce(l, out) }
            val outcome = try { attempt.await() } catch (e: CancellationException) { if (!scope.isActive) throw e; null }
            when (outcome) {
                null -> return l.job.join() // wait out the old link's workers (and a failed control send) before this file is re-offered
                Outcome.CORRUPT -> if (out.retried) return finish(out, false) else {
                    out.retried = true
                    out.offer = out.offer.copy(id = TransferCore.newId())
                }
                else -> return finish(out, outcome == Outcome.DELIVERED)
            }
        }
    }

    private fun finish(out: Outgoing, ok: Boolean) {
        synchronized(lock) { outbox.remove(out) }
        log("send ${out.offer.id.take(8)} ${out.offer.size} B: ${if (ok) "delivered" else "failed"}")
        out.onDone(ok)
    }

    /** @return the outcome, or null if the link went away first. */
    private suspend fun offerOnce(l: Link, out: Outgoing): Outcome? {
        out.reply = CompletableDeferred(); out.done = CompletableDeferred()
        out.activeMs = nowMs()
        if (!control(TransferCore.offerHeader(out.offer))) return null
        val reply = awaitActive(out, out.reply) ?: return stalled(out)
        if (reply.optString("type") != "xfer_accept") {
            return if (reply.optString("type") == "xfer_reject") Outcome.REJECTED else Outcome.CANCELLED
        }
        val ranges = TransferCore.parseRanges(reply.opt("ranges"), out.offer.size)
        if (ranges == null) { control(TransferCore.cancel(out.offer.id)); return Outcome.CANCELLED }
        out.onProgress(ranges.sumOf { it.have })
        // A range the receiver already has in full needs no data connection (PROTOCOL.md 6.2).
        ranges.filter { !it.complete }.forEach { l.sends.send(RangeJob.Send(out, it)) }
        return awaitActive(out, out.done) ?: stalled(out)
    }

    /** [d]'s value, or null once nothing has moved on [out] for [STALL_MS]. */
    private suspend fun <T : Any> awaitActive(out: Outgoing, d: CompletableDeferred<T>): T? {
        while (true) {
            @OptIn(kotlinx.coroutines.ExperimentalCoroutinesApi::class)
            if (d.isCompleted) return d.getCompleted() // arrived just as the timer ran out
            val left = STALL_MS - (nowMs() - out.activeMs)
            if (left <= 0) return null
            val v = withTimeoutOrNull(left) { d.await() } ?: continue // range progress may have moved activeMs
            out.activeMs = nowMs()
            return v
        }
    }

    /** The receiver went quiet: give the file up so the outbox moves on. */
    private fun stalled(out: Outgoing): Outcome {
        log("send ${out.offer.id.take(8)} stalled; cancelling")
        control(TransferCore.cancel(out.offer.id))
        out.done.complete(Outcome.CANCELLED) // a range waiting on the socket sees this and closes its connection
        return Outcome.CANCELLED
    }

    // -- control messages from the peer ----------------------------------------------------------

    /** A `ch = "link"` message; ignores types that are not file transfer. */
    fun onControl(peer: Peer, h: JSONObject) {
        val type = h.optString("type")
        if (type == "xfer_offer") {
            val offer = TransferCore.parseOffer(h) ?: return log("drop malformed xfer_offer")
            val l = link ?: return
            l.scope.launch { onOffer(l, peer, offer) }
            return
        }
        val id = h.optString("id")
        val out = synchronized(lock) { outbox.firstOrNull { it.sending && it.offer.id == id } }
        when (type) {
            "xfer_accept", "xfer_reject" -> out?.reply?.complete(h)
            "xfer_done" -> out?.done?.complete(if (h.optBoolean("ok")) Outcome.DELIVERED else Outcome.CORRUPT)
            "xfer_cancel" -> {
                out?.reply?.complete(h); out?.done?.complete(Outcome.CANCELLED)
                synchronized(lock) { incoming.remove(id) }?.let { fail(it, notifyPeer = null) }
            }
        }
    }

    // -- receiver side ---------------------------------------------------------------------------

    private suspend fun onOffer(l: Link, peer: Peer, offer: FileOffer) {
        if (synchronized(lock) { completed.has(offer.id, peer.id, offer.size, System.currentTimeMillis()) }) {
            // Already received; our xfer_done was lost with the connection. Confirm, do not receive again.
            log("recv ${offer.id.take(8)} already complete; confirming")
            control(TransferCore.accept(offer.id, TransferCore.fullRanges(offer.size)))
            control(TransferCore.done(offer.id, true))
            return
        }
        synchronized(lock) { incoming.remove(offer.id) }?.over = true // repeated offer: start over from saved state
        val feature = router.fileFeature(offer.feature)
        val saved = store.load(offer.id)
        val sink = feature?.let { f ->
            try { f.onFileOffer(peer, offer, saved?.token) } catch (e: Exception) { log("file offer failed: ${e.javaClass.simpleName}"); null }
        }
        if (feature == null || sink == null) { control(TransferCore.reject(offer.id)); return }
        val inc = Incoming(peer, offer, feature, sink, TransferCore.resumeRanges(offer, saved, sink.token))
        store.save(inc.state())
        synchronized(lock) { incoming[offer.id] = inc }
        if (!control(TransferCore.accept(offer.id, inc.ranges))) return
        log("recv ${offer.id.take(8)} ${offer.size} B, resuming at ${inc.received()} B")
        feature.onFileProgress(peer, offer, inc.received())
        if (inc.ranges.isEmpty()) complete(inc)
        inc.ranges.forEach { l.recvs.send(RangeJob.Recv(inc, it)) }
    }

    /** Every range ended with a matching hash: commit, report, forget. */
    private fun complete(inc: Incoming) {
        if (inc.over) return
        inc.over = true
        synchronized(lock) { incoming.remove(inc.offer.id) }
        val ok = try { inc.sink.commit(); true } catch (e: Exception) { log("commit failed: ${e.javaClass.simpleName}"); false }
        if (ok) remember(inc)
        store.delete(inc.offer.id)
        control(TransferCore.done(inc.offer.id, ok))
        if (ok) inc.feature.onFileReceived(inc.peer, inc.offer, inc.sink)
        else { runCatching { inc.sink.discard() }; inc.feature.onFileFailed(inc.peer, inc.offer) }
    }

    /** Record a completed id (PROTOCOL.md 6.2) so a re-offer after a lost `xfer_done` is confirmed, not received again. */
    private fun remember(inc: Incoming) {
        synchronized(lock) {
            completed.add(inc.offer.id, inc.peer.id, inc.offer.size, System.currentTimeMillis())
            try { store.saveCompleted(completed) } catch (e: IOException) { log("completed record not saved: ${e.javaClass.simpleName}") }
        }
    }

    /** Drop [inc] and its partial file; [notifyPeer] is the message to send (`xfer_done ok:false`, `xfer_cancel`) or null. */
    private fun fail(inc: Incoming, notifyPeer: JSONObject?) {
        inc.over = true
        synchronized(lock) { incoming.remove(inc.offer.id) }
        runCatching { inc.sink.discard() }
        store.delete(inc.offer.id)
        notifyPeer?.let { control(it) }
        inc.feature.onFileFailed(inc.peer, inc.offer)
    }

    private fun checkpoint(inc: Incoming) {
        if (inc.over) return
        synchronized(inc) {
            try { inc.sink.sync(); store.save(inc.state()) } catch (e: IOException) { log("checkpoint failed: ${e.javaClass.simpleName}") }
        }
    }

    // -- data connections ------------------------------------------------------------------------

    private enum class Result { OK, ABORTED, BROKEN }

    private suspend fun worker(l: Link, jobs: Channel<RangeJob>) {
        var conn: DataConn? = null
        try {
            while (true) {
                val next = if (conn == null) jobs.receiveCatching() else withTimeoutOrNull(IDLE_MS) { jobs.receiveCatching() }
                if (next == null) { conn?.close(1000); conn = null; continue } // idle: close the data connection
                val job = next.getOrNull() ?: return
                if (aborted(job)) continue
                if (conn?.isOpen != true) {
                    conn?.close(1000)
                    val fresh = DataConn(l)
                    conn = fresh
                    if (!fresh.open()) { conn = null; abandon(job); dropLink(); return }
                }
                val c: DataConn = conn
                setBusy(true)
                val result = try { if (job is RangeJob.Send) sendRange(c, job) else recvRange(c, job as RangeJob.Recv) } finally { setBusy(false) }
                when (result) {
                    Result.OK -> {}
                    Result.ABORTED -> { c.close(1000); conn = null }
                    Result.BROKEN -> { c.close(4400); conn = null; dropLink(); return }
                }
            }
        } finally {
            conn?.close(1000)
        }
    }

    private fun aborted(job: RangeJob): Boolean = when (job) {
        is RangeJob.Send -> job.out.done.isCompleted
        is RangeJob.Recv -> job.inc.over
    }

    private fun abandon(job: RangeJob) { if (job is RangeJob.Recv) checkpoint(job.inc) }

    private fun setBusy(on: Boolean) {
        val edge = synchronized(lock) { if (on) busy++ == 0 else --busy == 0 }
        if (edge) onBusy(on)
    }

    private suspend fun sendRange(c: DataConn, job: RangeJob.Send): Result {
        val out = job.out
        val r = job.range
        val sha = MessageDigest.getInstance("SHA-256")
        val started = System.nanoTime()
        try {
            out.source.open().use { input ->
                skipFully(input, r.start)
                hashPrefix(input, r.have, sha) { out.activeMs = nowMs() } // a long resume prefix is progress too
                if (!c.send(TransferCore.attach(out.offer.id, r.start))) return closedBy(out)
                var sent = 0L
                val base = out.offer.size - r.length + r.have
                coroutineScope {
                    val chunks = readAhead(input, r)
                    for (body in chunks) {
                        if (out.done.isCompleted) { chunks.cancel(); return@coroutineScope }
                        sha.update(body)
                        while (c.isOpen && !out.done.isCompleted && c.queued() > 2L * TransferCore.CHUNK) delay(5)
                        if (out.done.isCompleted) { chunks.cancel(); return@coroutineScope }
                        if (!c.send(TransferCore.data(), body)) { chunks.cancel(); return@coroutineScope }
                        sent += body.size
                        out.activeMs = nowMs()
                        out.onProgress(base + sent)
                    }
                }
                if (out.done.isCompleted) return Result.ABORTED
                if (!c.send(TransferCore.end(TransferCore.hex(sha.digest())))) return closedBy(out)
                logRate("sent", sent, started)
                return Result.OK
            }
        } catch (e: IOException) {
            // Source unreadable or shorter than offered: this file cannot be sent.
            log("send source failed: ${e.javaClass.simpleName}")
            control(TransferCore.cancel(out.offer.id))
            out.done.complete(Outcome.CANCELLED)
            return Result.ABORTED
        }
    }

    /**
     * The data connection closed under a send. A receiver that cannot go on sends `xfer_cancel` before
     * closing (1000), so wait briefly for it: then the transfer is over, not the link.
     */
    private suspend fun closedBy(out: Outgoing): Result =
        if (withTimeoutOrNull(CANCEL_GRACE_MS) { out.done.await() } != null) Result.ABORTED else Result.BROKEN

    /** Disk reads run ahead of the socket by two chunks. */
    private fun CoroutineScope.readAhead(input: InputStream, r: Range): ReceiveChannel<ByteArray> = produce(Dispatchers.IO, capacity = 2) {
        for ((_, len) in TransferCore.chunks(r)) send(readFully(input, len))
    }

    private suspend fun recvRange(c: DataConn, job: RangeJob.Recv): Result {
        val inc = job.inc
        val r = job.range
        val stream = RangeStream(r)
        val started = System.nanoTime()
        val from = stream.position
        try {
            primeFromDisk(inc.sink, r, stream)
            if (!c.send(TransferCore.attach(inc.offer.id, r.start))) return Result.BROKEN
            var sinceSync = 0L
            while (true) {
                val env = c.incoming.receiveCatching().getOrNull()
                // A sender that cannot go on sends xfer_cancel before closing (1000): give it a moment to arrive.
                if (env == null) withTimeoutOrNull(CANCEL_GRACE_MS) { while (!inc.over) delay(20) }
                if (env == null || inc.over) { checkpoint(inc); return if (inc.over) Result.ABORTED else Result.BROKEN }
                when (TransferCore.classify(env.header)) {
                    "data" -> {
                        stream.check(env.body.size)
                        inc.sink.write(stream.position, env.body)
                        stream.feed(env.body)
                        sinceSync += env.body.size
                        if (sinceSync >= CHECKPOINT_BYTES) { checkpoint(inc); sinceSync = 0 }
                        progress(inc)
                    }
                    "end" -> {
                        val match = stream.finish(env.header)
                        logRate("received", stream.position - from, started)
                        if (!match) { log("range hash mismatch"); fail(inc, TransferCore.done(inc.offer.id, false)); return Result.OK }
                        checkpoint(inc)
                        val all = synchronized(inc) { inc.ended += r.start; inc.ended.size == inc.ranges.size }
                        if (all) complete(inc)
                        return Result.OK
                    }
                }
            }
        } catch (e: TransferViolation) {
            log("data violation: ${e.message}")
            checkpoint(inc)
            return Result.BROKEN
        } catch (e: IOException) {
            // Our storage failed (full, file gone): the transfer cannot finish.
            log("receive write failed: ${e.javaClass.simpleName}")
            fail(inc, TransferCore.cancel(inc.offer.id))
            return Result.ABORTED
        } catch (e: CancellationException) {
            checkpoint(inc)
            throw e
        }
    }

    private fun progress(inc: Incoming) {
        val now = System.currentTimeMillis()
        if (now - inc.lastProgressMs < PROGRESS_MS) return
        inc.lastProgressMs = now
        inc.feature.onFileProgress(inc.peer, inc.offer, inc.received())
    }

    private fun logRate(what: String, bytes: Long, startedNs: Long) {
        val s = (System.nanoTime() - startedNs) / 1e9
        if (s > 0) log("$what $bytes B in ${"%.1f".format(s)} s (${"%.1f".format(bytes / s / 1e6)} MB/s)")
    }

    /** The `have` prefix is hashed from local storage (both sides do this). */
    private fun primeFromDisk(sink: FileSink, r: Range, stream: RangeStream) {
        val buf = ByteArray(minOf(TransferCore.CHUNK.toLong(), maxOf(r.have, 1)).toInt())
        var pos = r.start
        val stop = r.start + r.have
        while (pos < stop) {
            val n = sink.read(pos, buf, minOf(buf.size.toLong(), stop - pos).toInt())
            if (n <= 0) throw EOFException("partial file shorter than saved state")
            stream.prime(buf, n)
            pos += n
        }
    }

    private fun hashPrefix(input: InputStream, len: Long, sha: MessageDigest, onBlock: () -> Unit) {
        val buf = ByteArray(minOf(TransferCore.CHUNK.toLong(), maxOf(len, 1)).toInt())
        var left = len
        while (left > 0) {
            val n = input.read(buf, 0, minOf(buf.size.toLong(), left).toInt())
            if (n < 0) throw EOFException()
            sha.update(buf, 0, n)
            left -= n
            onBlock()
        }
        onBlock()
    }

    private fun skipFully(input: InputStream, n: Long) {
        var left = n
        while (left > 0) {
            val s = input.skip(left)
            if (s > 0) left -= s else if (input.read() < 0) throw EOFException() else left--
        }
    }

    private fun readFully(input: InputStream, len: Int): ByteArray {
        val b = ByteArray(len)
        var off = 0
        while (off < len) {
            val n = input.read(b, off, len - off)
            if (n < 0) throw EOFException()
            off += n
        }
        return b
    }

    /** One data connection (subprotocol `clipbridge.data.v1`). Incoming envelopes block OkHttp's reader (TCP backpressure). */
    private inner class DataConn(l: Link) {
        val incoming = Channel<Envelope>(2)
        private val ready = CompletableDeferred<Boolean>()
        private val sock = SecureSocket(
            scope = l.scope, pairing = l.pairing, endpoint = l.endpoint, subprotocol = DATA_SUBPROTOCOL,
            onReady = { ready.complete(true) },
            onPlaintext = { s, p ->
                val env = try { Envelopes.parse(p) } catch (e: EnvelopeException) { null }
                if (env == null) s.close(4400)
                else runBlocking { try { incoming.send(env) } catch (e: Exception) { /* consumer gone */ } }
            },
            log = log,
        )
        private var opened = false

        val isOpen: Boolean get() = sock.isReady

        suspend fun open(): Boolean {
            if (opened) return sock.isReady
            opened = true
            sock.open(http)
            scope.launch { sock.closed.await(); ready.complete(false); incoming.close() }
            val ok = withTimeoutOrNull(OPEN_TIMEOUT_MS) { ready.await() } ?: false
            if (!ok) sock.cancel()
            return ok
        }

        fun send(header: JSONObject, body: ByteArray = ByteArray(0)): Boolean = sock.send(Envelopes.encode(header, body))

        fun queued(): Long = sock.queueSize()

        fun close(code: Int) { sock.close(code); incoming.cancel() }
    }

    companion object {
        const val DATA_SUBPROTOCOL = "clipbridge.data.v1"
        /** A data connection with no work closes after this. */
        private const val IDLE_MS = 2_000L
        private const val OPEN_TIMEOUT_MS = 15_000L
        /** Received bytes are made durable and recorded as resumable at least this often. */
        private const val CHECKPOINT_BYTES = 32L * 1024 * 1024
        private const val PROGRESS_MS = 500L
        /** Matches MediaStore's expiry of pending (unfinished) downloads. */
        private const val PARTIAL_MAX_AGE_MS = 7L * 24 * 3600 * 1000
        /** A sender gives up on an offer that sees no reply and no range progress for this long (PROTOCOL.md 6.2). */
        private const val STALL_MS = 60_000L
        /** How long a closed data connection waits for the peer's `xfer_cancel` before counting as a broken link. */
        private const val CANCEL_GRACE_MS = 2_000L

        private fun nowMs(): Long = System.nanoTime() / 1_000_000
    }
}

