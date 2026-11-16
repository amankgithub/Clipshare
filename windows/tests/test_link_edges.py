"""Link edge cases: handshake field checks, auth replay, liveness, feature callbacks, failure paths."""

import asyncio
import json
import logging
import os
from types import SimpleNamespace
from typing import Any, AsyncIterator

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from clipbridge.link import crypto
from clipbridge.link.handshake import b64d, b64e
from clipbridge.link.server import Link
from tclient import PSK, MemoryDevices, TestClient, closed_code, until_connected

DEVICES = ("dev-1", "same")


class Recorder:
    name = "rec"
    version = 1

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    async def on_peer_connected(self, peer) -> None:
        self.events.append(("connected", peer))

    async def on_peer_disconnected(self, peer) -> None:
        self.events.append(("disconnected", peer))

    async def on_message(self, peer, header, body):
        return None


@pytest.fixture
def devices() -> MemoryDevices:
    return MemoryDevices(*DEVICES)


@pytest.fixture
async def env(tmp_path, devices) -> AsyncIterator[tuple[Link, Recorder]]:
    link = Link(devices, tmp_path / "partial", host="127.0.0.1", port=0, name="Test PC")
    rec = Recorder()
    link.register(rec)
    await link.start()
    yield link, rec
    await link.stop()


def raw(ws) -> SimpleNamespace:
    """Adapt a bare websocket to what `tclient.closed_code` expects."""
    return SimpleNamespace(ws=ws)


