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
