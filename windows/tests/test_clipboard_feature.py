"""ClipboardFeature glue: pending flush, parsing, oversize images, saving PNGs (fake link and peers)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from clipbridge.features.clipboard import feature as cf
from clipbridge.features.clipboard.feature import ClipboardFeature, save_png
from clipbridge.link.envelope import MAX_PLAINTEXT

PNG = b"\x89PNG\r\n\x1a\nfake"


class FakePeer:
    def __init__(self, device: str = "android-1", features: dict[str, int] | None = None) -> None:
        self.device = device
        self.features = {"clipboard": 1} if features is None else features

    def supports(self, feature: str, version: int = 1) -> bool:
        return self.features.get(feature, 0) >= version


class FakeLink:
    def __init__(self) -> None:
        self.peers: list[FakePeer] = []
        self.sent: list[tuple[FakePeer, dict[str, Any], bytes]] = []
        self.broadcasts: list[tuple[dict[str, Any], bytes, FakePeer | None]] = []

    async def send(self, peer: FakePeer, header: dict[str, Any], body: bytes = b"") -> bool:
        self.sent.append((peer, header, body))
        return True

    async def broadcast(self, header: dict[str, Any], body: bytes = b"", exclude: FakePeer | None = None) -> None:
        self.broadcasts.append((header, body, exclude))


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.link = FakeLink()
        self.texts: list[str] = []
        self.images: list[bytes] = []
        self.dir = tmp_path / "pics"
        self.feature = ClipboardFeature(
            self.link, self.texts.append, self.images.append, save_dir=self.dir  # type: ignore[arg-type]
        )


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


# -- pending flush --------------------------------------------------------------
async def test_pending_clip_flushed_once_to_first_supporting_peer(env: Env) -> None:
    await env.feature.on_local_text("copied offline")
    assert env.link.broadcasts == [] and env.link.sent == []
    legacy, phone, second = FakePeer(features={}), FakePeer("android-1"), FakePeer("android-2")
    await env.feature.on_peer_connected(legacy)  # no clipboard feature: pending stays
    assert env.link.sent == []
    await env.feature.on_peer_connected(phone)
    await env.feature.on_peer_connected(second)
    ((peer, header, body),) = env.link.sent
    assert peer is phone and body == b""
    assert {k: header[k] for k in ("ch", "type", "kind", "data")} == {
        "ch": "clipboard", "type": "clip", "kind": "text", "data": "copied offline"
    }
    assert isinstance(header["ts"], int)


async def test_pending_image_flushed_with_png_body(env: Env) -> None:
    await env.feature.on_local_image(PNG)
    await env.feature.on_peer_connected(FakePeer())
    ((_, header, body),) = env.link.sent
    assert header["kind"] == "image" and header["mime"] == "image/png" and body == PNG and "data" not in header


async def test_nothing_pending_sends_nothing_on_connect(env: Env) -> None:
    await env.feature.on_peer_connected(FakePeer())
    await env.feature.on_peer_disconnected(FakePeer())
    assert env.link.sent == []


async def test_connected_peer_gets_broadcast_not_pending(env: Env) -> None:
    env.link.peers = [FakePeer()]
    await env.feature.on_local_text("now")
    ((header, _, exclude),) = env.link.broadcasts
    assert header["data"] == "now" and exclude is None
    await env.feature.on_peer_connected(FakePeer("android-2"))
    assert env.link.sent == []


async def test_local_text_cleaned_and_empty_ignored(env: Env) -> None:
    env.link.peers = [FakePeer()]
    await env.feature.on_local_text("")
    await env.feature.on_local_image(b"")
    assert env.link.broadcasts == []
    await env.feature.on_local_text("a\ud800b")  # lone surrogate must not reach the wire
    assert env.link.broadcasts[0][0]["data"] == "a?b"


# -- oversize image ---------------------------------------------------------------
async def test_oversize_local_image_skipped_and_not_queued(env: Env) -> None:
    await env.feature.on_local_image(bytes(MAX_PLAINTEXT - 255))  # +256 header budget exceeds the limit
    env.link.peers = [FakePeer()]
    await env.feature.on_peer_connected(FakePeer())
    assert env.link.broadcasts == [] and env.link.sent == []


async def test_largest_allowed_local_image_is_sent(env: Env) -> None:
    env.link.peers = [FakePeer()]
    await env.feature.on_local_image(bytes(MAX_PLAINTEXT - 256))
    assert len(env.link.broadcasts) == 1


# -- _parse / on_message -----------------------------------------------------------
@pytest.mark.parametrize(
    "header, body",
    [
        ({"type": "other", "kind": "text", "data": "x"}, b""),
        ({"kind": "text", "data": "x"}, b""),
        ({"type": "clip", "kind": "text"}, b""),
        ({"type": "clip", "kind": "text", "data": ""}, b""),
        ({"type": "clip", "kind": "text", "data": 5}, b""),
        ({"type": "clip", "kind": "text", "data": None}, b""),
        ({"type": "clip", "kind": "image", "mime": "image/jpeg"}, PNG),
        ({"type": "clip", "kind": "image", "mime": "image/png"}, b""),
        ({"type": "clip", "kind": "image"}, PNG),
        ({"type": "clip", "kind": "video", "data": "x"}, b""),
        ({"type": "clip"}, b""),
    ],
)
def test_parse_rejects_malformed(header: dict[str, Any], body: bytes) -> None:
    assert ClipboardFeature._parse(header, body) is None


def test_parse_accepts_text_and_png() -> None:
    assert ClipboardFeature._parse({"type": "clip", "kind": "text", "data": "hi"}, b"") == ("text", "hi")
    assert ClipboardFeature._parse({"type": "clip", "kind": "image", "mime": "image/png"}, PNG) == ("image", PNG)


@pytest.mark.parametrize(
    "header, body",
    [
        ({"type": "clip", "kind": "text", "data": ""}, b""),
        ({"type": "clip", "kind": "image", "mime": "image/gif"}, PNG),
        ({"type": "clip", "kind": "bogus"}, b""),
    ],
)
async def test_malformed_clip_changes_nothing(env: Env, header: dict[str, Any], body: bytes) -> None:
    await env.feature.on_message(FakePeer(), {"ch": "clipboard", **header}, body)  # type: ignore[arg-type]
    assert env.texts == [] and env.images == [] and env.link.broadcasts == []
    assert not env.dir.exists()


@pytest.mark.xfail(
    strict=True,
    raises=pytest.fail.Exception,
    reason="bug: a malformed clip returns None from on_message, so the link answers a request with ok:true",
)
async def test_malformed_clip_request_is_not_acknowledged_as_ok(env: Env) -> None:
    with pytest.raises(Exception):  # raising makes the link send `ok: false`
        await env.feature.on_message(
            FakePeer(), {"ch": "clipboard", "type": "clip", "kind": "text", "data": ""}, b""  # type: ignore[arg-type]
        )


async def test_remote_text_written_and_forwarded_to_others_only(env: Env) -> None:
    peer = FakePeer()
    await env.feature.on_message(peer, {"ch": "clipboard", "type": "clip", "kind": "text", "data": "hi"}, b"")  # type: ignore[arg-type]
    assert env.texts == ["hi"]
    ((header, _, exclude),) = env.link.broadcasts
    assert header["data"] == "hi" and exclude is peer


async def test_remote_echo_dropped(env: Env) -> None:
    msg = {"ch": "clipboard", "type": "clip", "kind": "text", "data": "hi"}
    await env.feature.on_message(FakePeer(), msg, b"")  # type: ignore[arg-type]
    await env.feature.on_message(FakePeer("android-2"), msg, b"")  # type: ignore[arg-type]
    assert env.texts == ["hi"] and len(env.link.broadcasts) == 1


async def test_remote_image_saved_written_and_forwarded(env: Env) -> None:
    peer = FakePeer()
    await env.feature.on_message(peer, {"ch": "clipboard", "type": "clip", "kind": "image", "mime": "image/png"}, PNG)  # type: ignore[arg-type]
    (saved,) = env.dir.iterdir()
    assert saved.read_bytes() == PNG and saved.suffix == ".png" and saved.name.startswith("clip_")
    assert env.images == [PNG] and env.link.broadcasts[0][1] == PNG and env.link.broadcasts[0][2] is peer


async def test_remote_image_save_failure_still_writes_clipboard(env: Env, tmp_path: Path) -> None:
    (tmp_path / "pics").write_bytes(b"a file where the folder should be")
    await env.feature.on_message(FakePeer(), {"ch": "clipboard", "type": "clip", "kind": "image", "mime": "image/png"}, PNG)  # type: ignore[arg-type]
    assert env.images == [PNG]


async def test_remote_image_without_image_writer_is_saved_only(tmp_path: Path) -> None:
    link = FakeLink()
    feature = ClipboardFeature(link, lambda t: None, None, save_dir=tmp_path / "p")  # type: ignore[arg-type]
    await feature.on_message(FakePeer(), {"ch": "clipboard", "type": "clip", "kind": "image", "mime": "image/png"}, PNG)  # type: ignore[arg-type]
    assert [p.read_bytes() for p in (tmp_path / "p").iterdir()] == [PNG]


async def test_remote_image_uses_default_dir_when_none_given(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    feature = ClipboardFeature(FakeLink(), lambda t: None)  # type: ignore[arg-type]
    await feature.on_message(FakePeer(), {"ch": "clipboard", "type": "clip", "kind": "image", "mime": "image/png"}, PNG)  # type: ignore[arg-type]
    assert [p.read_bytes() for p in (tmp_path / "Pictures" / "ClipBridge").iterdir()] == [PNG]


# -- save_png / default dir ----------------------------------------------------------
def test_save_png_name_suffix_and_dir_creation(tmp_path: Path) -> None:
    now = datetime(2026, 1, 2, 3, 4, 5)
    target = tmp_path / "a" / "b"
    first = save_png(target, b"1", now)
    second = save_png(target, b"2", now)
    third = save_png(target, b"3", now)
    assert [p.name for p in (first, second, third)] == [
        "clip_20260102_030405.png", "clip_20260102_030405_1.png", "clip_20260102_030405_2.png"
    ]
    assert [p.read_bytes() for p in (first, second, third)] == [b"1", b"2", b"3"]  # never overwrites


def test_default_save_dir_uses_userprofile_then_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "u"))
    assert cf.default_save_dir() == tmp_path / "u" / "Pictures" / "ClipBridge"
    monkeypatch.delenv("USERPROFILE")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "h"))
    assert cf.default_save_dir() == tmp_path / "h" / "Pictures" / "ClipBridge"
