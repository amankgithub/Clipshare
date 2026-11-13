import asyncio
import json
import os
from typing import AsyncIterator

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from clipbridge.features.clipboard.feature import ClipboardFeature
from clipbridge.link import crypto
from clipbridge.link.handshake import b64d, b64e
from clipbridge.link.server import Link
from tclient import TestClient

PSK = bytes(range(32))


def clip(text: str) -> dict:
    return {"ch": "clipboard", "type": "clip", "kind": "text", "data": text, "ts": 1}


@pytest.fixture
async def env(tmp_path) -> AsyncIterator[tuple[Link, list[str]]]:
    link = Link(PSK, tmp_path / "partial", host="127.0.0.1", port=0)
    written: list[str] = []
    link.register(ClipboardFeature(link, written.append))
    await link.start()
    yield link, written
    await link.stop()


async def closed_code(ws) -> int:
    with pytest.raises(ConnectionClosed):
        await asyncio.wait_for(ws.recv(), 5)
    return ws.protocol.close_rcvd.code


async def test_good_handshake_and_clip(env) -> None:
    link, written = env
    c = await TestClient.connect(link.port, PSK)
    await c.send(clip("héllo 😀"))
    await asyncio.sleep(0.2)
    assert written == ["héllo 😀"]
    await c.ws.close()


async def test_receipt_sent_after_feature_handles(env) -> None:
    link, written = env
    c = await TestClient.connect(link.port, PSK)
    await c.send({**clip("a"), "mid": "m1"})
    header, body = await asyncio.wait_for(c.recv(), 5)
    assert header == {"ch": "link", "type": "ack", "mid": "m1"} and body == b""
    assert written == ["a"]
    await c.send({**clip("a"), "mid": "m2"})  # duplicate clip: ignored on purpose, still acked
    header, _ = await asyncio.wait_for(c.recv(), 5)
    assert header["mid"] == "m2"
    await c.ws.close()


async def test_no_receipt_for_invalid_mid_or_unknown_ch(env) -> None:
    link, _ = env
    c = await TestClient.connect(link.port, PSK)
    await c.send({**clip("a"), "mid": "bad mid!"})
    await c.send({"ch": "nope", "mid": "m1"})
    await c.send({"ch": "link", "type": "ack", "mid": "m2"})
    await c.send({**clip("b"), "mid": "ok"})
    header, _ = await asyncio.wait_for(c.recv(), 5)
    assert header["mid"] == "ok"
    await c.ws.close()


async def test_wrong_psk_4401(env) -> None:
    link, _ = env
    ws = await connect(f"ws://127.0.0.1:{link.port}/", subprotocols=["clipbridge.v1"])
    sn = b64d(json.loads(await ws.recv())["sn"], 16)
    cn = os.urandom(16)
    bad = crypto.client_mac(b"\x01" * 32, sn, cn, "dev")
    await ws.send(json.dumps({"type": "auth", "v": 1, "cn": b64e(cn), "device": "dev", "mac": b64e(bad)}))
    assert await closed_code(ws) == 4401


async def test_missing_subprotocol_rejected(env) -> None:
    link, _ = env
    with pytest.raises(InvalidStatus) as exc:
        await connect(f"ws://127.0.0.1:{link.port}/")
    assert exc.value.response.status_code == 400


async def test_bad_version_4426(env) -> None:
    link, _ = env
    ws = await connect(f"ws://127.0.0.1:{link.port}/", subprotocols=["clipbridge.v1"])
    await ws.recv()
    await ws.send(json.dumps({"type": "auth", "v": 2}))
    assert await closed_code(ws) == 4426


async def test_tampered_frame_4402(env) -> None:
    link, written = env
    c = await TestClient.connect(link.port, PSK)
    frame = bytearray(c.channel.encrypt(b"\x00\x00\x00\x00"))
    frame[15] ^= 1
    await c.ws.send(bytes(frame))
    assert await closed_code(c.ws) == 4402


async def test_replayed_frame_4402(env) -> None:
    link, written = env
    c = await TestClient.connect(link.port, PSK)
    from clipbridge.link import envelope

    frame = c.channel.encrypt(envelope.encode(clip("once")))
    await c.ws.send(frame)
    await c.ws.send(frame)
    assert await closed_code(c.ws) == 4402
    assert written == ["once"]


async def test_text_frame_after_handshake_4400(env) -> None:
    link, _ = env
    c = await TestClient.connect(link.port, PSK)
    await c.ws.send("hello")
    assert await closed_code(c.ws) == 4400


async def test_relay_between_clients(env) -> None:
    link, written = env
    a = await TestClient.connect(link.port, PSK, "phone-a")
    b = await TestClient.connect(link.port, PSK, "phone-b")
    await asyncio.sleep(0.1)
    await a.send(clip("relay me 😀"))
    header, _ = await asyncio.wait_for(b.recv(), 5)
    assert header["data"] == "relay me 😀" and header["ch"] == "clipboard"
    assert written == ["relay me 😀"]
    # echo suppression: same text again is dropped, not relayed
    await a.send(clip("relay me 😀"))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(b.recv(), 0.5)


PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(200))


def image_clip() -> dict:
    return {"ch": "clipboard", "type": "clip", "kind": "image", "mime": "image/png", "ts": 1}


