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
from clipbridge.link.requests import Answer, Reply
from clipbridge.link.server import Link
from tclient import PSK, MemoryDevices, TestClient, until_connected

DEVICES = ("dev-1", "phone-a", "phone-b", "phone-c", "same")


def clip(text: str) -> dict:
    return {"ch": "clipboard", "type": "clip", "kind": "text", "data": text, "ts": 1}


@pytest.fixture
def devices() -> MemoryDevices:
    return MemoryDevices(*DEVICES)


@pytest.fixture
async def env(tmp_path, devices) -> AsyncIterator[tuple[Link, list[str]]]:
    link = Link(devices, tmp_path / "partial", host="127.0.0.1", port=0, name="Test PC")
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


async def test_reply_sent_after_feature_handles(env) -> None:
    link, written = env
    c = await TestClient.connect(link.port, PSK)
    await c.send({**clip("a"), "mid": "m1"})
    header, body = await asyncio.wait_for(c.recv(), 5)
    assert header == {"ch": "link", "type": "reply", "mid": "m1", "ok": True} and body == b""
    assert written == ["a"]
    await c.send({**clip("a"), "mid": "m2"})  # duplicate clip: ignored on purpose, still handled
    header, _ = await asyncio.wait_for(c.recv(), 5)
    assert header == {"ch": "link", "type": "reply", "mid": "m2", "ok": True}
    await c.ws.close()


async def test_invalid_mid_dropped_and_no_reply_for_link_messages(env) -> None:
    link, written = env
    c = await TestClient.connect(link.port, PSK)
    await c.send({**clip("a"), "mid": "bad mid!"})
    await c.send({**clip("a2"), "mid": 7})
    await c.send({"ch": "link", "type": "nope", "mid": "m2"})
    await c.send({"ch": "linkx", "mid": "m3"})
    await c.send({"ch": "link", "type": "reply", "mid": "unknown", "ok": True})
    await c.send({**clip("b"), "mid": "ok"})
    header, _ = await asyncio.wait_for(c.recv(), 5)
    assert header["mid"] == "ok"
    assert written == ["b"]  # a message with an invalid mid never reaches the feature
    await c.ws.close()


class Boom:
    name = "boom"
    version = 3

    async def on_peer_connected(self, peer) -> None: ...

    async def on_peer_disconnected(self, peer) -> None: ...

    async def on_message(self, peer, header, body):
        if header.get("type") == "fail":
            raise RuntimeError("feature bug")
        if header.get("type") == "big":
            return Answer(body=b"x" * (16 * 1024 * 1024))
        return Answer({"echo": header.get("type")}, body[::-1])


async def test_reply_ok_false_on_unknown_ch_feature_error_or_oversize(env) -> None:
    link, _ = env
    link.register(Boom())
    c = await TestClient.connect(link.port, PSK, features={"boom": 1})
    assert c.server_hello["features"] == {"clipboard": 1, "boom": 3}
    for n, header in enumerate(({"ch": "nope"}, {"ch": "boom", "type": "fail"}, {"ch": "boom", "type": "big"})):
        await c.send({**header, "mid": f"m{n}"})
        reply, body = await asyncio.wait_for(c.recv(), 5)
        assert reply == {"ch": "link", "type": "reply", "mid": f"m{n}", "ok": False} and body == b""
    await c.send({"ch": "boom", "type": "hi", "mid": "m9"}, b"abc")
    reply, body = await asyncio.wait_for(c.recv(), 5)
    assert reply == {"ch": "link", "type": "reply", "mid": "m9", "ok": True, "data": {"echo": "hi"}}
    assert body == b"cba"
    await c.ws.close()


async def test_request_gets_reply(env) -> None:
    link, _ = env
    c = await TestClient.connect(link.port, PSK)
    await asyncio.sleep(0.1)
    result = asyncio.ensure_future(link.request(link.peers[0], clip("q")))
    header, _ = await asyncio.wait_for(c.recv(), 5)
    assert header["data"] == "q" and isinstance(header["mid"], str)
    await c.send({"ch": "link", "type": "reply", "mid": "other", "ok": True})  # unknown mid: ignored
    await c.send({"ch": "link", "type": "reply", "mid": header["mid"], "ok": True, "data": {"n": 1}}, b"zz")
    assert await asyncio.wait_for(result, 5) == Reply(True, {"n": 1}, b"zz")
    await c.ws.close()


