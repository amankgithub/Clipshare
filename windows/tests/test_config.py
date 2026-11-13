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
