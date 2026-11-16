"""File transfer end to end over loopback, with `tclient.TestClient` playing the phone."""

import asyncio
import hashlib
import logging
import os
import time
from pathlib import Path
from typing import AsyncIterator

import pytest

from clipbridge.features.clipboard.feature import ClipboardFeature
from clipbridge.features.files.feature import FilesFeature, unique_path
from clipbridge.link import transfer
from clipbridge.link.server import Link
from clipbridge.link.transfer_core import CHUNK, CompletedIds, Range, rewind_full
from tclient import PSK, MemoryDevices, TestClient, closed_code, connect_data, fid, offer, recv, sha, stream, until_connected

log = logging.getLogger(__name__)


class Env:
    def __init__(self, tmp: Path) -> None:
        self.partial = tmp / "partial"
        self.dest = tmp / "ClipShare"
        self.clips: list[str] = []
        self.received: list[Path] = []
        self.link = self.make_link()

    def make_link(self) -> Link:
        link = Link(MemoryDevices("phone", "tablet"), self.partial, host="127.0.0.1", port=0)
        link.register(ClipboardFeature(link, self.clips.append))
        self.files = FilesFeature(link, dest_dir=self.dest, notify=self.received.append)
        link.register(self.files)
        return link

    async def phone(self, device: str = "phone") -> TestClient:
        client = await TestClient.connect(self.link.port, PSK, device)
        await until_connected(self.link, device)
        return client

    async def data(self, device: str = "phone") -> TestClient:
        return await connect_data(self.link.port, device)


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    e = Env(tmp_path)
    await e.link.start()
    yield e
    await e.link.stop()


def partials(env: Env) -> list[str]:
    """Partial-transfer files (`completed.json`, the record of finished ids, is not one)."""
    if not env.partial.is_dir():
        return []
    return sorted(p.name for p in env.partial.iterdir() if p.name != "completed.json")


# -- phone -> PC ---------------------------------------------------------------


async def test_receive_new_file(env: Env) -> None:
    body = os.urandom(300_000)
    phone = await env.phone()
    await phone.send(offer(fid(), "photo.jpg", len(body)))
    accept = await recv(phone)
    assert accept == {"ch": "link", "type": "xfer_accept", "id": fid(), "ranges": [[0, len(body), 0]]}
    d = await env.data()
    await d.send({"ch": "link", "type": "xfer_attach", "id": fid(), "start": 0})
    await stream(d, body)
    await d.send({"ch": "link", "type": "xfer_end", "sha256": sha(body)})
    assert await recv(phone) == {"ch": "link", "type": "xfer_done", "id": fid(), "ok": True}
    target = env.dest / "photo.jpg"
    assert target.read_bytes() == body
    assert env.received == [target]
    assert partials(env) == []


async def test_resume_from_have_after_reconnect(env: Env) -> None:
    body = os.urandom(1_000_000)
    phone = await env.phone()
    await phone.send(offer(fid(), "big.bin", len(body)))
    await recv(phone)
    d = await env.data()
    await d.send({"ch": "link", "type": "xfer_attach", "id": fid(), "start": 0})
    await stream(d, body[:600_000])
    await asyncio.sleep(0.3)
    await phone.ws.close()  # control lost: server closes the data connection too
    assert await closed_code(d) == 1000

    phone = await env.phone()
    await phone.send(offer(fid(), "big.bin", len(body)))
    accept = await recv(phone)
    [[start, end, have]] = accept["ranges"]
    assert (start, end) == (0, len(body)) and 0 < have <= 600_000
    d = await env.data()
    await d.send({"ch": "link", "type": "xfer_attach", "id": fid(), "start": 0})
    await stream(d, body[have:])
    await d.send({"ch": "link", "type": "xfer_end", "sha256": sha(body)})
    assert (await recv(phone))["ok"] is True
    assert (env.dest / "big.bin").read_bytes() == body