async def wait_for(cond, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        assert asyncio.get_running_loop().time() < deadline, "condition never became true"
        await asyncio.sleep(0.01)


async def raw_challenge(port: int):
    ws = await connect(f"ws://127.0.0.1:{port}/", subprotocols=["clipbridge.v1"])
    sn = b64d(json.loads(await ws.recv())["sn"], 16)
    return ws, sn


def auth_msg(cn: bytes, device: str, mac: bytes) -> dict[str, Any]:
    return {"type": "auth", "v": 1, "cn": b64e(cn), "device": device, "mac": b64e(mac)}


GOOD16, GOOD32 = b64e(b"\x01" * 16), b64e(b"\x02" * 32)
OK: dict[str, Any] = {"type": "auth", "v": 1, "cn": GOOD16, "device": "dev-1", "mac": GOOD32}


def without(key: str) -> dict[str, Any]:
    return {k: v for k, v in OK.items() if k != key}


@pytest.mark.parametrize(
    "message, code",
    [
        ("not json{", 4400),
        ("[1, 2]", 4400),
        ('"auth"', 4400),
        ({**OK, "type": "nope"}, 4400),
        ({**OK, "v": "1"}, 4400),
        ({**OK, "v": True}, 4400),
        ({**OK, "v": 1.5}, 4400),
        ({**OK, "v": 2}, 4426),
        ({**OK, "cn": "!!!not base64"}, 4400),
        ({**OK, "cn": b64e(b"\x01" * 15)}, 4400),
        ({**OK, "cn": b64e(b"\x01" * 17)}, 4400),
        ({**OK, "cn": 5}, 4400),
        ({**OK, "mac": "%%%%"}, 4400),
        ({**OK, "mac": b64e(b"\x02" * 31)}, 4400),
        ({**OK, "mac": b64e(b"\x02" * 33)}, 4400),
        ({**OK, "mac": None}, 4400),
        ({**OK, "device": "bad device"}, 4400),
        ({**OK, "device": ""}, 4400),
        ({**OK, "device": "d" * 65}, 4400),
        ({**OK, "device": 7}, 4400),
        ({**OK, "device": "dév"}, 4400),
        (without("cn"), 4400),
        (without("device"), 4400),
        (without("mac"), 4400),
        (without("v"), 4400),
    ],
)
async def test_malformed_auth_rejected_with_code(env, message, code) -> None:
    link, _ = env
    ws, _ = await raw_challenge(link.port)
    await ws.send(message if isinstance(message, str) else json.dumps(message))
    assert await closed_code(raw(ws)) == code
    assert link.peers == []


async def test_binary_auth_frame_4400(env) -> None:
    link, _ = env
    ws, _ = await raw_challenge(link.port)
    await ws.send(json.dumps(OK).encode())
    assert await closed_code(raw(ws)) == 4400


async def test_auth_replayed_on_new_connection_4401(env) -> None:
    link, _ = env
    ws1, sn1 = await raw_challenge(link.port)
    cn = os.urandom(16)
    auth = json.dumps(auth_msg(cn, "dev-1", crypto.client_mac(PSK, sn1, cn, "dev-1")))
    await ws1.send(auth)
    assert json.loads(await ws1.recv())["type"] == "auth_ok"
    ws2, _ = await raw_challenge(link.port)  # fresh server nonce: the old MAC no longer fits
    await ws2.send(auth)
    assert await closed_code(raw(ws2)) == 4401
    await ws1.close()


async def test_wrong_subprotocol_rejected(env) -> None:
    link, _ = env
    with pytest.raises(InvalidStatus) as exc:
        await connect(f"ws://127.0.0.1:{link.port}/", subprotocols=["clipbridge.v2"])  # type: ignore[list-item]
    assert exc.value.response.status_code == 400


async def test_garbled_first_frame_after_handshake_4402(env) -> None:
    link, rec = env
    c = await TestClient.connect(link.port, PSK, hello=False)
    await c.ws.send(os.urandom(64))
    assert await closed_code(c) == 4402
    assert link.peers == [] and rec.events == []


async def test_request_returns_none_at_once_when_send_fails(env, monkeypatch) -> None:
    link, _ = env
    monkeypatch.setattr("clipbridge.link.requests.REQUEST_TIMEOUT", 30.0)
    c = await TestClient.connect(link.port, PSK)
    await until_connected(link, "dev-1")
    peer = link.peers[0]
    await c.ws.close()
    await peer.ws.wait_closed()
    result = await asyncio.wait_for(link.request(peer, {"ch": "rec", "type": "q"}), 2)
    assert result is None


class NameFails(MemoryDevices):
    def set_name(self, device: str, name: str) -> None:
        raise OSError("disk full")


async def test_set_name_failure_is_logged_and_link_stays_up(tmp_path, caplog) -> None:
    link = Link(NameFails("dev-1"), tmp_path / "partial", host="127.0.0.1", port=0)
    rec = Recorder()
    link.register(rec)
    await link.start()
    try:
        with caplog.at_level(logging.ERROR, logger="clipbridge.link.server"):
            c = await TestClient.connect(link.port, PSK)
            await until_connected(link, "dev-1")
        assert "storing the name" in caplog.text
        assert [e[0] for e in rec.events] == ["connected"]
        await c.send({"ch": "rec", "type": "x", "mid": "m1"})
        header, _ = await asyncio.wait_for(c.recv(), 5)
        assert header["mid"] == "m1" and header["ok"] is True
        await c.ws.close()
    finally:
        await link.stop()


async def test_client_that_stops_answering_pings_is_closed_by_server(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("clipbridge.link.server.PING_INTERVAL", 0.2)
    monkeypatch.setattr("clipbridge.link.server.PING_TIMEOUT", 0.3)
    link = Link(MemoryDevices("dev-1"), tmp_path / "partial", host="127.0.0.1", port=0)
    rec = Recorder()
    link.register(rec)
    await link.start()
    try:
        c = await TestClient.connect(link.port, PSK)
        await until_connected(link, "dev-1")
        peer = link.peers[0]
        c.ws.transport.pause_reading()  # no pongs from here on
        for _ in range(100):
            if peer.ws.protocol.close_sent is not None:
                break
            await asyncio.sleep(0.05)
        # the server gives up on the silent client itself (1011 keepalive ping timeout)
        assert peer.ws.protocol.close_sent is not None and peer.ws.protocol.close_sent.code == 1011
        c.ws.transport.resume_reading()  # let the close handshake finish so teardown is quick
    finally:
        await link.stop()


async def test_peer_callbacks_fire_once_each(env) -> None:
    link, rec = env
    c = await TestClient.connect(link.port, PSK)
    await until_connected(link, "dev-1")
    peer = link.peers[0]
    assert rec.events == [("connected", peer)]
    await c.ws.close()
    await wait_for(lambda: len(rec.events) >= 2)
    assert rec.events == [("connected", peer), ("disconnected", peer)]


async def test_replaced_peer_gets_one_disconnect_and_new_one_connect(env) -> None:
    link, rec = env
    old = await TestClient.connect(link.port, PSK, "same")
    await until_connected(link, "same")
    old_peer = link.peers[0]
    new = await TestClient.connect(link.port, PSK, "same")
    assert await closed_code(old) == 4409
    await wait_for(lambda: len(rec.events) >= 3 and len(link.peers) == 1 and link.peers[0] is not old_peer)
    new_peer = link.peers[0]
    assert new_peer is not old_peer
    assert rec.events.count(("connected", old_peer)) == 1
    assert rec.events.count(("disconnected", old_peer)) == 1
    assert rec.events.count(("connected", new_peer)) == 1
    assert rec.events.count(("disconnected", new_peer)) == 0
    await new.ws.close()
    await wait_for(lambda: len(rec.events) >= 4)
    assert rec.events.count(("disconnected", new_peer)) == 1
    assert len(rec.events) == 4
