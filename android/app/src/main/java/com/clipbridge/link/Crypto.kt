package com.clipbridge.link

import java.security.MessageDigest
import javax.crypto.Cipher
import javax.crypto.Mac
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/** Thrown when an encrypted frame fails any integrity check (close 4402). */
class FrameException(message: String) : Exception(message)

/** Pure crypto per PROTOCOL.md sections 3-5. No I/O. */
object Crypto {
    fun hmac(key: ByteArray, msg: ByteArray): ByteArray {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(key, "HmacSHA256"))
        return mac.doFinal(msg)
    }

    /** HKDF-SHA256 (RFC 5869), single 32-byte block. */
    fun hkdf(ikm: ByteArray, salt: ByteArray, info: ByteArray): ByteArray {
        val prk = hmac(salt, ikm)
        return hmac(prk, info + byteArrayOf(1))
    }

    fun clientMac(psk: ByteArray, sn: ByteArray, cn: ByteArray, device: String): ByteArray =
        hmac(psk, byteArrayOf(0x63) + sn + cn + device.toByteArray(Charsets.UTF_8))

    fun serverMac(psk: ByteArray, sn: ByteArray, cn: ByteArray): ByteArray =
        hmac(psk, byteArrayOf(0x73) + sn + cn)

    /** (key client->server, key server->client). */
    fun sessionKeys(psk: ByteArray, sn: ByteArray, cn: ByteArray): Pair<ByteArray, ByteArray> {
        val salt = sn + cn
        return hkdf(psk, salt, "c2s".toByteArray()) to hkdf(psk, salt, "s2c".toByteArray())
    }

    fun constantTimeEquals(a: ByteArray, b: ByteArray): Boolean = MessageDigest.isEqual(a, b)

    fun nonce(counter: Long): ByteArray {
        val n = ByteArray(12)
        for (i in 0 until 8) n[i] = (counter ushr (8 * i)).toByte()
        return n
    }

    fun encryptFrame(key: ByteArray, counter: Long, plaintext: ByteArray): ByteArray {
        val nonce = nonce(counter)
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.ENCRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(128, nonce))
        return nonce + c.doFinal(plaintext)
    }

    /** Checks in spec order: length, nonce padding, counter, GCM. */
    fun decryptFrame(key: ByteArray, expectedCounter: Long, frame: ByteArray): ByteArray {
        if (frame.size < 28) throw FrameException("short frame")
        for (i in 8 until 12) if (frame[i].toInt() != 0) throw FrameException("nonce padding")
        var counter = 0L
        for (i in 0 until 8) counter = counter or ((frame[i].toLong() and 0xff) shl (8 * i))
        if (counter != expectedCounter) throw FrameException("counter")
        try {
            val c = Cipher.getInstance("AES/GCM/NoPadding")
            c.init(Cipher.DECRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(128, frame.copyOfRange(0, 12)))
            return c.doFinal(frame, 12, frame.size - 12)
        } catch (e: java.security.GeneralSecurityException) {
            throw FrameException("gcm")
        }
    }
}

/** Per-connection counters + keys. Not thread-safe: callers serialize. */
class FrameCipher(private val sendKey: ByteArray, private val recvKey: ByteArray) {
    private var sendCounter = 0L
    private var recvCounter = 0L

    fun encrypt(plaintext: ByteArray): ByteArray {
        if (sendCounter == Long.MAX_VALUE) throw FrameException("counter exhausted")
        return Crypto.encryptFrame(sendKey, sendCounter++, plaintext)
    }

    fun decrypt(frame: ByteArray): ByteArray {
        val p = Crypto.decryptFrame(recvKey, recvCounter, frame)
        recvCounter++
        return p
    }
}

/** Strict base64 (padded, standard alphabet) with an exact decoded length. */
object B64 {
    private val re = Regex("^[A-Za-z0-9+/]*={0,2}$")
    fun decode(s: String, expectedLen: Int): ByteArray? {
        if (s.length % 4 != 0 || !re.matches(s)) return null
        val b = try { java.util.Base64.getDecoder().decode(s) } catch (e: IllegalArgumentException) { return null }
        return if (b.size == expectedLen) b else null
    }
    fun encode(b: ByteArray): String = java.util.Base64.getEncoder().encodeToString(b)
}
