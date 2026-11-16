"""CLI entry points: send path validation, pipe request codec, pipe round trip, unpair, install data."""

import json
import os
import threading
import uuid
from pathlib import Path

import pytest
import pywintypes

from clipbridge import install
from clipbridge.__main__ import main
from clipbridge import config, pipe


def test_validate_paths_accepts_files_rejects_folders_and_missing(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "dir").mkdir()
    files, errors = pipe.validate_paths(
        [str(tmp_path / "a.txt"), "dir", "missing.bin"], cwd=tmp_path
    )
    assert files == [tmp_path / "a.txt"]
    assert errors == ["dir: is a folder (only files can be sent)", "missing.bin: no such file"]


def test_validate_paths_resolves_relative_to_cwd(tmp_path: Path) -> None:
    (tmp_path / "b.pdf").write_bytes(b"x")
    files, errors = pipe.validate_paths(["b.pdf"], cwd=tmp_path)
    assert files == [tmp_path / "b.pdf"] and errors == []


def test_request_round_trip() -> None:
    paths = [Path(r"C:\Users\me\Ünïcode file.txt"), Path(r"D:\x\y.bin")]
    data = pipe.encode_request(pipe.Send(paths))
    assert json.loads(data) == {"send": [str(p) for p in paths]}
    assert pipe.decode_request(data) == pipe.Send(paths)
    data = pipe.encode_request(pipe.Unpair("android-0a1b2c3d"))
    assert json.loads(data) == {"unpair": "android-0a1b2c3d"}
    assert pipe.decode_request(data) == pipe.Unpair("android-0a1b2c3d")


@pytest.mark.parametrize(
    "data",
    [
        b"", b"\xff", b"[]", b'{"other": []}', b'{"send": "C:\\\\a"}', b'{"send": [1]}',
        b'{"unpair": 1}', b'{"send": [], "unpair": "x"}',
    ],
)
def test_decode_rejects_malformed(data: bytes) -> None:
    with pytest.raises(ValueError):
        pipe.decode_request(data)


def test_pipe_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipe, "PIPE_NAME", rf"\\.\pipe\ClipBridgeTest-{threading.get_ident()}")
    received: list[pipe.Request] = []
    server = pipe.PipeServer(received.append)
    server.start()
    try:
        many = [tmp_path / f"{'n' * 200}-{i}.bin" for i in range(400)]  # > one pipe buffer
        pipe.send_request(pipe.Send([tmp_path / "one.txt"]))
        pipe.send_request(pipe.Send(many))
        pipe.send_request(pipe.Unpair("android-1"))
        assert received == [pipe.Send([tmp_path / "one.txt"]), pipe.Send(many), pipe.Unpair("android-1")]
    finally:
        server.stop()
        server.join(5)
    assert not server.is_alive()


def test_send_without_server_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipe, "PIPE_NAME", r"\\.\pipe\ClipBridgeTest-nobody")
    with pytest.raises(pipe.ServerNotRunning):
        pipe.send_request(pipe.Send([Path("C:/x")]))


def test_send_cli_reports_rejected_paths_and_missing_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(pipe, "PIPE_NAME", r"\\.\pipe\ClipBridgeTest-nobody")
    (tmp_path / "f.txt").write_text("f")
    assert main(["send", str(tmp_path), str(tmp_path / "f.txt")]) == 1
    out = capsys.readouterr().out
    assert "is a folder" in out and "ClipBridge is not running" in out


def test_verb_values() -> None:
    py = Path(r"C:\repo\windows\.venv\Scripts\pythonw.exe")
    values = {(v.key, v.name): v.value for v in install.verb_values(py)}
    key = r"Software\Classes\*\shell\ClipBridge"
    assert values == {
        (key, ""): "Send to phone",
        (key, "MultiSelectModel"): "Player",
        (key, "Icon"): str(py),
        (key + r"\command", ""): r'"C:\repo\windows\.venv\Scripts\pythonw.exe" -m clipbridge send "%1"',
    }


