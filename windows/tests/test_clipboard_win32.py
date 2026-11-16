"""ClipboardThread._write retries a write another process disturbed, without a real clipboard."""

from __future__ import annotations

import io
import struct
from typing import Any

import pytest
import pywintypes
import win32con
from PIL import Image

from clipbridge.features.clipboard import win32


class FakeClipboard:
    def __init__(self, set_failures: int) -> None:
        self.set_failures = set_failures
        self.opened = 0
        self.closed = 0
        self.data: dict[int, Any] = {}

    def OpenClipboard(self, hwnd: int) -> None:
        self.opened += 1

    def CloseClipboard(self) -> None:
        self.closed += 1

    def EmptyClipboard(self) -> None:
        self.data.clear()

    def SetClipboardData(self, fmt: int, value: Any) -> None:
        if self.set_failures > 0:
            self.set_failures -= 1
            raise pywintypes.error(6, "SetClipboardData", "The handle is invalid.")
        self.data[fmt] = value


@pytest.fixture
def thread(monkeypatch: pytest.MonkeyPatch) -> win32.ClipboardThread:
    monkeypatch.setattr(win32.time, "sleep", lambda _: None)
    return win32.ClipboardThread(on_text=lambda _: None, on_resume=lambda: None)


class FakeReadClipboard:
    """Read side: `formats` maps format id -> raw payload."""

    def __init__(self, formats: dict[int, Any]) -> None:
        self.formats = formats

    def IsClipboardFormatAvailable(self, fmt: int) -> bool:
        return fmt in self.formats

    def OpenClipboard(self, hwnd: int) -> None:
        pass

    def CloseClipboard(self) -> None:
        pass

    def GetClipboardData(self, fmt: int) -> Any:
        return self.formats[fmt]


HISTORY_FMT, CLOUD_FMT = 0xC001, 0xC002


def _synced(monkeypatch: pytest.MonkeyPatch, extra: dict[int, bytes]) -> list[str]:
    fake = FakeReadClipboard({win32con.CF_UNICODETEXT: "secret", **extra})
    monkeypatch.setattr(win32, "win32clipboard", fake)
    got: list[str] = []
    t = win32.ClipboardThread(on_text=got.append, on_resume=lambda: None)
    t._exclude_fmt = 0xC000
    t._optout_fmts = (HISTORY_FMT, CLOUD_FMT)
    t._handle_update()
    return got


@pytest.mark.parametrize("fmt", [HISTORY_FMT, CLOUD_FMT])
def test_optout_format_zero_is_skipped(monkeypatch: pytest.MonkeyPatch, fmt: int) -> None:
    assert _synced(monkeypatch, {fmt: b"\x00\x00\x00\x00"}) == []


@pytest.mark.parametrize("fmt", [HISTORY_FMT, CLOUD_FMT])
def test_optout_format_one_is_synced(monkeypatch: pytest.MonkeyPatch, fmt: int) -> None:
    assert _synced(monkeypatch, {fmt: b"\x01\x00\x00\x00"}) == ["secret"]


def test_exclude_format_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _synced(monkeypatch, {0xC000: b""}) == []


def test_write_retries_after_set_failure(monkeypatch: pytest.MonkeyPatch, thread: win32.ClipboardThread) -> None:
    fake = FakeClipboard(set_failures=2)
    monkeypatch.setattr(win32, "win32clipboard", fake)
    assert thread._write("text", "hello", b"")
    assert fake.data == {win32con.CF_UNICODETEXT: "hello"}
    assert fake.opened == fake.closed == 3


def test_write_gives_up_after_all_retries(monkeypatch: pytest.MonkeyPatch, thread: win32.ClipboardThread) -> None:
    fake = FakeClipboard(set_failures=100)
    monkeypatch.setattr(win32, "win32clipboard", fake)
    assert not thread._write("text", "hello", b"")
    assert fake.opened == fake.closed == len(win32.OPEN_RETRY_DELAYS)


# -- dib_to_png / png_to_dib -----------------------------------------------------------
PNG_FMT, EXCLUDE_FMT = 0xC010, 0xC000


