package com.clipbridge.link

import org.json.JSONObject

/** The `hello` capability exchange (PROTOCOL.md section 6.1): who the peer is and which features it handles. */
class Hello(val name: String, val features: Map<String, Int>) {

    fun header(): JSONObject {
        val f = JSONObject()
        features.forEach { (k, v) -> f.put(k, v) }
        return JSONObject().put("ch", LINK_CH).put("type", TYPE).put("name", name).put("features", f)
    }

    companion object {
        const val TYPE = "hello"
        const val MAX_NAME = 64
        const val MAX_FEATURES = 64
        private const val FALLBACK_NAME = "Android"

        fun isHello(header: JSONObject): Boolean = header.optString("ch") == LINK_CH && header.optString("type") == TYPE

        /** A valid `hello` from the peer, or null (the connection must then close 4400). */
        fun parse(header: JSONObject, body: ByteArray): Hello? {
            if (!isHello(header) || body.isNotEmpty()) return null
            val name = header.opt("name") as? String ?: return null
            if (!isValidName(name)) return null
            val f = header.opt("features") as? JSONObject ?: return null
            if (f.length() > MAX_FEATURES) return null
            val features = HashMap<String, Int>()
            for (k in f.keys()) {
                if (!FeatureNames.isFeature(k)) return null
                val v = when (val raw = f.get(k)) {
                    is Int -> raw.toLong()
                    is Long -> raw
                    else -> return null
                }
                if (v < 1 || v > Int.MAX_VALUE) return null
                features[k] = v.toInt()
            }
            return Hello(name, features)
        }

        /** 1 to 64 code points, no control characters (U+0000 to U+001F, U+007F). */
        fun isValidName(s: String): Boolean {
            val n = s.codePointCount(0, s.length)
            return n in 1..MAX_NAME && s.none { it.code < 0x20 || it.code == 0x7f }
        }

        /** [raw] (for example `Build.MODEL`) made into a valid hello name. */
        fun deviceName(raw: String?): String {
            val clean = (raw ?: "").filter { it.code >= 0x20 && it.code != 0x7f }.trim()
            val cut = if (clean.codePointCount(0, clean.length) > MAX_NAME) clean.substring(0, clean.offsetByCodePoints(0, MAX_NAME)) else clean
            return cut.ifEmpty { FALLBACK_NAME }
        }
    }
}

/** Feature-name rules (PROTOCOL.md section 6, Routing). */
object FeatureNames {
    private val NAME = Regex("[A-Za-z0-9._-]{1,64}")

    /** A name a feature may register under: valid and not reserved for the link. */
    fun isFeature(s: String): Boolean = NAME.matches(s) && !s.startsWith(LINK_CH)
}