def test_shortcut_spec() -> None:
    py = Path(r"C:\repo\windows\.venv\Scripts\pythonw.exe")
    spec = install.shortcut_spec(py, Path(r"C:\Users\me\SendTo"))
    assert spec.path == Path(r"C:\Users\me\SendTo\Phone (ClipBridge).lnk")
    assert (spec.target, spec.arguments) == (py, "-m clipbridge send")


def test_purge_dirs_never_include_received_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPDATA", r"C:\Users\me\AppData\Roaming")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\me\AppData\Local")
    assert install.purge_dirs() == [
        Path(r"C:\Users\me\AppData\Roaming\ClipBridge"),
        Path(r"C:\Users\me\AppData\Local\ClipBridge"),
    ]


def test_firewall_rule_replaces_old_and_is_private_localsubnet_only() -> None:
    args = install.firewall_args()
    delete, add = args.split(" & ")
    assert "delete rule" in delete and f'name="{install.FIREWALL_RULE}"' in delete
    assert "add rule" in add and "profile=private " in add and "remoteip=localsubnet" in add
    assert "public" not in args


def test_unpair_cli_tells_running_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(pipe, "PIPE_NAME", rf"\\.\pipe\ClipBridgeTest-unpair-{threading.get_ident()}")
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path)
    registry = config.DeviceRegistry()
    registry.pair("android-1")
    received: list[pipe.Request] = []
    server = pipe.PipeServer(received.append)
    server.start()
    try:
        assert main(["unpair", "android-1"]) == 0
        assert main(["unpair", "android-1"]) == 1
    finally:
        server.stop()
        server.join(5)
    assert received == [pipe.Unpair("android-1")]
    assert registry.load() == {}
    assert "android-1: not paired" in capsys.readouterr().out


def test_devices_cli_lists_paired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path)
    registry = config.DeviceRegistry()
    registry.pair("android-1")
    registry.pair("android-2")
    registry.set_name("android-2", "Pixel 8")
    assert main(["devices"]) == 0
    assert capsys.readouterr().out.splitlines() == ["android-1  (not connected yet)", "android-2  Pixel 8"]


