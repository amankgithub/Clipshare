package com.clipbridge

import com.clipbridge.link.FileOffer
import com.clipbridge.link.PartialState
import com.clipbridge.link.Range
import com.clipbridge.link.RangeStream
import com.clipbridge.link.TransferCore
import com.clipbridge.link.TransferViolation
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.security.MessageDigest

class TransferCoreTest {
    private val id = "0123456789abcdef0123456789abcdef"
    private fun offer(size: Long) = FileOffer(id, "files", "a.bin", size, JSONObject())
    private fun sha(b: ByteArray) = TransferCore.hex(MessageDigest.getInstance("SHA-256").digest(b))

    @Test fun splitCoversSizeContiguously() {
        assertEquals(emptyList<Range>(), TransferCore.split(0))
        assertEquals(listOf(Range(0, 10)), TransferCore.split(10, 1))
        assertEquals(listOf(Range(0, 4), Range(4, 7), Range(7, 10)), TransferCore.split(10, 3))
        assertEquals(listOf(Range(0, 1), Range(1, 2)), TransferCore.split(2, 5))
        val big = 5L * 1024 * 1024 * 1024 + 3
        val parts = TransferCore.split(big, 3)
        assertEquals(0L, parts.first().start)
        assertEquals(big, parts.last().end)
        parts.zipWithNext().forEach { (a, b) -> assertEquals(a.end, b.start) }
    }

    @Test fun parseRangesValidatesCoverage() {
        fun r(vararg xs: List<Long>) = JSONArray().also { a -> xs.forEach { a.put(JSONArray(it)) } }
        assertEquals(listOf(Range(0, 5, 2), Range(5, 9, 0)), TransferCore.parseRanges(r(listOf(0, 5, 2), listOf(5, 9, 0)), 9))
        assertEquals(emptyList<Range>(), TransferCore.parseRanges(JSONArray(), 0))
        assertNull(TransferCore.parseRanges(JSONArray(), 1))                            // gap at end
        assertNull(TransferCore.parseRanges(r(listOf(0, 5, 0)), 9))                     // does not cover
        assertNull(TransferCore.parseRanges(r(listOf(1, 9, 0)), 9))                     // gap at start
        assertNull(TransferCore.parseRanges(r(listOf(0, 5, 0), listOf(4, 9, 0)), 9))    // overlap
        assertNull(TransferCore.parseRanges(r(listOf(0, 5, 6)), 5))                     // have > length
        assertNull(TransferCore.parseRanges(r(listOf(0, 0, 0)), 0))                     // empty range
        assertNull(TransferCore.parseRanges(JSONArray("[[0,5,true]]"), 5))
        assertNull(TransferCore.parseRanges(JSONArray("[[0,5,1.5]]"), 5))
        assertNull(TransferCore.parseRanges("[[0,5,0]]", 5))
    }

    @Test fun offerRoundTripAndValidation() {
        val o = FileOffer(id, "files", "x.pdf", 1L shl 33, JSONObject().put("k", 1))
        val parsed = TransferCore.parseOffer(JSONObject(TransferCore.offerHeader(o).toString()))!!
        assertEquals(o.id, parsed.id); assertEquals(o.size, parsed.size); assertEquals(1, parsed.meta.getInt("k"))
        val base = TransferCore.offerHeader(o)
        assertNull(TransferCore.parseOffer(JSONObject(base.toString()).put("id", "ABC")))
        assertNull(TransferCore.parseOffer(JSONObject(base.toString()).put("size", -1)))
        assertNull(TransferCore.parseOffer(JSONObject(base.toString()).put("size", "5")))
        assertNull(TransferCore.parseOffer(JSONObject(base.toString()).put("name", "")))
        assertNull(TransferCore.parseOffer(JSONObject(base.toString()).put("meta", "x")))
        assertEquals("evil.txt", TransferCore.parseOffer(JSONObject(base.toString()).put("name", "../../evil.txt"))!!.name)
    }

