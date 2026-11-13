"""Test client implementing the client side of PROTOCOL.md on top of the link primitives."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from websockets.asyncio.client import ClientConnection, connect

from clipbridge.link import crypto, envelope
from clipbridge.link.handshake import b64d, b64e

PSK = bytes(range(32))
FEATURES = {"clipboard": 1, "files": 1}


class MemoryDevices:
    """In-memory device registry (`link.server.Devices`): every device here has key `PSK` unless given."""

    def __init__(self, *devices: str, psk: bytes = PSK) -> None:
        self.keys = {d: psk for d in devices}
        self.names: dict[str, str] = {}

    def psk(self, device: str) -> bytes | None:
        return self.keys.get(device)

    def set_name(self, device: str, name: str) -> None:
        self.names[device] = name


class TestClient:
    __test__ = False

    def __init__(self, ws: ClientConnection, channel: crypto.SecureChannel) -> None:
        self.ws = ws
        self.channel = channel
        self.server_hello: dict[str, Any] | None = None

    @classmethod
    async def connect(
        cls,
        port: int,
        psk: bytes = PSK,
        device: str = "dev-1",
        subprotocol: str = "clipbridge.v1",
        features: dict[str, int] | None = None,
        hello: bool = True,
    ) -> "TestClient":
        """Control connection by default (reads the server hello, sends ours unless `hello=False`);
        pass subprotocol="clipbridge.data.v1" for a data connection."""
        ws = await connect(
            f"ws://127.0.0.1:{port}/", subprotocols=[subprotocol], max_size=None, compression=None  # type: ignore[list-item]
        )
        assert ws.subprotocol == subprotocol
        challenge = json.loads(await ws.recv())
        sn = b64d(challenge["sn"], 16)
        cn = os.urandom(16)
        auth = {
            "type": "auth",
            "v": 1,
            "cn": b64e(cn),
            "device": device,
            "mac": b64e(crypto.client_mac(psk, sn, cn, device)),
        }
        await ws.send(json.dumps(auth))
        ok = json.loads(await ws.recv())
        assert ok["type"] == "auth_ok"
        assert b64d(ok["mac"], 32) == crypto.server_mac(psk, sn, cn)
        c2s, s2c = crypto.derive_keys(psk, sn, cn)
        client = cls(ws, crypto.SecureChannel(send_key=c2s, recv_key=s2c))
        if subprotocol == "clipbridge.v1":
            client.server_hello, _ = await client.recv()
            assert client.server_hello["type"] == "hello"
            if hello:
                await client.send_hello(FEATURES if features is None else features)
        return client

    async def send_hello(self, features: dict[str, int], name: str = "Test phone") -> None:
        await self.send({"ch": "link", "type": "hello", "name": name, "features": features})

    async def send(self, header: dict[str, Any], body: bytes = b"") -> None:
        await self.ws.send(self.channel.encrypt(envelope.encode(header, body)))

    async def recv(self) -> tuple[dict[str, Any], bytes]:
        frame = await self.ws.recv()
        assert isinstance(frame, bytes)
        return envelope.decode(self.channel.decrypt(frame))


async def until_connected(link: Any, device: str, timeout: float = 5.0) -> None:
    """Wait until the server has `device`'s hello (it reaches `link.peers` only then)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not any(p.device == device for p in link.peers):
        assert loop.time() < deadline, f"{device} never connected"
        await asyncio.sleep(0.01)
