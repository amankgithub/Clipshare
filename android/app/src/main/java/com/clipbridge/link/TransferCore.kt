package com.clipbridge.link

import org.json.JSONArray
import org.json.JSONObject
import java.security.MessageDigest

/**
 * File transfer (PROTOCOL.md section 6.2): pure logic, no I/O.
 * Message builders/parsers, range split and resume bookkeeping, per-range streaming checks and
 * SHA-256, file-name sanitising, and the receiver's saved range state. Sockets and disk live in [Transfer].
 */

/** Data connection broke the stream rules (close 4400). */
class TransferViolation(message: String) : Exception(message)

/** A file one side wants to give the other. [name] is already a safe base name. */
data class FileOffer(val id: String, val feature: String, val name: String, val size: Long, val meta: JSONObject)

/** Bytes [start, end) of a file, of which the first [have] are already on the receiver's disk. */
data class Range(val start: Long, val end: Long, var have: Long = 0) {
    val length: Long get() = end - start
    val complete: Boolean get() = have == length
}

/** Receiver's resume state for one unfinished file, kept in app-private storage. */
data class PartialState(val offer: FileOffer, val token: String, val ranges: List<Range>, val updatedMs: Long)

object TransferCore {
    /** Data connections at once; raised only if the benchmark shows a gain. */
    const val STREAMS = 1
    /** Max `xfer_data` body. */
    const val CHUNK = 4 * 1024 * 1024

    private val ID = Regex("[0-9a-f]{32}")
    private val HEX64 = Regex("[0-9a-f]{64}")
    private val FORBIDDEN = Regex("[<>:\"/\\\\|?*\\x00-\\x1f]")
    private val RESERVED = setOf("CON", "PRN", "AUX", "NUL") +
        (1..9).flatMap { listOf("COM$it", "LPT$it") }

    fun validId(v: Any?): Boolean = v is String && ID.matches(v)

    fun newId(): String {
        val b = ByteArray(16).also { java.security.SecureRandom().nextBytes(it) }
        return hex(b)
    }

    fun hex(b: ByteArray): String = b.joinToString("") { "%02x".format(it) }

    /** Base name safe to create on any file system; never empty, never a path. */
    fun safeName(name: String): String {
        var base = name.split('/', '\\').last()
        base = FORBIDDEN.replace(base, "_").trimEnd(' ', '.').trimStart(' ')
        if (base.isEmpty() || base == "." || base == "..") base = "file"
        if (base.substringBefore('.').uppercase() in RESERVED) base = "_$base"
        if (base.length > 255) {
            val dot = base.lastIndexOf('.')
            val ext = if (dot > 0) base.substring(dot + 1) else ""
            base = if (dot > 0 && ext.length < 32) base.substring(0, 255 - ext.length - 1) + "." + ext else base.substring(0, 255)
        }
        return base
    }

    // -- integers from JSON (reject bools, floats, strings) ------------------------------------

    private fun long(v: Any?): Long? = when (v) {
        is Int -> v.toLong()
        is Long -> v
        else -> null
    }

    // -- offer -----------------------------------------------------------------------------------

    fun offerHeader(o: FileOffer): JSONObject = link("xfer_offer").put("id", o.id).put("feature", o.feature)
        .put("name", o.name).put("size", o.size).put("meta", o.meta)

    /** Validated offer, or null if any field is missing or ill-typed. */
    fun parseOffer(h: JSONObject): FileOffer? {
        val id = h.opt("id"); val feature = h.opt("feature"); val name = h.opt("name")
        val size = long(h.opt("size")); val meta = h.opt("meta")
        if (!validId(id) || feature !is String || feature.isEmpty()) return null
        if (name !is String || name.length !in 1..255) return null
        if (size == null || size < 0 || meta !is JSONObject) return null
        return FileOffer(id as String, feature, safeName(name), size, meta)
    }

    // -- ranges ----------------------------------------------------------------------------------

    /** Split [0, size) into up to [parts] contiguous ranges (none for an empty file). */
    fun split(size: Long, parts: Int = STREAMS): List<Range> {
        if (size == 0L) return emptyList()
        val n = parts.toLong().coerceIn(1, size)
        val step = size / n
        val extra = size % n
        var start = 0L
        return (0 until n).map { i ->
            val end = start + step + if (i < extra) 1 else 0
            Range(start, end).also { start = end }
        }
    }

    /** Validate an `xfer_accept` ranges list: sorted, non-overlapping, covering [0, size). */
    fun parseRanges(raw: Any?, size: Long): List<Range>? {
        if (raw !is JSONArray) return null
        val out = ArrayList<Range>()
        var pos = 0L
        for (i in 0 until raw.length()) {
            val item = raw.opt(i) as? JSONArray ?: return null
            if (item.length() != 3) return null
            val start = long(item.opt(0)) ?: return null
            val end = long(item.opt(1)) ?: return null
            val have = long(item.opt(2)) ?: return null
            if (start != pos || end <= start || have < 0 || have > end - start) return null
            out += Range(start, end, have)
            pos = end
        }
        return if (pos == size) out else null
    }