async def test_resume_after_server_restart(env: Env) -> None:
    body = os.urandom(500_000)
    phone = await env.phone()
    await phone.send(offer(fid(), "r.bin", len(body)))
    await recv(phone)
    d = await env.data()
    await d.send({"ch": "link", "type": "xfer_attach", "id": fid(), "start": 0})
    await stream(d, body[:200_000])
    await asyncio.sleep(0.3)
    await env.link.stop()
    env.link = env.make_link()  # fresh process state; only the partial directory survives
    await env.link.start()

    phone = await env.phone()
    await phone.send(offer(fid(), "r.bin", len(body)))
    [[_, _, have]] = (await recv(phone))["ranges"]
    assert have == 200_000
    d = await env.data()
    await d.send({"ch": "link", "type": "xfer_attach", "id": fid(), "start": 0})
    await stream(d, body[have:])
    await d.send({"ch": "link", "type": "xfer_end", "sha256": sha(body)})
    assert (await recv(phone))["ok"] is True
    assert (env.dest / "r.bin").read_bytes() == body


async def test_hash_mismatch_reports_failure_and_discards(env: Env) -> None:
    body = os.urandom(10_000)
    phone = await env.phone()
    await phone.send(offer(fid(), "x.bin", len(body)))
    await recv(phone)
    d = await env.data()
    await d.send({"ch": "link", "type": "xfer_attach", "id": fid(), "start": 0})
    await stream(d, body)
    await d.send({"ch": "link", "type": "xfer_end", "sha256": sha(b"other")})
    assert await recv(phone) == {"ch": "link", "type": "xfer_done", "id": fid(), "ok": False}
    assert not (env.dest / "x.bin").exists()
    assert partials(env) == []
    # a fresh offer under a new id starts from zero
    await phone.send(offer(fid(2), "x.bin", len(body)))
    assert (await recv(phone))["ranges"] == [[0, len(body), 0]]


async def test_name_clash_gets_suffix(env: Env) -> None:
    env.dest.mkdir(parents=True)
    (env.dest / "a.txt").write_bytes(b"old")
    (env.dest / "a (1).txt").write_bytes(b"old")
    phone = await env.phone()
    await phone.send(offer(fid(), "a.txt", 3))
    await recv(phone)
    d = await env.data()
    await d.send({"ch": "link", "type": "xfer_attach", "id": fid(), "start": 0})
    await d.send({"ch": "link", "type": "xfer_data"}, b"new")
    await d.send({"ch": "link", "type": "xfer_end", "sha256": sha(b"new")})
    assert (await recv(phone))["ok"] is True
    assert (env.dest / "a (2).txt").read_bytes() == b"new"
    assert (env.dest / "a.txt").read_bytes() == b"old"


def test_unique_path(tmp_path: Path) -> None:
    assert unique_path(tmp_path, "n.tar.gz") == tmp_path / "n.tar.gz"
    (tmp_path / "n.tar.gz").write_bytes(b"")
    assert unique_path(tmp_path, "n.tar.gz") == tmp_path / "n.tar (1).gz"
    (tmp_path / "noext").write_bytes(b"")
    assert unique_path(tmp_path, "noext") == tmp_path / "noext (1)"


async def test_empty_file(env: Env) -> None:
    phone = await env.phone()
    await phone.send(offer(fid(), "empty.txt", 0))
    assert (await recv(phone))["ranges"] == []
    assert await recv(phone) == {"ch": "link", "type": "xfer_done", "id": fid(), "ok": True}
    assert (env.dest / "empty.txt").read_bytes() == b""
    assert env.received == [env.dest / "empty.txt"]


async def test_unknown_feature_rejected(env: Env) -> None:
    phone = await env.phone()
    await phone.send({**offer(fid(), "a", 1), "feature": "nope"})
    assert await recv(phone) == {"ch": "link", "type": "xfer_reject", "id": fid()}


async def test_attach_unknown_id_4404(env: Env) -> None:
    d = await env.data()
    await d.send({"ch": "link", "type": "xfer_attach", "id": fid(9), "start": 0})
    assert await closed_code(d) == 4404