async def test_image_relay_between_clients(tmp_path) -> None:
    link = Link(PSK, tmp_path / "partial", host="127.0.0.1", port=0)
    images: list[bytes] = []
    link.register(ClipboardFeature(link, lambda t: None, images.append, save_dir=tmp_path))
    await link.start()
    try:
        a = await TestClient.connect(link.port, PSK, "phone-a")
        b = await TestClient.connect(link.port, PSK, "phone-b")
        await asyncio.sleep(0.1)
        await a.send(image_clip(), PNG)
        header, body = await asyncio.wait_for(b.recv(), 5)
        assert header["kind"] == "image" and header["mime"] == "image/png" and "data" not in header
        assert body == PNG
        assert images == [PNG]
        assert [p.read_bytes() for p in tmp_path.glob("clip_*.png")] == [PNG]
        # echo drop; bad mime and empty body ignored
        await a.send(image_clip(), PNG)
        await a.send({**image_clip(), "mime": "image/jpeg"}, b"x")
        await a.send(image_clip(), b"")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(b.recv(), 0.5)
    finally:
        await link.stop()


@pytest.fixture
async def stalled(tmp_path) -> AsyncIterator[tuple[Link, TestClient, TestClient]]:
    """Link with phone A reading normally and phone B that stopped reading (vanished mid-send)."""
    link = Link(PSK, tmp_path / "partial", host="127.0.0.1", port=0)
    link.register(ClipboardFeature(link, lambda t: None, lambda png: None, save_dir=tmp_path))
    await link.start()
    a = await TestClient.connect(link.port, PSK, "phone-a")
    b = await TestClient.connect(link.port, PSK, "phone-b")
    await asyncio.sleep(0.1)
    b.ws.transport.pause_reading()
    for _ in range(3):  # more than the loopback socket buffers hold
        await a.send(image_clip(), os.urandom(12 * 1024 * 1024))
    yield link, a, b
    await link.stop()


async def test_relay_to_stalled_peer_does_not_delay_sender(stalled) -> None:
    _, a, _ = stalled
    await a.send({**clip("after images"), "mid": "late"})
    header, _ = await asyncio.wait_for(a.recv(), 5)
    assert header == {"ch": "link", "type": "ack", "mid": "late"}


async def test_stalled_peer_is_dropped(stalled, monkeypatch) -> None:
    from clipbridge.link import peers

    monkeypatch.setattr(peers, "STALL_TIMEOUT", 0.5)
    monkeypatch.setattr(peers, "STALL_POLL", 0.1)
    link, a, _ = stalled
    await a.send(image_clip(), os.urandom(1024))  # a send that starts after the patch
    for _ in range(50):
        if [p.device for p in link.peers] == ["phone-a"]:
            break
        await asyncio.sleep(0.1)
    assert [p.device for p in link.peers] == ["phone-a"]


async def test_send_to_stalled_peer_reports_failure(stalled, monkeypatch) -> None:
    from clipbridge.link import peers

    monkeypatch.setattr(peers, "STALL_TIMEOUT", 0.5)
    monkeypatch.setattr(peers, "STALL_POLL", 0.1)
    link, _, _ = stalled
    b = next(p for p in link.peers if p.device == "phone-b")
    for _ in range(3):  # whatever the socket buffers absorbed, these cannot all fit
        await link.broadcast(image_clip(), os.urandom(12 * 1024 * 1024))
    assert await asyncio.wait_for(link.send(b, clip("lost")), 10) is False


async def test_duplicate_device_replaces_older_4409(env) -> None:
    link, _ = env
    old = await TestClient.connect(link.port, PSK, "same")
    new = await TestClient.connect(link.port, PSK, "same")
    assert await closed_code(old.ws) == 4409
    assert len(link.peers) == 1
    await new.ws.close()


async def test_oversize_preauth_message_closed(env) -> None:
    link, _ = env
    ws = await connect(f"ws://127.0.0.1:{link.port}/", subprotocols=["clipbridge.v1"])
    await ws.recv()
    await ws.send("x" * 5000)
    assert await closed_code(ws) == 1009


async def test_handshake_timeout_4408(env, monkeypatch) -> None:
    link, _ = env
    monkeypatch.setattr("clipbridge.link.server.AUTH_TIMEOUT", 0.3)
    ws = await connect(f"ws://127.0.0.1:{link.port}/", subprotocols=["clipbridge.v1"])
    await ws.recv()
    assert await closed_code(ws) == 4408


async def test_pending_handshake_cap_then_recovers(env, monkeypatch) -> None:
    link, _ = env
    monkeypatch.setattr("clipbridge.link.server.MAX_PENDING_HANDSHAKES", 2)
    url = f"ws://127.0.0.1:{link.port}/"
    held = [await connect(url, subprotocols=["clipbridge.v1"]) for _ in range(2)]
    for ws in held:
        await ws.recv()
    extra = await connect(url, subprotocols=["clipbridge.v1"])
    assert await closed_code(extra) == 1013
    await held[0].close()
    await asyncio.sleep(0.2)
    c = await TestClient.connect(link.port, PSK)  # slot freed
    await c.ws.close()
    await held[1].close()


async def test_full_size_allowed_after_auth(env) -> None:
    link, written = env
    c = await TestClient.connect(link.port, PSK)
    await c.send(clip("y" * 100_000))
    await asyncio.sleep(0.3)
    assert written == ["y" * 100_000]
    await c.ws.close()
