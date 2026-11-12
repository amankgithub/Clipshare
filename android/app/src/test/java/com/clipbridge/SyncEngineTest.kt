package com.clipbridge

import com.clipbridge.features.clipboard.Clip
import com.clipbridge.features.clipboard.SyncEngine
import com.clipbridge.features.clipboard.SyncEngine.Local
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SyncEngineTest {
    private fun t(s: String) = Clip.Text(s)
    private fun img(vararg b: Int) = Clip.Image(ByteArray(b.size) { b[it].toByte() })

    @Test fun localSendsWhenConnectedAndDropsDuplicate() {
        val e = SyncEngine()
        assertEquals(Local.Send(t("a")), e.onLocal(t("a"), true))
        assertEquals(Local.Drop, e.onLocal(t("a"), true))
        assertEquals(Local.Send(t("b")), e.onLocal(t("b"), true))
    }

    @Test fun remoteWriteThenLocalEchoIsDropped() {
        val e = SyncEngine()
        assertTrue(e.onRemote(t("x")))
        assertEquals(Local.Drop, e.onLocal(t("x"), true)) // clipboard change caused by our own write
        assertFalse(e.onRemote(t("x")))
    }

    @Test fun sentThenRemoteSameTextIsDropped() {
        val e = SyncEngine()
        e.onLocal(t("x"), true)
        assertFalse(e.onRemote(t("x")))
    }

    @Test fun holdsLatestWhileOfflineAndFlushesOnConnect() {
        val e = SyncEngine()
        assertEquals(Local.Hold, e.onLocal(t("one"), false))
        assertEquals(Local.Hold, e.onLocal(t("two"), false))
        assertEquals(t("two"), e.onConnected())
        assertNull(e.onConnected())
    }

    @Test fun noFullStatePushOnConnect() {
        val e = SyncEngine()
        e.onLocal(t("sent"), true)
        assertNull(e.onConnected())
    }

    @Test fun remoteClearsPendingLastWriterWins() {
        val e = SyncEngine()
        e.onLocal(t("old"), false)
        assertTrue(e.onRemote(t("new")))
        assertNull(e.onConnected())
    }

    @Test fun emojiHashStable() {
        val e = SyncEngine()
        assertEquals(Local.Send(t("😀")), e.onLocal(t("😀"), true))
        assertEquals(Local.Drop, e.onLocal(t("😀"), true))
    }

    @Test fun imageEchoDropped() {
        val e = SyncEngine()
        assertTrue(e.onRemote(img(1, 2, 3)))
        assertEquals(Local.Drop, e.onLocal(img(1, 2, 3), true)) // our own FileProvider write
        assertFalse(e.onRemote(img(1, 2, 3)))
    }

    @Test fun sentImageThenRemoteSameImageDropped() {
        val e = SyncEngine()
        assertEquals(Local.Send(img(9, 9)), e.onLocal(img(9, 9), true))
        assertFalse(e.onRemote(img(9, 9)))
    }

    @Test fun textImageAlternationSharesOneSlot() {
        val e = SyncEngine()
        assertEquals(Local.Send(t("a")), e.onLocal(t("a"), true))
        assertEquals(Local.Send(img(1)), e.onLocal(img(1), true))
        assertEquals(Local.Send(t("a")), e.onLocal(t("a"), true)) // slot moved on, text sends again
        assertTrue(e.onRemote(img(1)))
        assertEquals(Local.Drop, e.onLocal(img(1), true))
    }

    @Test fun pendingImageFlushesOnConnect() {
        val e = SyncEngine()
        assertEquals(Local.Hold, e.onLocal(img(5), false))
        assertEquals(img(5), e.onConnected())
    }
}