async def test_request_timeout_and_close_return_none(env, monkeypatch) -> None:
    link, _ = env
    monkeypatch.setattr("clipbridge.link.requests.REQUEST_TIMEOUT", 0.3)
    c = await TestClient.connect(link.port, PSK)
    await asyncio.sleep(0.1)
    peer = link.peers[0]
    assert await link.request(peer, clip("unanswered")) is None
    monkeypatch.setattr("clipbridge.link.requests.REQUEST_TIMEOUT", 10.0)
    pending = asyncio.ensure_future(link.request(peer, clip("closing")))
    await asyncio.wait_for(c.recv(), 5)
    await asyncio.wait_for(c.recv(), 5)
    await c.ws.close()
    assert await asyncio.wait_for(pending, 5) is None


async def test_wrong_psk_4401(env) -> None:
    link, _ = env
    ws = await connect(f"ws://127.0.0.1:{link.port}/", subprotocols=["clipbridge.v1"])
    sn = b64d(json.loads(await ws.recv())["sn"], 16)
    cn = os.urandom(16)
    bad = crypto.client_mac(b"\x01" * 32, sn, cn, "dev-1")
    await ws.send(json.dumps({"type": "auth", "v": 1, "cn": b64e(cn), "device": "dev-1", "mac": b64e(bad)}))
    assert await closed_code(ws) == 4401


async def test_unknown_device_4401(env) -> None:
    link, _ = env
    ws = await connect(f"ws://127.0.0.1:{link.port}/", subprotocols=["clipbridge.v1"])
    sn = b64d(json.loads(await ws.recv())["sn"], 16)
    cn = os.urandom(16)
    mac = crypto.client_mac(PSK, sn, cn, "stranger")
    await ws.send(json.dumps({"type": "auth", "v": 1, "cn": b64e(cn), "device": "stranger", "mac": b64e(mac)}))
    assert await closed_code(ws) == 4401
    assert ws.protocol.close_rcvd.reason == ""


async def test_per_device_keys(env, devices) -> None:
    link, written = env
    devices.keys["phone-b"] = b"\x02" * 32
    with pytest.raises(ConnectionClosed):  # phone-b's key is not phone-a's
        await TestClient.connect(link.port, PSK, "phone-b")
    b = await TestClient.connect(link.port, b"\x02" * 32, "phone-b")
    await b.send(clip("own key"))
    await asyncio.sleep(0.2)
    assert written == ["own key"]
    await b.ws.close()


async def test_hello_exchange(env, devices) -> None:
    link, _ = env
    c = await TestClient.connect(link.port, PSK, features={"clipboard": 2, "x.y-z": 7})
    assert c.server_hello == {"ch": "link", "type": "hello", "name": "Test PC", "features": {"clipboard": 1}}
    await asyncio.sleep(0.1)
    (peer,) = link.peers
    assert peer.name == "Test phone" and devices.names == {"dev-1": "Test phone"}
    assert peer.supports("clipboard") and peer.supports("clipboard", 2) and not peer.supports("clipboard", 3)
    assert peer.supports("x.y-z", 7) and not peer.supports("files")
    await c.ws.close()


async def test_peer_not_connected_before_hello(env) -> None:
    link, written = env
    c = await TestClient.connect(link.port, PSK, hello=False)
    await asyncio.sleep(0.1)
    assert link.peers == []
    await c.send_hello({"clipboard": 1})
    await asyncio.sleep(0.1)
    assert [p.device for p in link.peers] == ["dev-1"]
    await c.ws.close()


@pytest.mark.parametrize(
    "first",
    [
        clip("no hello"),
        {"ch": "link", "type": "hello", "features": {"clipboard": 1}},
        {"ch": "link", "type": "hello", "name": "", "features": {}},
        {"ch": "link", "type": "hello", "name": "a\nb", "features": {}},
        {"ch": "link", "type": "hello", "name": "x" * 65, "features": {}},
        {"ch": "link", "type": "hello", "name": "p", "features": []},
        {"ch": "link", "type": "hello", "name": "p", "features": {"clipboard": 0}},
        {"ch": "link", "type": "hello", "name": "p", "features": {"clipboard": 2**31}},
        {"ch": "link", "type": "hello", "name": "p", "features": {"clipboard": True}},
        {"ch": "link", "type": "hello", "name": "p", "features": {"linkx": 1}},
        {"ch": "link", "type": "hello", "name": "p", "features": {"bad name": 1}},
        {"ch": "link", "type": "hello", "name": "p", "features": {f"f{i}": 1 for i in range(65)}},
    ],
)
async def test_missing_or_invalid_hello_4400(env, first) -> None:
    link, written = env
    c = await TestClient.connect(link.port, PSK, hello=False)
    await c.send(first)
    assert await closed_code(c.ws) == 4400
    assert written == [] and link.peers == []