async def test_attach_unknown_start_4404(env: Env) -> None:
    phone = await env.phone()
    await phone.send(offer(fid(), "a", 10))
    await recv(phone)
    d = await env.data()
    await d.send({"ch": "link", "type": "xfer_attach", "id": fid(), "start": 5})
    assert await closed_code(d) == 4404


async def test_bytes_past_end_4400(env: Env) -> None:
    phone = await env.phone()
    await phone.send(offer(fid(), "a", 10))
    await recv(phone)
    d = await env.data()
    await d.send({"ch": "link", "type": "xfer_attach", "id": fid(), "start": 0})
    await d.send({"ch": "link", "type": "xfer_data"}, b"x" * 11)
    assert await closed_code(d) == 4400


async def test_data_connection_is_not_a_peer(env: Env) -> None:
    phone = await env.phone()
    await env.data()
    await asyncio.sleep(0.1)
    assert [p.device for p in env.link.peers] == ["phone"]
    await phone.send({"ch": "clipboard", "type": "clip", "kind": "text", "data": "still here", "ts": 1})
    await asyncio.sleep(0.1)
    assert env.clips == ["still here"]  # no 4409 for the control connection


async def test_cancel_by_sender_deletes_partial(env: Env) -> None:
    phone = await env.phone()
    await phone.send(offer(fid(), "c.bin", 100))
    await recv(phone)
    assert partials(env)
    await phone.send({"ch": "link", "type": "xfer_cancel", "id": fid()})
    await asyncio.sleep(0.2)
    assert partials(env) == []


async def test_reoffer_of_completed_id_is_confirmed_not_received_again(env: Env) -> None:
    body = os.urandom(50_000)
    phone = await env.phone()
    await phone.send(offer(fid(), "once.bin", len(body)))
    await recv(phone)
    d = await env.data()
    await d.send({"ch": "link", "type": "xfer_attach", "id": fid(), "start": 0})
    await stream(d, body)
    await d.send({"ch": "link", "type": "xfer_end", "sha256": sha(body)})
    await recv(phone)  # xfer_done ok:true, which the phone never saw: the link dropped first
    await phone.ws.close()

    phone = await env.phone()
    await phone.send(offer(fid(), "once.bin", len(body)))
    assert await recv(phone) == {"ch": "link", "type": "xfer_accept", "id": fid(), "ranges": [[0, len(body), len(body)]]}
    assert await recv(phone) == {"ch": "link", "type": "xfer_done", "id": fid(), "ok": True}
    assert [p.name for p in env.dest.iterdir()] == ["once.bin"]
    assert env.received == [env.dest / "once.bin"]
    assert partials(env) == []
    # another device, or another size, under that id is a new transfer
    other = await env.phone("tablet")
    await other.send(offer(fid(), "once.bin", len(body)))
    assert (await recv(other))["ranges"] == [[0, len(body), 0]]


async def test_completed_id_survives_server_restart(env: Env) -> None:
    body = os.urandom(20_000)
    phone = await env.phone()
    await phone.send(offer(fid(), "kept.bin", len(body)))
    await recv(phone)
    d = await env.data()
    await d.send({"ch": "link", "type": "xfer_attach", "id": fid(), "start": 0})
    await stream(d, body)
    await d.send({"ch": "link", "type": "xfer_end", "sha256": sha(body)})
    await recv(phone)  # xfer_done ok:true, lost
    await env.link.stop()
    env.link = env.make_link()
    await env.link.start()

    phone = await env.phone()
    await phone.send(offer(fid(), "kept.bin", len(body)))
    assert (await recv(phone))["ranges"] == [[0, len(body), len(body)]]
    assert (await recv(phone))["ok"] is True
    assert [p.name for p in env.dest.iterdir()] == ["kept.bin"]