def _png(mode: str, size: tuple[int, int], color: Any) -> bytes:
    out = io.BytesIO()
    Image.new(mode, size, color).save(out, "PNG")
    return out.getvalue()


def _pixels(png: bytes) -> list[Any]:
    with Image.open(io.BytesIO(png)) as im:
        raw = im.convert("RGB").tobytes()
    return [tuple(raw[i : i + 3]) for i in range(0, len(raw), 3)]


def test_png_dib_round_trip_rgb() -> None:
    src = _png("RGB", (5, 3), (10, 200, 30))
    dib = win32.png_to_dib(src)
    assert struct.unpack_from("<I", dib, 0)[0] == 40  # BITMAPINFOHEADER, file header stripped
    assert _pixels(win32.dib_to_png(dib)) == [(10, 200, 30)] * 15


def test_png_to_dib_flattens_alpha_on_white() -> None:
    dib = win32.png_to_dib(_png("RGBA", (2, 2), (255, 0, 0, 0)))
    assert _pixels(win32.dib_to_png(dib)) == [(255, 255, 255)] * 4
    dib = win32.png_to_dib(_png("RGBA", (2, 2), (255, 0, 0, 255)))
    assert _pixels(win32.dib_to_png(dib)) == [(255, 0, 0)] * 4


def test_png_to_dib_accepts_palette_and_grayscale() -> None:
    assert _pixels(win32.dib_to_png(win32.png_to_dib(_png("P", (2, 1), 0)))) == [(0, 0, 0)] * 2
    assert _pixels(win32.dib_to_png(win32.png_to_dib(_png("L", (2, 1), 128)))) == [(128, 128, 128)] * 2


def _info_header(width: int, height: int, bits: int, compression: int = 0, clr_used: int = 0, size: int = 40) -> bytes:
    head = struct.pack("<IiiHHIIiiII", size, width, height, 1, bits, compression, 0, 0, 0, clr_used, 0)
    return head + bytes(size - 40)


def test_dib_to_png_bitfields_32bit_masks_after_header() -> None:
    masks = struct.pack("<III", 0xFF0000, 0x00FF00, 0x0000FF)
    pixel = bytes([30, 20, 10, 0])  # B G R x
    dib = _info_header(2, 1, 32, compression=3) + masks + pixel * 2
    assert _pixels(win32.dib_to_png(dib)) == [(10, 20, 30)] * 2


def test_dib_to_png_8bit_palette_default_size() -> None:
    palette = bytes([0, 0, 255, 0]) + bytes(4 * 255)  # index 0 = red (BGRA), clr_used == 0 -> 256 entries
    dib = _info_header(2, 1, 8) + palette + bytes([0, 0, 0, 0])  # row padded to 4 bytes
    assert _pixels(win32.dib_to_png(dib)) == [(255, 0, 0)] * 2


def test_dib_to_png_24bit_rows_are_padded() -> None:
    row = bytes([1, 2, 3]) + bytes(1)  # one pixel B,G,R + pad
    assert _pixels(win32.dib_to_png(_info_header(1, 2, 24) + row * 2)) == [(3, 2, 1)] * 2


def test_dib_to_png_v5_header() -> None:
    dib = _info_header(1, 1, 32, compression=3, size=124)
    dib = dib[:40] + struct.pack("<IIII", 0xFF0000, 0xFF00, 0xFF, 0xFF000000) + dib[56:] + bytes([3, 2, 1, 255])
    assert _pixels(win32.dib_to_png(dib)) == [(1, 2, 3)]


@pytest.mark.parametrize("bad", [b"", b"\x28\x00\x00\x00", bytes(60)])
def test_dib_to_png_rejects_garbage_by_raising(bad: bytes) -> None:
    with pytest.raises((struct.error, OSError, ValueError)):  # Pillow: UnidentifiedImageError is an OSError
        win32.dib_to_png(bad)


