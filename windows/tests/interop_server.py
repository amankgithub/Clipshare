"""Harness for the Kotlin<->Python interop tests (android/app/src/test/.../interop). Not a pytest module.

Starts a real `Link` on 127.0.0.1 (port 0) with a real `ClipboardFeature` whose text writer only records.
stdin line 1 is the config JSON `{"devices": {id: psk_hex}, "no_hello": bool, "kick_on_connect": code|null}`
(PSKs never go through argv or logs); every later stdin line is a command:
  {"cmd": "send_clip", "text": ...} | {"cmd": "kick", "code": n} | {"cmd": "stop"} | {"cmd": "start"} | {"cmd": "quit"}
stdout gets one JSON line per event: listening(port), connected(device), disconnected(device, code),
auth_failed, clip(len, hash), stopped. Clip texts are never printed, only length and hash.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from clipbridge.features.clipboard.feature import ClipboardFeature
from clipbridge.link import peers
from clipbridge.link.peers import Peer
from clipbridge.link.server import Link
from tclient import MemoryDevices


def emit(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}), flush=True)


def clip_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:8]


class Probe:
    """Link feature that reports peer connect/disconnect and can kick a peer as soon as it is connected."""

    name = "interop"
    version = 1

    def __init__(self, kick_on_connect: int | None) -> None:
        self._kick = kick_on_connect

    async def on_peer_connected(self, peer: Peer) -> None:
        emit("connected", device=peer.device)
        if self._kick is not None:
            await peer.ws.close(self._kick)

    async def on_peer_disconnected(self, peer: Peer) -> None:
        emit("disconnected", device=peer.device, code=peer.ws.close_code)

    async def on_message(self, peer: Peer, header: dict[str, Any], body: bytes) -> None:
        return None


class AuthFailures(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage().startswith("auth failed"):
            emit("auth_failed")


class Harness:
    def __init__(self, config: dict[str, Any]) -> None:
        self.devices = MemoryDevices()
        self.devices.keys = {d: bytes.fromhex(k) for d, k in config["devices"].items()}
        self.kick_on_connect: int | None = config.get("kick_on_connect")
        self.partial = Path(tempfile.mkdtemp(prefix="cb-interop-"))
        self.port = 0
        self.link: Link | None = None
        if config.get("no_hello"):  # complete auth, then stay silent: the client never gets the server hello
            original = peers.Peer.send

            async def send(peer: Peer, header: dict[str, Any], body: bytes = b"") -> None:
                if header.get("ch") == "link" and header.get("type") == "hello":
                    return
                await original(peer, header, body)

            peers.Peer.send = send  # type: ignore[method-assign]

    def _record(self, text: str) -> None:
        emit("clip", len=len(text), hash=clip_hash(text))

    async def start(self) -> None:
        link = Link(self.devices, self.partial, host="127.0.0.1", port=self.port, name="Interop PC")
        self.clipboard = ClipboardFeature(link, self._record)
        link.register(self.clipboard)
        link.register(Probe(self.kick_on_connect))
        await link.start()
        self.link = link
        self.port = link.port
        emit("listening", port=self.port)

    async def stop(self) -> None:
        if self.link is not None:
            await self.link.stop()
            self.link = None
            emit("stopped")

    async def command(self, cmd: dict[str, Any]) -> bool:
        """Run one command; False means quit."""
        name = cmd["cmd"]
        if name == "send_clip" and self.link is not None:
            await self.clipboard.on_local_text(cmd["text"])
        elif name == "kick" and self.link is not None:
            for peer in self.link.peers:
                await peer.ws.close(cmd["code"])
        elif name == "stop":
            await self.stop()
        elif name == "start" and self.link is None:
            await self.start()
        elif name == "quit":
            return False
        return True


async def main() -> None:
    loop = asyncio.get_running_loop()
    lines: asyncio.Queue[str | None] = asyncio.Queue()

    def read_stdin() -> None:
        for line in sys.stdin:
            loop.call_soon_threadsafe(lines.put_nowait, line)
        loop.call_soon_threadsafe(lines.put_nowait, None)

    threading.Thread(target=read_stdin, daemon=True).start()
    first = await lines.get()
    assert first is not None
    harness = Harness(json.loads(first))
    logging.getLogger("clipbridge.link.server").addHandler(AuthFailures())
    await harness.start()
    while True:
        line = await lines.get()
        if line is None or not await harness.command(json.loads(line)):
            break
    await harness.stop()


if __name__ == "__main__":
    asyncio.run(main())