async def test_resume_of_fully_written_range_still_verifies(env: Env) -> None:
    body = os.urandom(30_000)
    phone = await env.phone()
    await phone.send(offer(fid(), "full.bin", len(body)))
    await recv(phone)
    d = await env.data()
    await d.send({"ch": "link", "type": "xfer_attach", "id": fid(), "start": 0})
    await stream(d, body)  # every byte written, but no xfer_end yet
    await asyncio.sleep(0.3)
    await phone.ws.close()
    assert await closed_code(d) == 1000

    phone = await env.phone()
    await phone.send(offer(fid(), "full.bin", len(body)))
    assert (await recv(phone))["ranges"] == [[0, len(body), len(body) - 1]]  # unverified: one byte short
    d = await env.data()
    await d.send({"ch": "link", "type": "xfer_attach", "id": fid(), "start": 0})
    await stream(d, body[-1:])
    await d.send({"ch": "link", "type": "xfer_end", "sha256": sha(body)})
    assert (await recv(phone))["ok"] is True
    assert (env.dest / "full.bin").read_bytes() == body


def test_completed_ids_round_trip_and_rewind() -> None:
    ids = CompletedIds()
    ids.add(fid(1), "phone", 5, 1000.0)
    again = CompletedIds.from_json(ids.to_json() + [["bad"], [fid(2), "x", -1.5, 1]], 1001.0)
    assert again.has(fid(1), "phone", 5, 1001.0) and len(again) == 1
    assert len(CompletedIds.from_json(ids.to_json(), 1000.0 + 25 * 3600)) == 0  # expired on load
    ranges = [Range(0, 10, 10), Range(10, 20, 4)]
    rewind_full(ranges)
    assert [r.have for r in ranges] == [9, 4]


def test_completed_ids_bounded_by_count_and_age() -> None:
    ids = CompletedIds(max_items=2, max_age=100.0)
    for n in (1, 2, 3):
        ids.add(fid(n), "phone", 10, float(n))
    assert len(ids) == 2 and not ids.has(fid(1), "phone", 10, 3.0)
    assert ids.has(fid(3), "phone", 10, 3.0)
    assert not ids.has(fid(3), "tablet", 10, 3.0) and not ids.has(fid(3), "phone", 11, 3.0)
    assert not ids.has(fid(3), "phone", 10, 104.0)  # older than max_age
    assert len(ids) == 0