    @Test fun safeNameStripsPathsAndForbiddenChars() {
        assertEquals("b.txt", TransferCore.safeName("C:\\a\\b.txt"))
        assertEquals("a_b_.txt", TransferCore.safeName("a:b?.txt"))
        assertEquals("file", TransferCore.safeName(".."))
        assertEquals("file", TransferCore.safeName("dir/"))
        assertEquals("_CON.txt", TransferCore.safeName("CON.txt"))
        val long = TransferCore.safeName("a".repeat(300) + ".mp4")
        assertEquals(255, long.length); assertTrue(long.endsWith(".mp4"))
    }

    @Test fun resumeUsesSavedHaveOnlyForSameFile() {
        val saved = PartialState(offer(10), "tok", listOf(Range(0, 10, 6)), 0)
        assertEquals(listOf(Range(0, 10, 6)), TransferCore.resumeRanges(offer(10), saved, "tok"))
        assertEquals(listOf(Range(0, 10, 0)), TransferCore.resumeRanges(offer(10), saved, "other"))  // destination gone
        assertEquals(listOf(Range(0, 11, 0)), TransferCore.resumeRanges(offer(11), saved, "tok"))   // size changed
        assertEquals(listOf(Range(0, 10, 0)), TransferCore.resumeRanges(offer(10), null, "tok"))
        // the returned ranges are copies: streaming must not mutate the saved state object
        TransferCore.resumeRanges(offer(10), saved, "tok")[0].have = 9
        assertEquals(6L, saved.ranges[0].have)
    }

    @Test fun stateRoundTrip() {
        val s = PartialState(offer(1L shl 40), "content://x/1", listOf(Range(0, 1L shl 40, 12345)), 99)
        val back = TransferCore.parseState(TransferCore.stateJson(s))!!
        assertEquals(s.token, back.token); assertEquals(s.ranges, back.ranges); assertEquals(99L, back.updatedMs)
        assertNull(TransferCore.parseState("not json"))
        assertNull(TransferCore.parseState("{}"))
    }

    @Test fun chunksStartAfterHave() {
        assertEquals(listOf(3L to 4, 7L to 3), TransferCore.chunks(Range(0, 10, 3), chunk = 4).toList())
        assertEquals(emptyList<Pair<Long, Int>>(), TransferCore.chunks(Range(0, 10, 10)).toList())
        assertEquals(listOf(5L to 5), TransferCore.chunks(Range(5, 10, 0)).toList())
    }

    @Test fun rangeStreamResumesWithPrefixHash() {
        val file = ByteArray(100) { it.toByte() }
        val r = Range(0, 100, 40)
        val s = RangeStream(r)
        assertEquals(40L, s.position)
        s.prime(file.copyOfRange(0, 40))
        s.feed(file.copyOfRange(40, 90))
        assertEquals(90L, r.have)
        s.feed(file.copyOfRange(90, 100))
        assertTrue(r.complete)
        assertTrue(s.finish(TransferCore.end(sha(file))))
    }

    @Test fun rangeStreamDetectsMismatch() {
        val s = RangeStream(Range(10, 20))
        s.feed(ByteArray(10))
        assertFalse(s.finish(TransferCore.end(sha(ByteArray(10) { 1 }))))
    }

    @Test fun rangeStreamRejectsViolations() {
        fun throws(block: () -> Unit) = try { block(); false } catch (e: TransferViolation) { true }
        assertTrue(throws { RangeStream(Range(0, 4)).feed(ByteArray(5)) })                 // past end
        assertTrue(throws { RangeStream(Range(0, 4)).feed(ByteArray(0)) })                 // empty body
        assertTrue(throws { RangeStream(Range(0, 4)).finish(TransferCore.end("0".repeat(64))) }) // early end
        assertTrue(throws { RangeStream(Range(0, 0 + 1, 1)).finish(JSONObject().put("sha256", "XYZ")) })
        assertTrue(throws { TransferCore.classify(JSONObject().put("ch", "link").put("type", "xfer_attach")) })
        assertTrue(throws { TransferCore.classify(JSONObject().put("ch", "files").put("type", "xfer_data")) })
        assertEquals("data", TransferCore.classify(TransferCore.data()))
        assertEquals("end", TransferCore.classify(TransferCore.end("0".repeat(64))))
    }

    @Test fun idsAreLowercaseHex() {
        val a = TransferCore.newId()
        assertTrue(TransferCore.validId(a)); assertNotNull(a)
        assertFalse(TransferCore.validId(a.uppercase()))
    }
}
