package com.clipbridge.link

/**
 * This device's credentials (PROTOCOL.md section 2): the device id and PSK the PC assigned when it
 * paired this device. They are only ever stored and replaced together.
 */
class Pairing(val device: String, val psk: ByteArray) {
    /** The manual pairing code, `<device>:<psk b64>`. */
    fun code(): String = "$device:${B64.encode(psk)}"

    companion object {
        private val DEVICE = Regex("[A-Za-z0-9._-]{1,64}")

        /** @return null if [device] is not a valid device id or [pskB64] is not a 32-byte PSK. */
        fun of(device: String, pskB64: String): Pairing? {
            if (!DEVICE.matches(device)) return null
            val psk = B64.decode(pskB64, 32) ?: return null
            return Pairing(device, psk)
        }

        /** Parses a pairing code `<device>:<psk b64>` (surrounding whitespace ignored), or null. */
        fun parseCode(code: String): Pairing? {
            val t = code.trim()
            val i = t.indexOf(':')
            if (i < 0) return null
            return of(t.substring(0, i), t.substring(i + 1))
        }
    }
}
