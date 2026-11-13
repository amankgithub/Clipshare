package com.clipbridge

import com.clipbridge.link.CompletedIds
import com.clipbridge.link.FileOffer
import com.clipbridge.link.PartialState
import com.clipbridge.link.Range
import com.clipbridge.link.TransferCore
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class CompletedIdsTest {
    private val id = "0123456789abcdef0123456789abcdef"

    @Test fun remembersIdForSamePeerAndSize() {
        val c = CompletedIds()
        c.add(id, "pc", 10, 0)
        assertTrue(c.has(id, "pc", 10, 1))
        assertFalse(c.has(id, "other", 10, 1))
        assertFalse(c.has(id, "pc", 11, 1))
        assertFalse(c.has("f".repeat(32), "pc", 10, 1))
    }

    @Test fun forgetsAfterMaxAge() {
        val c = CompletedIds(maxAgeMs = 100)
        c.add(id, "pc", 10, 0)
        assertTrue(c.has(id, "pc", 10, 100))
        assertFalse(c.has(id, "pc", 10, 101))
        assertEquals(0, c.size)
    }

    @Test fun keepsOnlyNewestMaxItems() {
        val c = CompletedIds(maxItems = 2)
        val ids = (1..3).map { "%032x".format(it) }
        ids.forEachIndexed { i, x -> c.add(x, "pc", 1, i.toLong()) }
        assertEquals(2, c.size)
        assertFalse(c.has(ids[0], "pc", 1, 3))
        assertTrue(c.has(ids[2], "pc", 1, 3))
    }

    @Test fun survivesSaveAndReload() {
        val c = CompletedIds()
        c.add(id, "pc", 10, 1_000)
        val back = CompletedIds.parse(c.toJson(), 2_000)
        assertTrue(back.has(id, "pc", 10, 2_000))
        assertEquals(0, CompletedIds.parse(c.toJson(), 1_000 + 25L * 3600 * 1000).size) // expired on load
        assertEquals(0, CompletedIds.parse("not json", 0).size)
        assertEquals(0, CompletedIds.parse("[[\"bad\"],[1,2,3,4]]", 0).size)
        assertEquals(0, CompletedIds.parse(null, 0).size)
    }

    @Test fun fullRangesAndResumeNeverReportsUnverifiedFullRange() {
        assertEquals(listOf(Range(0, 10, 10)), TransferCore.fullRanges(10))
        val offer = FileOffer(id, "files", "a.bin", 10, JSONObject())
        val saved = PartialState(offer, "tok", listOf(Range(0, 10, 10)), 0)
        assertEquals(listOf(Range(0, 10, 9)), TransferCore.resumeRanges(offer, saved, "tok"))
    }
}