async def test_hello_with_body_4400(env) -> None:
    link, _ = env
    c = await TestClient.connect(link.port, PSK, hello=False)
    await c.send({"ch": "link", "type": "hello", "name": "p", "features": {"clipboard": 1}}, b"x")
    assert await closed_code(c.ws) == 4400
    assert link.peers == []


async def test_hello_timeout_4408(env, monkeypatch) -> None:
    link, _ = env
    monkeypatch.setattr("clipbridge.link.server.HELLO_TIMEOUT", 0.3)
    c = await TestClient.connect(link.port, PSK, hello=False)
    assert await closed_code(c.ws) == 4408
    assert link.peers == []


async def test_older_connection_evicted_only_after_valid_hello(env) -> None:
    link, written = env
    old = await TestClient.connect(link.port, PSK, "same")
    await until_connected(link, "same")
    bad = await TestClient.connect(link.port, PSK, "same", hello=False)
    await bad.send(clip("not a hello"))
    assert await closed_code(bad.ws) == 4400
    await old.send(clip("old still works"))
    await asyncio.sleep(0.2)
    assert written == ["old still works"] and [p.device for p in link.peers] == ["same"]
    pending = await TestClient.connect(link.port, PSK, "same", hello=False)
    with pytest.raises(asyncio.TimeoutError):  # no hello yet: old is not replaced
        await asyncio.wait_for(old.recv(), 0.3)
    await pending.send_hello({"clipboard": 1})
    assert await closed_code(old.ws) == 4409
    await pending.ws.close()


async def test_unpair_closes_connection_waiting_for_hello_4401(env, devices) -> None:
    link, _ = env
    c = await TestClient.connect(link.port, PSK, hello=False)
    del devices.keys["dev-1"]
    await link.revoke("dev-1")
    assert await closed_code(c.ws) == 4401


class UnpairedDuringHandshake(MemoryDevices):
    """Hands out the key once (the auth check), then the device is gone (unpaired meanwhile)."""

    def psk(self, device: str) -> bytes | None:
        return self.keys.pop(device, None)


@pytest.mark.parametrize("subprotocol", ["clipbridge.v1", "clipbridge.data.v1"])
async def test_unpaired_during_handshake_4401(tmp_path, subprotocol) -> None:
    link = Link(UnpairedDuringHandshake("dev-1"), tmp_path / "partial", host="127.0.0.1", port=0)
    await link.start()
    try:
        ws = await connect(f"ws://127.0.0.1:{link.port}/", subprotocols=[subprotocol])
        sn = b64d(json.loads(await ws.recv())["sn"], 16)
        cn = os.urandom(16)
        mac = crypto.client_mac(PSK, sn, cn, "dev-1")
        await ws.send(json.dumps({"type": "auth", "v": 1, "cn": b64e(cn), "device": "dev-1", "mac": b64e(mac)}))
        assert json.loads(await ws.recv())["type"] == "auth_ok"
        assert await closed_code(ws) == 4401
        assert link.peers == []
    finally:
        await link.stop()


async def test_second_hello_4400(env) -> None:
    link, _ = env
    c = await TestClient.connect(link.port, PSK)
    await c.send_hello({"clipboard": 1})
    assert await closed_code(c.ws) == 4400


async def test_unpair_closes_control_and_data_4401(env, devices) -> None:
    link, _ = env
    c = await TestClient.connect(link.port, PSK)
    d = await TestClient.connect(link.port, PSK, subprotocol="clipbridge.data.v1")
    await asyncio.sleep(0.1)
    del devices.keys["dev-1"]
    await link.revoke("dev-1")
    assert await closed_code(c.ws) == 4401
    assert await closed_code(d.ws) == 4401
    with pytest.raises(ConnectionClosed):
        await TestClient.connect(link.port, PSK)


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


async def test_relay_skips_peer_without_feature(env) -> None:
    link, written = env
    a = await TestClient.connect(link.port, PSK, "phone-a")
    b = await TestClient.connect(link.port, PSK, "phone-b", features={"files": 1})
    c = await TestClient.connect(link.port, PSK, "phone-c")
    await asyncio.sleep(0.1)
    await a.send(clip("only c"))
    header, _ = await asyncio.wait_for(c.recv(), 5)
    assert header["data"] == "only c"
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(b.recv(), 0.5)


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
    link = Link(MemoryDevices(*DEVICES), tmp_path / "partial", host="127.0.0.1", port=0)
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
    link = Link(MemoryDevices(*DEVICES), tmp_path / "partial", host="127.0.0.1", port=0)
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
    assert header == {"ch": "link", "type": "reply", "mid": "late", "ok": True}


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
    await until_connected(link, "same")
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
