"""`clipbridge pair`: adb lookup, primary IP, key push, failure and re-pair behaviour (no real adb / network)."""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clipbridge import __main__ as cli
from clipbridge import config, pipe


class FakeRun:
    """Stands in for subprocess.run inside the CLI module."""

    def __init__(self, returncode: int = 0, stdout: str = "Starting: Intent", stderr: str = "") -> None:
        self.result = subprocess.CompletedProcess([], returncode, stdout, stderr)
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        return self.result


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_primary_ip", lambda: "192.168.1.7")
    monkeypatch.setattr(cli, "_find_adb", lambda: r"C:\adb\adb.exe")
    run = FakeRun()
    monkeypatch.setattr(cli, "subprocess", SimpleNamespace(run=run))
    return SimpleNamespace(run=run, registry=config.DeviceRegistry())


def test_pair_pushes_device_psk_host_and_saves_key(env: SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.pair() == 0
    ((device, entry),) = env.registry.load().items()
    psk_b64 = base64.b64encode(entry.psk).decode()
    (cmd,) = env.run.calls
    assert cmd[:2] == [r"C:\adb\adb.exe", "shell"]
    assert cmd[cmd.index("-n") + 1] == cli.ANDROID_ACTIVITY
    extras = {cmd[i + 1]: cmd[i + 2] for i, a in enumerate(cmd) if a == "--es"}
    assert extras == {"device": device, "psk": psk_b64, "host": "192.168.1.7"}
    out = capsys.readouterr().out
    assert f"{device}:{psk_b64}" in out and "Pushed to phone." in out


def test_pair_without_adb_prints_code_and_keeps_key_for_manual_entry(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_find_adb", lambda: None)
    assert cli.pair() == 1
    ((device, entry),) = env.registry.load().items()
    out = capsys.readouterr().out
    assert "adb not found" in out and f"{device}:{base64.b64encode(entry.psk).decode()}" in out
    assert env.run.calls == []


@pytest.mark.parametrize("returncode, stdout", [(1, ""), (0, "Error: Activity class does not exist.")])
def test_pair_adb_failure_returns_1_and_reports(
    env: SimpleNamespace, capsys: pytest.CaptureFixture[str], returncode: int, stdout: str
) -> None:
    env.run.result = subprocess.CompletedProcess([], returncode, stdout, "device offline")
    assert cli.pair() == 1
    assert "adb pairing failed" in capsys.readouterr().out


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="bug: pair() saves the key (__main__.py:42) before the adb push (:53), so a failed push leaves a live key",
)
def test_failed_push_leaves_no_live_key(env: SimpleNamespace) -> None:
    env.run.result = subprocess.CompletedProcess([], 1, "", "device offline")
    assert cli.pair() == 1
    assert env.registry.load() == {}


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="bug: every pair mints a new device id (__main__.py:41), so the old id and its key stay valid after re-pair",
)
def test_repair_does_not_leave_old_key_valid(env: SimpleNamespace) -> None:
    assert cli.pair() == 0
    assert cli.pair() == 0
    assert len(env.registry.load()) == 1


def test_repair_mints_new_id_each_time(env: SimpleNamespace) -> None:
    assert cli.pair() == 0 and cli.pair() == 0
    assert len(env.registry.load()) == 2  # pins the buggy behaviour; flips together with the xfail test_repair_does_not_leave_old_key_valid


def test_unpair_when_server_not_running_removes_key(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(pipe, "PIPE_NAME", r"\\.\pipe\ClipBridgeTest-pair-nobody")
    env.registry.pair("android-1")
    assert cli.unpair("android-1") == 0
    assert env.registry.load() == {}
    assert "android-1: unpaired" in capsys.readouterr().out


# -- _find_adb ---------------------------------------------------------------
@pytest.fixture
def adb_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT", "LOCALAPPDATA"):
        monkeypatch.delenv(var, raising=False)

    def sdk(name: str) -> Path:
        exe = tmp_path / name / "platform-tools" / "adb.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
        return exe

    return SimpleNamespace(sdk=sdk)


def test_find_adb_prefers_path(adb_env: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    adb_env.sdk("home")
    monkeypatch.setattr(cli.shutil, "which", lambda name: r"C:\path\adb.exe" if name == "adb" else None)
    monkeypatch.setenv("ANDROID_HOME", str(tmp_path / "home"))
    assert cli._find_adb() == r"C:\path\adb.exe"


def test_find_adb_order_home_then_sdk_root_then_localappdata(
    adb_env: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, root = adb_env.sdk("home"), adb_env.sdk("root")
    local = tmp_path / "local"
    (local / "Android" / "Sdk" / "platform-tools").mkdir(parents=True)
    (local / "Android" / "Sdk" / "platform-tools" / "adb.exe").write_bytes(b"")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    assert cli._find_adb() == str(local / "Android" / "Sdk" / "platform-tools" / "adb.exe")
    monkeypatch.setenv("ANDROID_SDK_ROOT", str(tmp_path / "root"))
    assert cli._find_adb() == str(root)
    monkeypatch.setenv("ANDROID_HOME", str(tmp_path / "home"))
    assert cli._find_adb() == str(home)


def test_find_adb_skips_roots_without_adb_and_returns_none(
    adb_env: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANDROID_HOME", str(tmp_path / "empty"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "nolocal"))
    assert cli._find_adb() is None


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="bug: _find_adb with LOCALAPPDATA unset probes Path('')/Android/Sdk, i.e. a relative path under the cwd",
)
def test_find_adb_without_any_env_does_not_probe_cwd(
    adb_env: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "Android" / "Sdk" / "platform-tools").mkdir(parents=True)
    (tmp_path / "Android" / "Sdk" / "platform-tools" / "adb.exe").write_bytes(b"")
    monkeypatch.chdir(tmp_path)  # LOCALAPPDATA unset -> Path("") is the cwd
    assert cli._find_adb() is None


# -- _primary_ip -------------------------------------------------------------
def test_primary_ip_uses_route_without_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    class FakeSock:
        def __init__(self, family: int, kind: int) -> None:
            seen.append(("new", family, kind))

        def __enter__(self) -> "FakeSock":
            return self

        def __exit__(self, *a: object) -> None:
            seen.append("closed")

        def connect(self, addr: tuple[str, int]) -> None:
            seen.append(("connect", addr))

        def getsockname(self) -> tuple[str, int]:
            return ("10.1.2.3", 5555)

    monkeypatch.setattr(cli, "socket", SimpleNamespace(socket=FakeSock, AF_INET=2, SOCK_DGRAM=2))
    assert cli._primary_ip() == "10.1.2.3"
    assert seen == [("new", 2, 2), ("connect", ("192.0.2.1", 9)), "closed"]