# -- _handle_update format precedence --------------------------------------------------------
def _update(monkeypatch: pytest.MonkeyPatch, formats: dict[int, Any], image: bool = True) -> list[Any]:
    fake = FakeReadClipboard(formats)
    monkeypatch.setattr(win32, "win32clipboard", fake)
    got: list[Any] = []
    t = win32.ClipboardThread(
        on_text=lambda s: got.append(("text", s)),
        on_resume=lambda: None,
        on_image=(lambda b: got.append(("image", b))) if image else None,
    )
    t._exclude_fmt, t._optout_fmts, t._png_fmt = EXCLUDE_FMT, (), PNG_FMT
    t._handle_update()
    return got


DIB = win32.png_to_dib(_png("RGB", (1, 1), (1, 2, 3)))
DIB5 = win32.png_to_dib(_png("RGB", (1, 1), (9, 8, 7)))


def test_text_wins_over_image_formats(monkeypatch: pytest.MonkeyPatch) -> None:
    got = _update(monkeypatch, {win32con.CF_UNICODETEXT: "t", PNG_FMT: b"png", win32con.CF_DIB: DIB})
    assert got == [("text", "t")]


def test_png_wins_over_dibs_and_is_passed_through_raw(monkeypatch: pytest.MonkeyPatch) -> None:
    got = _update(monkeypatch, {PNG_FMT: b"raw-png", win32.CF_DIBV5: DIB5, win32con.CF_DIB: DIB})
    assert got == [("image", b"raw-png")]


def test_dibv5_wins_over_dib(monkeypatch: pytest.MonkeyPatch) -> None:
    got = _update(monkeypatch, {win32.CF_DIBV5: DIB5, win32con.CF_DIB: DIB})
    assert [k for k, _ in got] == ["image"] and _pixels(got[0][1]) == [(9, 8, 7)]


def test_plain_dib_is_converted(monkeypatch: pytest.MonkeyPatch) -> None:
    got = _update(monkeypatch, {win32con.CF_DIB: DIB})
    assert _pixels(got[0][1]) == [(1, 2, 3)]


