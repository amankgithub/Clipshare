"""Loopback runner for the PLAN.md scenario list (step 13): real server, `tclient` as the phone.

Each scenario is one function in SCENARIOS, numbered as in PLAN.md. Pass = every received file's SHA-256
equals the source, it sits in the expected folder under the expected name, nothing partial is left behind,
and a clip sent mid-transfer arrives in < 1 s while the transfer still completes intact.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

import pytest

from clipbridge.features.clipboard.feature import ClipboardFeature
from clipbridge.features.files.feature import FilesFeature
from clipbridge.link.server import Link
from clipbridge.link.transfer_core import CHUNK
from tclient import PSK, MemoryDevices, TestClient, connect_data, fid, offer, sha, until_connected

KB, MB = 1024, 1024 * 1024
BIG = 64 * MB  # "1 GB" in PLAN.md, scaled for the loopback suite
MID = 24 * MB  # "200 MB" / big-but-not-the-biggest, scaled
CLIP_LIMIT = 1.0  # seconds


@dataclass
class Blob:
    name: str
    data: bytes

    @classmethod
    def random(cls, name: str, size: int) -> "Blob":
        return cls(name, os.urandom(size))

    @property
    def digest(self) -> str:
        return sha(self.data)


class Gate:
    """Holds one named transfer once `at` bytes have moved, so a scenario can act mid-transfer."""

    def __init__(self, name: str, at: int) -> None:
        self.name, self.at = name, at
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def hit(self, name: str, moved: int) -> None:
        if name == self.name and moved >= self.at and not self.reached.is_set():
            self.reached.set()
            await self.release.wait()


class Server:
    """The PC side: link + clipboard + files features, temp dirs, clips recorded as they are written."""

    def __init__(self, tmp: Path) -> None:
        self.partial = tmp / "partial"
        self.dest = tmp / "ClipShare"
        self.written: dict[str, float] = {}  # clip text -> time it reached the PC clipboard
        self.link = Link(MemoryDevices("phone"), self.partial, host="127.0.0.1", port=0)
        self.clipboard = ClipboardFeature(self.link, lambda t: self.written.setdefault(t, time.monotonic()))
        self.files = FilesFeature(self.link, dest_dir=self.dest)
        self.link.register(self.clipboard)
        self.link.register(self.files)

    async def clip_written(self, text: str, timeout: float = 5) -> float:
        deadline = time.monotonic() + timeout
        while text not in self.written:
            assert time.monotonic() < deadline, f"clip {text!r} never reached the PC"
            await asyncio.sleep(0.005)
        return self.written[text]

    def leftovers(self) -> list[str]:
        """Anything partial or pending on the PC (`completed.json` is the record of finished ids, not one)."""
        found = [p.name for p in self.partial.iterdir() if p.name != "completed.json"] if self.partial.is_dir() else []
        if self.dest.is_dir():
            found += [p.name for p in self.dest.iterdir() if p.suffix in (".part", ".json", ".tmp")]
        return sorted(found)


class Phone:
    """tclient playing the phone: pushes files to the PC, receives files from the PC, collects clips."""

    def __init__(self, server: Server, folder: Path) -> None:
        self.server = server
        self.folder = folder  # where received files land
        self.gate: Gate | None = None
        self.progress: dict[str, int] = {}  # file name -> bytes moved so far, either direction
        self.read_delay = 0.0  # per-chunk pause on the receiving side only (bytes keep flowing)
        self.last_have = 0  # `have` the PC reported for the latest push
        self.inbox: asyncio.Queue[tuple[dict[str, Any], float]] = asyncio.Queue()  # clips etc., with arrival time
        self._replies: dict[tuple[str, str], asyncio.Queue[dict[str, Any]]] = {}
        self._tasks: list[asyncio.Future[None]] = []
        self.client: TestClient

    async def start(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self.client = await TestClient.connect(self.server.link.port, PSK, "phone")
        await until_connected(self.server.link, "phone")
        self._tasks.append(asyncio.ensure_future(self._read()))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.client.ws.close()

    def _queue(self, kind: str, id_: str) -> asyncio.Queue[dict[str, Any]]:
        return self._replies.setdefault((kind, id_), asyncio.Queue())

    async def _reply(self, kind: str, id_: str, timeout: float = 20) -> dict[str, Any]:
        return await asyncio.wait_for(self._queue(kind, id_).get(), timeout)

    async def _read(self) -> None:
        while True:
            header, body = await self.client.recv()
            kind = header.get("type")
            if kind == "xfer_offer":
                self._tasks.append(asyncio.ensure_future(self._receive(header)))
            elif isinstance(kind, str) and kind.startswith("xfer_") and "id" in header:
                self._queue(kind, header["id"]).put_nowait(header)
            else:
                self.inbox.put_nowait((header, time.monotonic()))

    async def _hit(self, name: str, moved: int) -> None:
        if self.gate is not None:
            await self.gate.hit(name, moved)

    # PC -> phone: the phone side of one offer
    async def _receive(self, o: dict[str, Any]) -> None:
        id_, size, name = o["id"], o["size"], o["name"]
        await self.client.send({"ch": "link", "type": "xfer_accept", "id": id_, "ranges": [[0, size, 0]] if size else []})
        data = bytearray()
        if size:
            d = await connect_data(self.server.link.port)
            await d.send({"ch": "link", "type": "xfer_attach", "id": id_, "start": 0})
            while True:
                header, body = await asyncio.wait_for(d.recv(), 30)
                if header["type"] == "xfer_end":
                    break
                data += body
                self.progress[name] = len(data)
                await self._hit(name, len(data))
                if self.read_delay:
                    await asyncio.sleep(self.read_delay)
            assert len(data) == size and header["sha256"] == sha(bytes(data))
            await d.ws.close()
        (self.folder / name).write_bytes(data)
        await self.client.send({"ch": "link", "type": "xfer_done", "id": id_, "ok": True})

    # phone -> PC
    async def push(self, blob: Blob, id_: str, *, stop_at: int | None = None) -> int:
        """Offer `blob` and send what the PC asks for. With `stop_at`, drop the data connection once that many
        bytes are sent; either way return the number of bytes streamed in this call."""
        size = len(blob.data)
        await self.client.send(offer(id_, blob.name, size))
        ranges = (await self._reply("xfer_accept", id_))["ranges"]
        if not ranges:
            assert (await self._reply("xfer_done", id_))["ok"] is True
            return 0
        [[start, _, have]] = ranges
        d = await connect_data(self.server.link.port)
        await d.send({"ch": "link", "type": "xfer_attach", "id": id_, "start": start})
        pos = have
        self.last_have = have
        sent = 0
        while pos < size:
            piece = blob.data[pos : pos + CHUNK]
            await d.send({"ch": "link", "type": "xfer_data"}, piece)
            pos += len(piece)
            sent += len(piece)
            self.progress[blob.name] = pos
            await self._hit(blob.name, pos)
            if stop_at is not None and pos >= stop_at:
                await asyncio.sleep(0.3)  # let the server drain what was sent
                await d.ws.close()
                return sent
        await d.send({"ch": "link", "type": "xfer_end", "sha256": blob.digest})
        assert (await self._reply("xfer_done", id_))["ok"] is True
        await d.ws.close()
        return sent

    async def clip_to_pc(self, text: str) -> float:
        t0 = time.monotonic()
        await self.client.send({"ch": "clipboard", "type": "clip", "kind": "text", "data": text, "ts": int(time.time() * 1000)})
        return await self.server.clip_written(text) - t0

    async def clip_from_pc(self, text: str) -> float:
        t0 = time.monotonic()
        await self.server.clipboard.on_local_text(text)
        while True:
            header, at = await asyncio.wait_for(self.inbox.get(), 5)
            if header.get("type") == "clip" and header.get("data") == text:
                return at - t0


@dataclass
class Ctx:
    server: Server
    phone: Phone
    tmp: Path
    to_pc: dict[str, str] = field(default_factory=dict)  # name -> sha256 expected in the PC folder
    to_phone: dict[str, str] = field(default_factory=dict)  # name -> sha256 expected in the phone folder
    _n: int = 0

    def next_id(self) -> str:
        self._n += 1
        return fid(self._n)

    def source(self, blob: Blob) -> Path:
        """Write `blob` to a PC-side source file and expect it on the phone."""
        src = self.tmp / "src" / blob.name
        src.parent.mkdir(exist_ok=True)
        src.write_bytes(blob.data)
        self.to_phone[blob.name] = blob.digest
        return src

    async def phone_sends(self, blob: Blob, **kw: Any) -> int:
        self.to_pc[blob.name] = blob.digest
        return await self.phone.push(blob, self.next_id(), **kw)

    async def pc_sends(self, blob: Blob) -> bool:
        return await asyncio.wait_for(self.server.files.send(self.source(blob)), 60)


# -- scenarios ------------------------------------------------------------------


async def s1_small_file_pc_to_phone(c: Ctx) -> None:
    assert await c.pc_sends(Blob.random("small.bin", 10 * KB))


async def s2_big_file_pc_to_phone(c: Ctx) -> None:
    assert await c.pc_sends(Blob.random("big.bin", BIG))


async def s4_five_photos_and_pdf_phone_to_pc(c: Ctx) -> None:
    blobs = [Blob.random(f"IMG_{i}.jpg", 800 * KB + i) for i in range(5)] + [Blob.random("scan.pdf", 300 * KB)]
    await asyncio.gather(*(c.phone_sends(b) for b in blobs))


async def s5_several_files_pc_to_phone(c: Ctx) -> None:
    blobs = [Blob.random("a.txt", 10 * KB), Blob.random("b.jpg", 2 * MB), Blob.random("c.pdf", 300 * KB), Blob.random("d.bin", MID)]
    assert all(await asyncio.gather(*(c.pc_sends(b) for b in blobs)))


async def s6_empty_file_both_directions(c: Ctx) -> None:
    await c.phone_sends(Blob("empty_up.txt", b""))
    assert await c.pc_sends(Blob("empty_down.txt", b""))


async def s7_name_clash_gets_suffix(c: Ctx) -> None:
    first, second = Blob.random("note.txt", 5 * KB), Blob.random("note.txt", 7 * KB)
    await c.phone.push(first, c.next_id())
    await c.phone.push(second, c.next_id())
    c.to_pc = {"note.txt": first.digest, "note (1).txt": second.digest}


async def mid_transfer_clips(c: Ctx, name: str, size: int, transfer: asyncio.Future[Any]) -> None:
    """Once the transfer is streaming, exchange one clip each way; both must arrive in < 1 s while bytes keep moving."""
    progress = c.phone.progress
    deadline = time.monotonic() + 20
    while progress.get(name, 0) < size // 8:
        assert time.monotonic() < deadline and not transfer.done(), "transfer never got going"
        await asyncio.sleep(0.001)
    moved0 = progress[name]
    for n in range(4):  # a window of clips both ways, long enough for several chunks to move
        assert await c.phone.clip_to_pc(f"clip from phone mid-transfer {n}") < CLIP_LIMIT
        assert await c.phone.clip_from_pc(f"clip from pc mid-transfer {n}") < CLIP_LIMIT
        await asyncio.sleep(0.05)
    moved1 = progress[name]
    assert not transfer.done() and moved0 < moved1 < size, f"bytes moved {moved0} -> {moved1} of {size} during the clips"
    await asyncio.wait_for(transfer, 60)


async def s8_clips_during_transfer_phone_to_pc(c: Ctx) -> None:
    big = Blob.random("big_up.bin", BIG)
    transfer = asyncio.ensure_future(c.phone_sends(big))
    await mid_transfer_clips(c, big.name, len(big.data), transfer)


async def s8_clips_during_transfer_pc_to_phone(c: Ctx) -> None:
    big = Blob.random("big_down.bin", BIG)
    c.phone.read_delay = 0.02  # slow only the phone's reads; the PC keeps streaming
    transfer = asyncio.ensure_future(c.pc_sends(big))
    await mid_transfer_clips(c, big.name, len(big.data), transfer)
    assert transfer.result() is True


async def s9_small_file_during_big_phone_to_pc(c: Ctx) -> None:
    big = Blob.random("big_up.bin", MID)
    c.phone.gate = Gate(big.name, MID // 4)
    transfer = asyncio.ensure_future(c.phone_sends(big))
    await asyncio.wait_for(c.phone.gate.reached.wait(), 20)
    await asyncio.wait_for(c.phone_sends(Blob.random("small_up.txt", 10 * KB)), 10)  # not stuck behind the big one
    assert not transfer.done()
    c.phone.gate.release.set()
    await asyncio.wait_for(transfer, 60)


async def s9_small_file_during_big_pc_to_phone(c: Ctx) -> None:
    big = Blob.random("big_down.bin", MID)
    c.phone.gate = Gate(big.name, MID // 4)
    transfer = asyncio.ensure_future(c.pc_sends(big))
    await asyncio.wait_for(c.phone.gate.reached.wait(), 20)
    assert await asyncio.wait_for(c.pc_sends(Blob.random("small_down.txt", 10 * KB)), 10)
    assert not transfer.done()
    c.phone.gate.release.set()
    assert await asyncio.wait_for(transfer, 60) is True


async def s10_resume_after_data_connection_drop(c: Ctx) -> None:
    blob = Blob.random("resume.bin", MID)
    id_ = c.next_id()
    c.to_pc[blob.name] = blob.digest
    first = await c.phone.push(blob, id_, stop_at=MID // 3)
    assert first >= MID // 3
    await asyncio.sleep(0.2)
    assert c.server.leftovers(), "the PC dropped its partial file when the data connection closed"
    sent = await c.phone.push(blob, id_)  # same id: the PC answers with `have`
    have = c.phone.last_have
    assert have > 0, "the PC kept nothing"
    assert sent < len(blob.data) and sent == len(blob.data) - have, f"re-sent {sent} of {len(blob.data)} bytes (have {have})"


@dataclass
class Scenario:
    number: int
    title: str
    run: Callable[[Ctx], Awaitable[None]]
    xfail: str = ""  # a real product bug this scenario exposes
    raises: tuple[type[BaseException], ...] = (AssertionError,)  # the only failure that counts as that bug


SCENARIOS = [
    Scenario(1, "PC to phone, one small file", s1_small_file_pc_to_phone),
    Scenario(2, "PC to phone, one big file", s2_big_file_pc_to_phone),
    Scenario(4, "phone to PC, five photos and a PDF in one share", s4_five_photos_and_pdf_phone_to_pc),
    Scenario(5, "PC to phone, several files at once", s5_several_files_pc_to_phone),
    Scenario(6, "empty file, both directions", s6_empty_file_both_directions),
    Scenario(7, "name clash gets the (1) suffix, both intact", s7_name_clash_gets_suffix),
    Scenario(8, "clips both ways during a phone-to-PC transfer", s8_clips_during_transfer_phone_to_pc),
    Scenario(8, "clips both ways during a PC-to-phone transfer", s8_clips_during_transfer_pc_to_phone),
    Scenario(9, "small file during a big phone-to-PC transfer", s9_small_file_during_big_phone_to_pc),
    Scenario(9, "small file during a big PC-to-phone transfer", s9_small_file_during_big_pc_to_phone,
             xfail="PC->phone sends run one file at a time per device (transfer.py STREAMS FIFO), so a small file waits behind a big one; fixed in PLAN.md step 14 (streams)",
             raises=(asyncio.TimeoutError, TimeoutError)),
    Scenario(10, "data connection dropped mid-range resumes from have", s10_resume_after_data_connection_drop),
]


@pytest.fixture
async def ctx(tmp_path: Path) -> AsyncIterator[Ctx]:
    server = Server(tmp_path)
    await server.link.start()
    phone = Phone(server, tmp_path / "phone")
    await phone.start()
    yield Ctx(server, phone, tmp_path)
    await phone.stop()
    await server.link.stop()


def folder_hashes(folder: Path) -> dict[str, str]:
    return {p.name: sha(p.read_bytes()) for p in folder.iterdir()} if folder.is_dir() else {}


@pytest.mark.parametrize(
    "scenario",
    [pytest.param(s, id=f"{s.number}-{s.title}", marks=[pytest.mark.xfail(strict=True, reason=s.xfail, raises=s.raises)] if s.xfail else []) for s in SCENARIOS],
)
async def test_scenario(ctx: Ctx, scenario: Scenario) -> None:
    await asyncio.wait_for(scenario.run(ctx), 15 if scenario.xfail else 45)
    await asyncio.sleep(0.2)  # let the PC finish its bookkeeping
    for task in ctx.phone._tasks[1:]:  # surface errors from the phone's receive tasks
        await asyncio.wait_for(task, 10)
    assert folder_hashes(ctx.server.dest) == ctx.to_pc
    assert folder_hashes(ctx.phone.folder) == ctx.to_phone
    assert ctx.server.leftovers() == []
