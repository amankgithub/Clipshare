package com.clipbridge.interop

import org.json.JSONObject
import org.junit.Assume
import java.io.File
import java.util.concurrent.CopyOnWriteArrayList

/** One line the harness printed, with the time we read it. */
class Ev(val name: String, val json: JSONObject, val atMs: Long = System.currentTimeMillis()) {
    override fun toString() = json.toString()
}

/**
 * The real Python link (`windows/tests/interop_server.py`) as a child process on 127.0.0.1.
 * Config (device ids and PSK hex) goes over stdin only; stderr goes to a per-instance temp file, deleted by [kill].
 */
class PythonServer private constructor(private val proc: Process, private val err: File) {
    val events = CopyOnWriteArrayList<Ev>()
    @Volatile var port = 0
        private set
    private val stdin = proc.outputStream.bufferedWriter()

    init {
        Thread {
            proc.inputStream.bufferedReader().forEachLine { line ->
                val j = runCatching { JSONObject(line) }.getOrNull() ?: return@forEachLine
                val ev = Ev(j.getString("event"), j)
                if (ev.name == "listening") port = j.getInt("port")
                events += ev
            }
        }.apply { isDaemon = true }.start()
    }

    private fun command(c: JSONObject) {
        synchronized(stdin) { stdin.write(c.toString() + "\n"); stdin.flush() }
    }

    fun sendClip(text: String) = command(JSONObject().put("cmd", "send_clip").put("text", text))
    fun kick(code: Int) = command(JSONObject().put("cmd", "kick").put("code", code))
    fun stop() = command(JSONObject().put("cmd", "stop"))
    fun start() = command(JSONObject().put("cmd", "start"))

    fun count(name: String) = events.count { it.name == name }

    /** The [n]-th (1-based) event called [name] that arrives within [timeoutMs]; AssertionError otherwise. */
    fun await(name: String, n: Int = 1, timeoutMs: Long = 8_000): Ev {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (true) {
            events.filter { it.name == name }.getOrNull(n - 1)?.let { return it }
            if (System.currentTimeMillis() > deadline) throw AssertionError("no '$name' #$n within $timeoutMs ms; events=${events.map { it.name }}")
            Thread.sleep(10)
        }
    }

    fun kill() {
        runCatching { command(JSONObject().put("cmd", "quit")) }
        if (!proc.waitFor(2, java.util.concurrent.TimeUnit.SECONDS)) {
            proc.destroyForcibly(); proc.waitFor(2, java.util.concurrent.TimeUnit.SECONDS)
        }
        err.delete()
    }

    companion object {
        private fun root(): File? =
            generateSequence(File(System.getProperty("user.dir")!!).absoluteFile) { it.parentFile }
                .firstOrNull { File(it, "windows/.venv/Scripts/python.exe").exists() }

        /** Skips the calling test (Assume) when the Python venv is not there. */
        fun requirePython(): File {
            val r = root()
            Assume.assumeTrue("interop skipped: windows/.venv/Scripts/python.exe not found", r != null)
            return r!!
        }

        /** [devices]: device id to PSK. [noHello]: never send the server hello. [kickOnConnect]: close each peer with this code once connected. */
        fun launch(
            devices: Map<String, ByteArray>,
            noHello: Boolean = false,
            kickOnConnect: Int? = null,
        ): PythonServer {
            val r = requirePython()
            val err = File.createTempFile("cb-interop-python", ".err")
            val proc = ProcessBuilder(
                File(r, "windows/.venv/Scripts/python.exe").path, "-u", File(r, "windows/tests/interop_server.py").path,
            ).directory(File(r, "windows/tests")).redirectError(err).start()
            val server = PythonServer(proc, err)
            try {
                val cfg = JSONObject()
                    .put("devices", JSONObject().also { d -> devices.forEach { (id, k) -> d.put(id, k.joinToString("") { "%02x".format(it) }) } })
                    .put("no_hello", noHello).put("kick_on_connect", kickOnConnect ?: JSONObject.NULL)
                proc.outputStream.write((cfg.toString() + "\n").toByteArray()); proc.outputStream.flush()
                server.await("listening", timeoutMs = 15_000)
            } catch (e: Throwable) {
                server.kill()
                throw e
            }
            return server
        }
    }
}
