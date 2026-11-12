"""Test client implementing the client side of PROTOCOL.md on top of the link primitives."""

from __future__ import annotations

import json
import os
from typing import Any

from websockets.asyncio.client import ClientConnection, connect

from clipbridge.link import crypto, envelope
from clipbridge.link.handshake import b64d, b64e


class TestClient:
    __test__ = False

    def __init__(self, ws: ClientConnection, channel: crypto.SecureChannel) -> None:
        self.ws = ws
        self.channel = channel

    @classmethod
    async def connect(
        cls, port: int, psk: bytes, device: str = "dev-1", subprotocol: str = "clipbridge.v1"
    ) -> "TestClient":
        """Control connection by default; pass subprotocol="clipbridge.data.v1" for a data connection."""
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
        return cls(ws, crypto.SecureChannel(send_key=c2s, recv_key=s2c))

    async def send(self, header: dict[str, Any], body: bytes = b"") -> None:
        await self.ws.send(self.channel.encrypt(envelope.encode(header, body)))

    async def recv(self) -> tuple[dict[str, Any], bytes]:
        frame = await self.ws.recv()
        assert isinstance(frame, bytes)
        return envelope.decode(self.channel.decrypt(frame))
