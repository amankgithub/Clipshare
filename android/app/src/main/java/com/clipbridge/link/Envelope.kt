package com.clipbridge.link

import org.json.JSONException
import org.json.JSONObject
import org.json.JSONTokener
import java.nio.ByteBuffer
import java.nio.charset.CharacterCodingException
import java.nio.charset.CodingErrorAction

/** `ch` of link-layer messages (PROTOCOL.md section 6); never a feature's. */
const val LINK_CH = "link"

class EnvelopeException(message: String) : Exception(message)

class Envelope(val header: JSONObject, val body: ByteArray)

/** plaintext = u32 BE header_len || header JSON (UTF-8) || body. */
object Envelopes {
    const val MAX_PLAINTEXT = 16 * 1024 * 1024

    fun encode(header: JSONObject, body: ByteArray = ByteArray(0)): ByteArray {
        val h = header.toString().toByteArray(Charsets.UTF_8)
        return ByteBuffer.allocate(4 + h.size + body.size).putInt(h.size).put(h).put(body).array()
    }

    fun parse(plaintext: ByteArray): Envelope {
        if (plaintext.size < 4) throw EnvelopeException("short")
        val len = ByteBuffer.wrap(plaintext, 0, 4).int
        if (len < 0 || len > plaintext.size - 4) throw EnvelopeException("header_len")
        val text = try {
            Charsets.UTF_8.newDecoder()
                .onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT)
                .decode(ByteBuffer.wrap(plaintext, 4, len)).toString()
        } catch (e: CharacterCodingException) {
            throw EnvelopeException("utf8")
        }
        val header = try {
            JSONTokener(text).nextValue() as? JSONObject ?: throw EnvelopeException("header not object")
        } catch (e: JSONException) {
            throw EnvelopeException("json")
        }
        if (header.opt("ch") !is String) throw EnvelopeException("ch")
        return Envelope(header, plaintext.copyOfRange(4 + len, plaintext.size))
    }
}