async def test_write_error_cancels_and_discards(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    def disk_full(*_: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(transfer, "_write_at", disk_full)
    phone = await env.phone()
    await phone.send(offer(fid(), "w.bin", 10_000))
    await recv(phone)
    d = await env.data()
    await d.send({"ch": "link", "type": "xfer_attach", "id": fid(), "start": 0})
    await d.send({"ch": "link", "type": "xfer_data"}, os.urandom(10_000))
    assert await recv(phone) == {"ch": "link", "type": "xfer_cancel", "id": fid()}  # before the close
    assert await closed_code(d) == 1000
    await asyncio.sleep(0.2)
    assert partials(env) == []


# -- PC -> phone ---------------------------------------------------------------


async def phone_receive(env: Env, phone: TestClient, *, have: int = 0, ok: bool = True) -> tuple[str, bytes]:
    """Play the receiving phone for one offer: accept, attach, collect, check, report done."""
    o = await recv(phone)
    assert o["type"] == "xfer_offer" and o["feature"] == "files"
    await phone.send({"ch": "link", "type": "xfer_accept", "id": o["id"], "ranges": [[0, o["size"], have]] if o["size"] else []})
    if o["size"] == 0:
        await phone.send({"ch": "link", "type": "xfer_done", "id": o["id"], "ok": ok})
        return o["id"], b""
    d = await env.data()
    await d.send({"ch": "link", "type": "xfer_attach", "id": o["id"], "start": 0})
    parts: list[bytes] = []
    while True:
        header, body = await asyncio.wait_for(d.recv(), 30)
        if header["type"] == "xfer_end":
            break
        assert header["type"] == "xfer_data" and 1 <= len(body) <= CHUNK
        parts.append(body)
    got = b"".join(parts)
    assert len(got) == o["size"] - have
    await phone.send({"ch": "link", "type": "xfer_done", "id": o["id"], "ok": ok})
    return header["sha256"], got


async def test_send_file_to_phone(env: Env, tmp_path: Path) -> None:
    src = tmp_path / "doc.pdf"
    src.write_bytes(os.urandom(9 * 1024 * 1024 + 7))  # spans several 4 MiB frames
    phone = await env.phone()
    result = asyncio.ensure_future(env.files.send(src))
    digest, got = await phone_receive(env, phone)
    assert got == src.read_bytes() and digest == sha(got)
    assert await asyncio.wait_for(result, 5) is True


async def test_send_file_resumes_with_have_and_retries_once(env: Env, tmp_path: Path) -> None:
    src = tmp_path / "v.bin"
    data = os.urandom(100_000)
    src.write_bytes(data)
    phone = await env.phone()
    result = asyncio.ensure_future(env.files.send(src))
    digest, got = await phone_receive(env, phone, have=40_000, ok=False)
    assert got == data[40_000:] and digest == sha(data)  # hash covers the have prefix
    digest, got = await phone_receive(env, phone, ok=True)  # retry under a new id
    assert got == data
    assert await asyncio.wait_for(result, 5) is True


async def test_send_file_reoffered_after_reconnect(env: Env, tmp_path: Path) -> None:
    src = tmp_path / "q.bin"
    src.write_bytes(b"q" * 1000)
    phone = await env.phone()
    result = asyncio.ensure_future(env.files.send(src))
    first = await recv(phone)
    await phone.ws.close()
    phone = await env.phone()
    again = await recv(phone)
    assert again["id"] == first["id"]
    await phone.send({"ch": "link", "type": "xfer_reject", "id": again["id"]})
    assert await asyncio.wait_for(result, 5) is False


async def test_send_empty_file(env: Env, tmp_path: Path) -> None:
    src = tmp_path / "e.txt"
    src.write_bytes(b"")
    phone = await env.phone()
    result = asyncio.ensure_future(env.files.send(src))
    await phone_receive(env, phone)
    assert await asyncio.wait_for(result, 5) is True


async def test_send_file_gives_up_when_receiver_stalls(env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transfer, "STALL_TIMEOUT", 0.5)
    src = tmp_path / "s.bin"
    src.write_bytes(b"s" * 1000)
    phone = await env.phone()
    result = asyncio.ensure_future(env.files.send(src))
    o = await recv(phone)  # never answered
    assert await recv(phone) == {"ch": "link", "type": "xfer_cancel", "id": o["id"]}
    assert await asyncio.wait_for(result, 5) is False


async def test_send_file_slow_prefix_hash_is_not_a_stall(env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transfer, "STALL_TIMEOUT", 0.3)
    real = transfer._hash_region

    def slow(f, start, length, update):  # type: ignore[no-untyped-def]
        for _ in range(8):  # 0.8 s of hashing, progress every 0.1 s
            time.sleep(0.1)
            update(b"")
        real(f, start, length, update)

    monkeypatch.setattr(transfer, "_hash_region", slow)
    src = tmp_path / "h.bin"
    data = os.urandom(50_000)
    src.write_bytes(data)
    phone = await env.phone()
    result = asyncio.ensure_future(env.files.send(src))
    digest, got = await phone_receive(env, phone, have=40_000)
    assert got == data[40_000:] and digest == sha(data)
    assert await asyncio.wait_for(result, 5) is True


async def test_send_file_unreadable_source_cancels(env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def locked(*_: object) -> bytes:
        raise PermissionError(13, "file is locked")

    monkeypatch.setattr(transfer, "_read_hashed", locked)
    src = tmp_path / "u.bin"
    src.write_bytes(b"u" * 1000)
    phone = await env.phone()
    result = asyncio.ensure_future(env.files.send(src))
    o = await recv(phone)
    await phone.send({"ch": "link", "type": "xfer_accept", "id": o["id"], "ranges": [[0, 1000, 0]]})
    d = await env.data()
    await d.send({"ch": "link", "type": "xfer_attach", "id": o["id"], "start": 0})
    assert await closed_code(d) == 1000
    assert await recv(phone) == {"ch": "link", "type": "xfer_cancel", "id": o["id"]}
    assert await asyncio.wait_for(result, 5) is False


# -- throughput with the clipboard still flowing --------------------------------


async def clip_round_trips(phone: TestClient, stop: asyncio.Event) -> list[float]:
    """Send clips with receipts until stopped; return each round-trip time."""
    rtts: list[float] = []
    n = 0
    while not stop.is_set():
        n += 1
        t0 = time.monotonic()
        await phone.send({"ch": "clipboard", "type": "clip", "kind": "text", "data": f"clip {n}", "ts": 1, "mid": f"m{n}"})
        while True:
            header, _ = await asyncio.wait_for(phone.recv(), 5)
            if header.get("mid") == f"m{n}":
                break
        rtts.append(time.monotonic() - t0)
        await asyncio.sleep(0.05)
    return rtts


async def test_200mb_receive_while_clipboard_flows(env: Env) -> None:
    size = 200 * 1024 * 1024
    block = os.urandom(CHUNK)
    whole = hashlib.sha256()
    for _ in range(size // CHUNK):
        whole.update(block)
    phone = await env.phone()
    await phone.send(offer(fid(), "big.bin", size))
    await recv(phone)
    stop = asyncio.Event()
    clips = asyncio.ensure_future(clip_round_trips(phone, stop))
    d = await env.data()
    t0 = time.monotonic()
    await d.send({"ch": "link", "type": "xfer_attach", "id": fid(), "start": 0})
    for _ in range(size // CHUNK):
        await d.send({"ch": "link", "type": "xfer_data"}, block)
    await d.send({"ch": "link", "type": "xfer_end", "sha256": whole.hexdigest()})
    while not clips.done():
        await asyncio.sleep(0.01)
        if (env.dest / "big.bin").exists():
            break
    elapsed = time.monotonic() - t0
    stop.set()
    rtts = await clips
    print(f"\n200 MB phone->PC: {size / 1e6 / elapsed:.1f} MB/s; {len(rtts)} clips, max rtt {max(rtts) * 1000:.0f} ms")
    assert (env.dest / "big.bin").stat().st_size == size
    assert len(rtts) >= 3 and max(rtts) < 1.0
    assert env.clips[-1] == f"clip {len(rtts)}"


async def test_200mb_send_while_clipboard_flows(env: Env, tmp_path: Path) -> None:
    src = tmp_path / "big.bin"
    block = os.urandom(1024 * 1024)
    with open(src, "wb") as f:
        for _ in range(200):
            f.write(block)
    phone = await env.phone()
    stop = asyncio.Event()
    result = asyncio.ensure_future(env.files.send(src))
    o = await recv(phone)
    clips = asyncio.ensure_future(clip_round_trips(phone, stop))
    await phone.send({"ch": "link", "type": "xfer_accept", "id": o["id"], "ranges": [[0, o["size"], 0]]})
    d = await env.data()
    t0 = time.monotonic()
    await d.send({"ch": "link", "type": "xfer_attach", "id": o["id"], "start": 0})
    h, n = hashlib.sha256(), 0
    while True:
        header, body = await asyncio.wait_for(d.recv(), 30)
        if header["type"] == "xfer_end":
            break
        h.update(body)
        n += len(body)
    elapsed = time.monotonic() - t0
    stop.set()
    rtts = await clips
    assert n == o["size"] and header["sha256"] == h.hexdigest()
    await phone.send({"ch": "link", "type": "xfer_done", "id": o["id"], "ok": True})
    assert await asyncio.wait_for(result, 5) is True
    print(f"\n200 MB PC->phone: {n / 1e6 / elapsed:.1f} MB/s; {len(rtts)} clips, max rtt {max(rtts) * 1000:.0f} ms")
    assert len(rtts) >= 3 and max(rtts) < 1.0
