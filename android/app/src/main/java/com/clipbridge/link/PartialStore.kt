package com.clipbridge.link

import java.io.File

/** Receiver resume state, one small JSON file per unfinished transfer `id`, in app-private storage. */
class PartialStore(private val dir: File) {
    private fun file(id: String) = File(dir, "$id.json")

    fun load(id: String): PartialState? =
        runCatching { TransferCore.parseState(file(id).readText()) }.getOrNull()

    /** Write-then-rename, so a crash never leaves a torn state file. */
    fun save(s: PartialState) {
        dir.mkdirs()
        val tmp = File(dir, "${s.offer.id}.tmp")
        tmp.writeText(TransferCore.stateJson(s))
        tmp.renameTo(file(s.offer.id))
    }

    fun delete(id: String) { file(id).delete() }

    /** Forget transfers the sender has not resumed for [maxAgeMs] (and unreadable state files). */
    fun prune(nowMs: Long, maxAgeMs: Long) {
        dir.listFiles().orEmpty().forEach { f ->
            val s = runCatching { TransferCore.parseState(f.readText()) }.getOrNull()
            if (s == null || nowMs - s.updatedMs > maxAgeMs) f.delete()
        }
    }
}
