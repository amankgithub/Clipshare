package com.clipbridge.link

import java.io.File

/**
 * Receiver state in app-private storage: one small JSON file per unfinished transfer `id`, plus
 * `completed.json`, the record of recently completed ids ([CompletedIds]).
 */
class PartialStore(private val dir: File) {
    private fun file(id: String) = File(dir, "$id.json")
    private val completedFile = File(dir, COMPLETED)

    fun load(id: String): PartialState? =
        runCatching { TransferCore.parseState(file(id).readText()) }.getOrNull()

    fun save(s: PartialState) = write(file(s.offer.id), TransferCore.stateJson(s))

    fun delete(id: String) { file(id).delete() }

    fun loadCompleted(nowMs: Long): CompletedIds =
        CompletedIds.parse(runCatching { completedFile.readText() }.getOrNull(), nowMs)

    fun saveCompleted(ids: CompletedIds) = write(completedFile, ids.toJson())

    /** Write-then-rename, so a crash never leaves a torn file. */
    private fun write(target: File, text: String) {
        dir.mkdirs()
        val tmp = File(dir, "${target.nameWithoutExtension}.tmp")
        tmp.writeText(text)
        tmp.renameTo(target)
    }

    /** Forget transfers the sender has not resumed for [maxAgeMs] (and unreadable state files). */
    fun prune(nowMs: Long, maxAgeMs: Long) {
        dir.listFiles().orEmpty().forEach { f ->
            if (f.name == COMPLETED) return@forEach // bounds its own age
            val s = runCatching { TransferCore.parseState(f.readText()) }.getOrNull()
            if (s == null || nowMs - s.updatedMs > maxAgeMs) f.delete()
        }
    }

    private companion object {
        const val COMPLETED = "completed.json"
    }
}
