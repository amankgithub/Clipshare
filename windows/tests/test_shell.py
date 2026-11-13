"""Explorer entry points: send path validation, pipe request codec, pipe round trip, install data."""

import json
import threading
from pathlib import Path

import pytest

from clipbridge import install
from clipbridge.__main__ import main
from clipbridge.features.files import pipe


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
    data = pipe.encode_request(paths)
    assert json.loads(data) == {"send": [str(p) for p in paths]}
    assert pipe.decode_request(data) == paths


@pytest.mark.parametrize(
    "data", [b"", b"\xff", b"[]", b'{"other": []}', b'{"send": "C:\\\\a"}', b'{"send": [1]}']
)
def test_decode_rejects_malformed(data: bytes) -> None:
    with pytest.raises(ValueError):
        pipe.decode_request(data)


def test_pipe_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipe, "PIPE_NAME", rf"\\.\pipe\ClipBridgeTest-{threading.get_ident()}")
    received: list[list[Path]] = []
    server = pipe.PipeServer(received.append)
    server.start()
    try:
        many = [tmp_path / f"{'n' * 200}-{i}.bin" for i in range(400)]  # > one pipe buffer
        pipe.send_request([tmp_path / "one.txt"])
        pipe.send_request(many)
        assert received == [[tmp_path / "one.txt"], many]
    finally:
        server.stop()
        server.join(5)
    assert not server.is_alive()


def test_send_without_server_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipe, "PIPE_NAME", r"\\.\pipe\ClipBridgeTest-nobody")
    with pytest.raises(pipe.ServerNotRunning):
        pipe.send_request([Path("C:/x")])


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
