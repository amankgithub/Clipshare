"""ClipboardThread._write retries a write another process disturbed, without a real clipboard."""

from __future__ import annotations

from typing import Any

import pytest
import pywintypes
import win32con

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
