package com.clipbridge

import com.clipbridge.features.clipboard.SyncEngine
import com.clipbridge.features.clipboard.SyncEngine.Local
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SyncEngineTest {
    @Test fun localSendsWhenConnectedAndDropsDuplicate() {
        val e = SyncEngine()
        assertEquals(Local.Send("a"), e.onLocal("a", true))
        assertEquals(Local.Drop, e.onLocal("a", true))
        assertEquals(Local.Send("b"), e.onLocal("b", true))
    }

    @Test fun remoteWriteThenLocalEchoIsDropped() {
        val e = SyncEngine()
        assertTrue(e.onRemote("x"))
        assertEquals(Local.Drop, e.onLocal("x", true)) // clipboard change caused by our own write
        assertFalse(e.onRemote("x"))
    }

    @Test fun sentThenRemoteSameTextIsDropped() {
        val e = SyncEngine()
        e.onLocal("x", true)
        assertFalse(e.onRemote("x"))
    }

    @Test fun holdsLatestWhileOfflineAndFlushesOnConnect() {
        val e = SyncEngine()
        assertEquals(Local.Hold, e.onLocal("one", false))
        assertEquals(Local.Hold, e.onLocal("two", false))
        assertEquals("two", e.onConnected())
        assertNull(e.onConnected())
    }

    @Test fun noFullStatePushOnConnect() {
        val e = SyncEngine()
        e.onLocal("sent", true)
        assertNull(e.onConnected())
    }

    @Test fun remoteClearsPendingLastWriterWins() {
        val e = SyncEngine()
        e.onLocal("old", false)
        assertTrue(e.onRemote("new"))
        assertNull(e.onConnected())
    }

    @Test fun emojiHashStable() {
        val e = SyncEngine()
        assertEquals(Local.Send("😀"), e.onLocal("😀", true))
        assertEquals(Local.Drop, e.onLocal("😀", true))
    }
}
