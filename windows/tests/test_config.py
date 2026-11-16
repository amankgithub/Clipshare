"""Device registry: DPAPI-protected `{device: {psk, name}}` round trip."""

import base64
import threading
import time
from pathlib import Path

import pytest

from clipbridge import config


@pytest.fixture
def registry(tmp_path: Path) -> config.DeviceRegistry:
    return config.DeviceRegistry(tmp_path / "devices.bin")


def test_round_trip(registry: config.DeviceRegistry, tmp_path: Path) -> None:
    assert registry.load() == {} and registry.psk("android-1") is None
    psk = registry.pair("android-1")
    other = registry.pair("android-2")
    assert len(psk) == 32 and psk != other
    assert registry.psk("android-1") == psk
    registry.set_name("android-1", "Pixel 8 é😀")
    reread = config.DeviceRegistry(tmp_path / "devices.bin")  # a second process sees the same file
    assert reread.load() == {
        "android-1": config.Device(psk, "Pixel 8 é😀"),
        "android-2": config.Device(other, ""),
    }
    raw = (tmp_path / "devices.bin").read_bytes()
    assert base64.b64encode(psk) not in raw and b"Pixel" not in raw  # DPAPI blob, not plain JSON


def test_repair_replaces_key_keeps_name(registry: config.DeviceRegistry) -> None:
    old = registry.pair("android-1")
    registry.set_name("android-1", "Pixel")
    new = registry.pair("android-1")
    assert new != old and registry.load() == {"android-1": config.Device(new, "Pixel")}


def test_unpair(registry: config.DeviceRegistry) -> None:
    registry.pair("android-1")
    registry.pair("android-2")
    assert registry.unpair("android-1") is True
    assert registry.unpair("android-1") is False
    assert registry.psk("android-1") is None and list(registry.load()) == ["android-2"]


def test_set_name_of_unknown_device_is_ignored(registry: config.DeviceRegistry) -> None:
    registry.set_name("ghost", "x")
    assert registry.load() == {}


def test_new_device_id() -> None:
    device = config.new_device_id()
    assert device.startswith("android-") and len(device) == 16
    int(device[8:], 16)


def test_set_name_waits_for_lock_and_never_resurrects(registry: config.DeviceRegistry) -> None:
    registry.pair("android-1")
    worker = threading.Thread(target=registry.set_name, args=("android-1", "Pixel"))
    with registry.locked():  # a CLI unpair holding the lock while the server stores a hello name
        worker.start()
        time.sleep(0.2)
        assert worker.is_alive()
        assert registry.unpair("android-1")
    worker.join(5)
    assert registry.load() == {}


def test_no_tmp_files_left(registry: config.DeviceRegistry, tmp_path: Path) -> None:
    registry.pair("android-1")
    registry.set_name("android-1", "x")
    registry.unpair("android-1")
    assert [p.name for p in tmp_path.iterdir()] == ["devices.bin"]


def _write_raw(path: Path, raw: object) -> None:
    import json

    path.write_bytes(config._protect(json.dumps(raw).encode("utf-8")))


@pytest.mark.parametrize(
    "entry",
    [
        {"psk": base64.b64encode(b"short").decode(), "name": "x"},  # wrong length
        {"psk": base64.b64encode(bytes(33)).decode(), "name": "x"},
        {"psk": "!!not base64!!", "name": "x"},
        {"name": "x"},  # no psk
        {"psk": None},
        "not a dict",
    ],
)
def test_corrupt_registry_entry_raises_valueerror(tmp_path: Path, entry: object) -> None:
    path = tmp_path / "devices.bin"
    _write_raw(path, {"android-1": entry})
    registry = config.DeviceRegistry(path)
    with pytest.raises(ValueError, match="android-1"):
        registry.load()
    with pytest.raises(ValueError):
        registry.psk("android-1")  # the link must not authenticate against a bad entry


def test_corrupt_file_is_not_overwritten_by_pair(tmp_path: Path) -> None:
    path = tmp_path / "devices.bin"
    _write_raw(path, {"bad": {"psk": "AA=="}})
    before = path.read_bytes()
    registry = config.DeviceRegistry(path)
    with pytest.raises(ValueError):
        registry.pair("android-2")
    assert path.read_bytes() == before  # a failed load-modify-save must not destroy the other entries


def test_lock_timeout_raises_and_leaves_registry_usable(
    registry: config.DeviceRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "LOCK_TIMEOUT_MS", 100)
    registry.pair("android-1")
    held, release = threading.Event(), threading.Event()

    def holder() -> None:
        with registry.locked():
            held.set()
            release.wait(5)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert held.wait(5)
        with pytest.raises(TimeoutError):
            registry.pair("android-2")
        with pytest.raises(TimeoutError):
            registry.unpair("android-1")
        with pytest.raises(TimeoutError):
            registry.set_name("android-1", "x")
    finally:
        release.set()
        thread.join(5)
    assert list(registry.load()) == ["android-1"]  # nothing was written while locked out
    registry.pair("android-2")  # and the lock is free again
    assert sorted(registry.load()) == ["android-1", "android-2"]


def test_rekey_invalidates_old_psk_at_once(registry: config.DeviceRegistry, tmp_path: Path) -> None:
    old = registry.pair("android-1")
    other = config.DeviceRegistry(tmp_path / "devices.bin")  # the server's view
    assert other.psk("android-1") == old
    new = registry.pair("android-1")
    assert other.psk("android-1") == new != old
    assert list(registry.load()) == ["android-1"]