def test_image_ignored_without_image_callback_or_formats(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _update(monkeypatch, {PNG_FMT: b"x", win32con.CF_DIB: DIB}, image=False) == []
    assert _update(monkeypatch, {win32con.CF_HDROP: b"files"}) == []
    assert _update(monkeypatch, {}) == []


def test_text_still_synced_without_image_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _update(monkeypatch, {win32con.CF_UNICODETEXT: "t", PNG_FMT: b"x"}, image=False) == [("text", "t")]


def test_empty_text_and_empty_image_data_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _update(monkeypatch, {win32con.CF_UNICODETEXT: ""}) == []
    assert _update(monkeypatch, {PNG_FMT: b""}) == []
    assert _update(monkeypatch, {win32con.CF_DIB: "not bytes"}) == []


def test_unconvertible_dib_is_dropped_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _update(monkeypatch, {win32con.CF_DIB: b"garbage"}) == []


def test_update_survives_getclipboarddata_error_and_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    class Failing(FakeReadClipboard):
        closed = 0

        def GetClipboardData(self, fmt: int) -> Any:
            raise pywintypes.error(1418, "GetClipboardData", "Clipboard is not open")

        def CloseClipboard(self) -> None:
            self.closed += 1

    fake = Failing({win32con.CF_UNICODETEXT: "t"})
    monkeypatch.setattr(win32, "win32clipboard", fake)
    got: list[str] = []
    t = win32.ClipboardThread(on_text=got.append, on_resume=lambda: None)
    t._exclude_fmt = EXCLUDE_FMT
    t._handle_update()
    assert got == [] and fake.closed == 1


# -- _handle_set image path -------------------------------------------------------------------
def test_handle_set_image_writes_png_and_dib(monkeypatch: pytest.MonkeyPatch, thread: win32.ClipboardThread) -> None:
    fake = FakeClipboard(set_failures=0)
    monkeypatch.setattr(win32, "win32clipboard", fake)
    thread._png_fmt = PNG_FMT
    png = _png("RGB", (2, 2), (4, 5, 6))
    thread._outbox.append(("image", png))
    thread._handle_set()
    assert fake.data[PNG_FMT] == png
    assert _pixels(win32.dib_to_png(fake.data[win32con.CF_DIB])) == [(4, 5, 6)] * 4
    assert not thread._outbox


def test_handle_set_skips_bad_image_and_continues_with_next(
    monkeypatch: pytest.MonkeyPatch, thread: win32.ClipboardThread
) -> None:
    fake = FakeClipboard(set_failures=0)
    monkeypatch.setattr(win32, "win32clipboard", fake)
    thread._png_fmt = PNG_FMT
    thread._outbox.extend([("image", b"not a png"), ("text", "after")])
    thread._handle_set()
    assert fake.data == {win32con.CF_UNICODETEXT: "after"} and not thread._outbox


def test_handle_set_text_only(monkeypatch: pytest.MonkeyPatch, thread: win32.ClipboardThread) -> None:
    fake = FakeClipboard(set_failures=0)
    monkeypatch.setattr(win32, "win32clipboard", fake)
    thread._outbox.append(("text", "hi"))
    thread._handle_set()
    assert fake.data == {win32con.CF_UNICODETEXT: "hi"}


# -- _dword_is_zero -----------------------------------------------------------------------------
class CountingRead(FakeReadClipboard):
    def __init__(self, formats: dict[int, Any], open_fails: bool = False, get_raises: bool = False) -> None:
        super().__init__(formats)
        self.open_fails, self.get_raises = open_fails, get_raises
        self.opened = self.closed = 0

    def OpenClipboard(self, hwnd: int) -> None:
        self.opened += 1
        if self.open_fails:
            raise pywintypes.error(5, "OpenClipboard", "Access is denied.")

    def CloseClipboard(self) -> None:
        self.closed += 1

    def GetClipboardData(self, fmt: int) -> Any:
        if self.get_raises:
            raise pywintypes.error(1418, "GetClipboardData", "not open")
        return super().GetClipboardData(fmt)


@pytest.mark.parametrize(
    "data, expected",
    [
        (b"\x00\x00\x00\x00", True),
        (bytearray(4), True),
        (b"\x00\x00\x00\x00\xff", True),  # only the first DWORD counts
        (b"\x01\x00\x00\x00", False),
        (b"\x00\x00\x00\x01", False),  # little endian: 0x01000000
        (b"\x00\x00\x00", False),  # too short
        (b"", False),
        ("0000", False),  # not bytes
        (0, False),
    ],
)
def test_dword_is_zero_values(
    monkeypatch: pytest.MonkeyPatch, thread: win32.ClipboardThread, data: Any, expected: bool
) -> None:
    fake = CountingRead({HISTORY_FMT: data})
    monkeypatch.setattr(win32, "win32clipboard", fake)
    assert thread._dword_is_zero(HISTORY_FMT) is expected
    assert fake.opened == fake.closed == 1


def test_dword_is_zero_false_when_format_absent_without_opening(
    monkeypatch: pytest.MonkeyPatch, thread: win32.ClipboardThread
) -> None:
    fake = CountingRead({})
    monkeypatch.setattr(win32, "win32clipboard", fake)
    assert thread._dword_is_zero(HISTORY_FMT) is False and fake.opened == 0


def test_dword_is_zero_false_when_clipboard_cannot_be_opened(
    monkeypatch: pytest.MonkeyPatch, thread: win32.ClipboardThread
) -> None:
    fake = CountingRead({HISTORY_FMT: bytes(4)}, open_fails=True)
    monkeypatch.setattr(win32, "win32clipboard", fake)
    assert thread._dword_is_zero(HISTORY_FMT) is False
    assert fake.closed == 0  # never opened, so never closed


def test_dword_is_zero_false_and_closes_when_read_fails(
    monkeypatch: pytest.MonkeyPatch, thread: win32.ClipboardThread
) -> None:
    fake = CountingRead({HISTORY_FMT: bytes(4)}, get_raises=True)
    monkeypatch.setattr(win32, "win32clipboard", fake)
    assert thread._dword_is_zero(HISTORY_FMT) is False
    assert fake.opened == fake.closed == 1