    private fun rangesJson(ranges: List<Range>): JSONArray =
        JSONArray().also { a -> ranges.forEach { a.put(JSONArray().put(it.start).put(it.end).put(it.have)) } }

    /**
     * Ranges to accept an offer with: [saved] (with its `have` values) when it still describes the
     * same file, else a fresh split.
     */
    fun resumeRanges(offer: FileOffer, saved: PartialState?, token: String): List<Range> =
        if (saved != null && saved.token == token && saved.offer.size == offer.size && saved.offer.feature == offer.feature &&
            parseRanges(rangesJson(saved.ranges), offer.size) != null
        ) saved.ranges.map { it.copy() } else split(offer.size)

    // -- control and data messages ---------------------------------------------------------------

    private fun link(type: String): JSONObject = JSONObject().put("ch", Receipts.LINK_CH).put("type", type)

    fun accept(id: String, ranges: List<Range>): JSONObject = link("xfer_accept").put("id", id).put("ranges", rangesJson(ranges))
    fun reject(id: String): JSONObject = link("xfer_reject").put("id", id)
    fun done(id: String, ok: Boolean): JSONObject = link("xfer_done").put("id", id).put("ok", ok)
    fun cancel(id: String): JSONObject = link("xfer_cancel").put("id", id)
    fun attach(id: String, start: Long): JSONObject = link("xfer_attach").put("id", id).put("start", start)
    fun data(): JSONObject = link("xfer_data")
    fun end(sha256Hex: String): JSONObject = link("xfer_end").put("sha256", sha256Hex)

    /** Frame type received on a data connection mid-range: "data", "end", or throws. */
    fun classify(h: JSONObject): String {
        if (h.optString("ch") == Receipts.LINK_CH) when (h.optString("type")) {
            "xfer_data" -> return "data"
            "xfer_end" -> return "end"
        }
        throw TransferViolation("unexpected frame ${h.opt("type")}")
    }

    /** Absolute offsets of the `xfer_data` bodies a sender streams for [r]: (position, length) pairs. */
    fun chunks(r: Range, chunk: Int = CHUNK): Sequence<Pair<Long, Int>> = sequence {
        var pos = r.start + r.have
        while (pos < r.end) {
            val n = minOf(chunk.toLong(), r.end - pos).toInt()
            yield(pos to n)
            pos += n
        }
    }

    // -- saved state -----------------------------------------------------------------------------

    fun stateJson(s: PartialState): String = JSONObject()
        .put("offer", offerHeader(s.offer)).put("token", s.token)
        .put("ranges", rangesJson(s.ranges)).put("updated", s.updatedMs).toString()

    fun parseState(text: String): PartialState? = try {
        val o = JSONObject(text)
        val offer = parseOffer(o.getJSONObject("offer"))
        val ranges = offer?.let { parseRanges(o.opt("ranges"), it.size) }
        val token = o.opt("token") as? String
        if (offer == null || ranges == null || token == null) null
        else PartialState(offer, token, ranges, o.optLong("updated"))
    } catch (e: org.json.JSONException) { null }

    fun isValidSha(s: Any?): Boolean = s is String && HEX64.matches(s)
}

/**
 * Receiver-side checks for one range: positions, chunk limits, and the running SHA-256.
 * Feed it the `have` prefix read back from disk ([prime]), then each `xfer_data` body ([feed]),
 * then the `xfer_end` header ([finish]). The caller writes bytes at [position] before feeding.
 */
class RangeStream(val range: Range) {
    /** Absolute offset of the next byte to arrive. */
    var position: Long = range.start + range.have
        private set
    private val sha = MessageDigest.getInstance("SHA-256")

    fun prime(prefix: ByteArray, len: Int = prefix.size) { sha.update(prefix, 0, len) }

    /** Throws if this body may not be written at [position]. */
    fun check(len: Int) {
        if (len < 1 || len > TransferCore.CHUNK) throw TransferViolation("bad chunk size")
        if (position + len > range.end) throw TransferViolation("bytes past end of range")
    }

    fun feed(body: ByteArray) {
        check(body.size)
        sha.update(body)
        position += body.size
        range.have = position - range.start
    }

    /** @return whether the range hash matches. Throws if the end arrived early or is malformed. */
    fun finish(h: JSONObject): Boolean {
        if (position != range.end) throw TransferViolation("xfer_end before end of range")
        val digest = h.opt("sha256")
        if (!TransferCore.isValidSha(digest)) throw TransferViolation("bad sha256")
        return digest == TransferCore.hex(sha.digest())
    }
}