def test_unpair_cli_reports_pipe_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:

    def denied(request: pipe.Request) -> None:
        raise pywintypes.error(5, "CreateFile", "Access is denied.")

    monkeypatch.setattr(config, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(pipe, "send_request", denied)
    config.DeviceRegistry().pair("android-1")
    assert main(["unpair", "android-1"]) == 1
    assert "key removed, live connections not dropped" in capsys.readouterr().out
    assert config.DeviceRegistry().load() == {}


# -- send(): other pipe errors -----------------------------------------------
@pytest.mark.xfail(
    strict=True,
    raises=pywintypes.error,
    reason="bug: send() only catches ServerNotRunning; a pywintypes.error (e.g. access denied) escapes as a traceback",
)
def test_send_cli_pipe_error_is_a_clean_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:

    def denied(request: pipe.Request) -> None:
        raise pywintypes.error(5, "CreateFile", "Access is denied.")

    monkeypatch.setattr(pipe, "send_request", denied)
    (tmp_path / "f.txt").write_text("f")
    assert main(["send", str(tmp_path / "f.txt")]) != 0
    assert "Access is denied" in capsys.readouterr().out


# -- pipe client busy loop ---------------------------------------------------
class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        self.now += 0.5
        return self.now


@pytest.fixture
def fake_pipe(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the win32 calls the client makes; returns the event log."""
    log: list[str] = []
    monkeypatch.setattr(pipe, "time", _Clock())
    monkeypatch.setattr(pipe.win32pipe, "WaitNamedPipe", lambda name, ms: log.append("wait"))
    monkeypatch.setattr(pipe.win32pipe, "SetNamedPipeHandleState", lambda *a: log.append("mode"))
    monkeypatch.setattr(pipe.win32file, "WriteFile", lambda h, data: log.append("write"))
    monkeypatch.setattr(pipe.win32file, "ReadFile", lambda h, n: log.append("read") or (0, b"\x01"))
    monkeypatch.setattr(pipe.win32file, "CloseHandle", lambda h: log.append("close"))
    return log


def test_client_retries_while_pipe_busy_then_sends(fake_pipe: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    import winerror

    attempts = iter([winerror.ERROR_PIPE_BUSY, winerror.ERROR_PIPE_BUSY, None])

    def create(*args: object) -> object:
        code = next(attempts)
        if code is not None:
            raise pywintypes.error(code, "CreateFile", "busy")
        return object()

    monkeypatch.setattr(pipe.win32file, "CreateFile", create)
    pipe.send_request(pipe.Unpair("android-1"))
    assert fake_pipe == ["wait", "wait", "mode", "write", "read", "close"]


def test_client_gives_up_after_deadline_with_busy_error(fake_pipe: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    import winerror

    calls = 0

    def busy(*args: object) -> object:
        nonlocal calls
        calls += 1
        raise pywintypes.error(winerror.ERROR_PIPE_BUSY, "CreateFile", "busy")

    monkeypatch.setattr(pipe.win32file, "CreateFile", busy)
    with pytest.raises(pywintypes.error) as info:
        pipe.send_request(pipe.Unpair("android-1"))
    assert info.value.winerror == winerror.ERROR_PIPE_BUSY
    assert 1 < calls <= 20  # bounded by CONNECT_TIMEOUT_MS, not an endless loop
    assert "write" not in fake_pipe


def test_client_other_error_is_raised_immediately(fake_pipe: list[str], monkeypatch: pytest.MonkeyPatch) -> None:

    def denied(*args: object) -> object:
        raise pywintypes.error(5, "CreateFile", "Access is denied.")

    monkeypatch.setattr(pipe.win32file, "CreateFile", denied)
    with pytest.raises(pywintypes.error):
        pipe.send_request(pipe.Unpair("android-1"))
    assert fake_pipe == []


def test_client_closes_handle_when_write_fails(fake_pipe: list[str], monkeypatch: pytest.MonkeyPatch) -> None:

    monkeypatch.setattr(pipe.win32file, "CreateFile", lambda *a: object())

    def broken(h: object, data: bytes) -> None:
        raise pywintypes.error(109, "WriteFile", "broken pipe")

    monkeypatch.setattr(pipe.win32file, "WriteFile", broken)
    with pytest.raises(pywintypes.error):
        pipe.send_request(pipe.Unpair("android-1"))
    assert fake_pipe[-1] == "close"


# -- server: malformed request ---------------------------------------------------
def _raw_exchange(name: str, payload: bytes, outcome: list[object]) -> None:
    import win32con
    import win32file
    import win32pipe

    handle = win32file.CreateFile(
        name, win32con.GENERIC_READ | win32con.GENERIC_WRITE, 0, None, win32con.OPEN_EXISTING, 0, None
    )
    try:
        win32pipe.SetNamedPipeHandleState(handle, win32pipe.PIPE_READMODE_MESSAGE, None, None)
        win32file.WriteFile(handle, payload)
        try:
            outcome.append(win32file.ReadFile(handle, 1)[1])
        except pywintypes.error as e:
            outcome.append(e.winerror)
    finally:
        win32file.CloseHandle(handle)


@pytest.mark.parametrize("payload", [b"not json", b"\xff\xfe", b'{"send": 3}', b"[]"])
def test_malformed_pipe_request_gets_reply_or_clean_close_and_server_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    import winerror

    name = rf"\\.\pipe\ClipBridgeTest-malformed-{os.getpid()}-{uuid.uuid4().hex}"
    monkeypatch.setattr(pipe, "PIPE_NAME", name)
    received: list[pipe.Request] = []
    server = pipe.PipeServer(received.append)
    server.start()
    try:
        outcome: list[object] = []
        client = threading.Thread(target=_raw_exchange, args=(name, payload, outcome), daemon=True)
        client.start()
        client.join(5)
        assert not client.is_alive(), "client hangs on a malformed request"  # xfail-worthy if it does
        assert outcome and outcome == [winerror.ERROR_PIPE_NOT_CONNECTED]  # no ack: the server logs and disconnects, so the read fails
        assert received == []  # never handed to the server logic
        pipe.send_request(pipe.Unpair("android-1"))  # still serving
        assert received == [pipe.Unpair("android-1")]
    finally:
        server.stop()
        server.join(5)
