package com.clipbridge.ui

import com.clipbridge.Settings
import com.clipbridge.link.Pairing

/** What [PairExtras.apply] did with the intent extras of [PairActivity]. */
class PairResult(val changed: Boolean, val invalidPairing: Boolean)

/** The extras of [PairActivity]: device id and PSK come together, the host is optional. */
object PairExtras {
    fun apply(s: Settings, device: String?, psk: String?, host: String?): PairResult {
        var changed = false
        var invalid = false
        if (device != null || psk != null) {
            val pairing = if (device != null && psk != null) Pairing.of(device, psk.trim()) else null
            if (pairing != null) { s.pairing = pairing; changed = true } else invalid = true
        }
        if (host != null) { s.manualHost = host; changed = true }
        return PairResult(changed, invalid)
    }
}
