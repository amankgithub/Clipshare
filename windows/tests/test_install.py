"""install / uninstall / status with every system touch point faked (no registry, Task Scheduler, firewall, UAC)."""

from __future__ import annotations

import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clipbridge import app, config, install

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
PYW = Path(r"C:\repo\windows\.venv\Scripts\pythonw.exe")


def _parse(xml: str) -> ET.Element:
    return ET.fromstring(xml.encode("utf-16"))  # the declaration says UTF-16


# -- task_xml ----------------------------------------------------------------
def test_task_xml_triggers_restart_policy_and_action() -> None:
    root = _parse(install.task_xml(PYW, r"PC\me"))
    assert [e.tag.split("}")[1] for e in root.find("t:Triggers", NS)] == ["LogonTrigger", "SessionStateChangeTrigger"]
    assert root.findtext("t:Triggers/t:SessionStateChangeTrigger/t:StateChange", namespaces=NS) == "SessionUnlock"
    for trigger in root.find("t:Triggers", NS):
        assert trigger.findtext("t:UserId", namespaces=NS) == r"PC\me"
        assert trigger.findtext("t:Enabled", namespaces=NS) == "true"
    restart = root.find("t:Settings/t:RestartOnFailure", NS)
    assert restart is not None and restart.findtext("t:Interval", namespaces=NS) == "PT1M"
    assert restart.findtext("t:Count", namespaces=NS) == "999"
    assert root.findtext("t:Settings/t:MultipleInstancesPolicy", namespaces=NS) == "IgnoreNew"
    assert root.findtext("t:Settings/t:ExecutionTimeLimit", namespaces=NS) == "PT0S"
    assert root.findtext("t:Principals/t:Principal/t:RunLevel", namespaces=NS) == "LeastPrivilege"
    assert root.findtext("t:Actions/t:Exec/t:Command", namespaces=NS) == str(PYW)
    assert root.findtext("t:Actions/t:Exec/t:Arguments", namespaces=NS) == "-m clipbridge run"


def test_task_xml_escapes_paths_and_user() -> None:
    nasty = Path(r"C:\R&D <x>\it's \"q\"\pythonw.exe")
    root = _parse(install.task_xml(nasty, "DOM&AIN\\o'brien<>"))
    assert root.findtext("t:Actions/t:Exec/t:Command", namespaces=NS) == str(nasty)
    assert root.findtext("t:Principals/t:Principal/t:UserId", namespaces=NS) == "DOM&AIN\\o'brien<>"


# -- small helpers -----------------------------------------------------------
def test_pythonw_path_prefers_pythonw_next_to_interpreter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "python.exe").write_bytes(b"")
    monkeypatch.setattr(install.sys, "executable", str(tmp_path / "python.exe"))
    assert install.pythonw_path() == tmp_path / "python.exe"
    (tmp_path / "pythonw.exe").write_bytes(b"")
    assert install.pythonw_path() == tmp_path / "pythonw.exe"


def test_user_has_no_leading_backslash_without_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install.getpass, "getuser", lambda: "me")
    monkeypatch.delenv("USERDOMAIN", raising=False)
    assert install._user() == "me"
    monkeypatch.setenv("USERDOMAIN", "PC")
    assert install._user() == "PC\\me"


class Runner:
    """Records `_run` calls; `fail` lists substrings of command lines that exit 1."""

    def __init__(self, fail: tuple[str, ...] = ()) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        code = 1 if any(f in " ".join(args) for f in self.fail) else 0
        return subprocess.CompletedProcess(args, code, "", "boom" if code else "")


def test_run_captures_text(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update(args=args, **kwargs)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(install, "subprocess", SimpleNamespace(run=fake, CompletedProcess=subprocess.CompletedProcess))
    install._run(["x", "y"])
    assert seen == {"args": ["x", "y"], "capture_output": True, "text": True}


def test_elevated_runs_command_through_runas_and_reports_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Runner()
    monkeypatch.setattr(install, "_run", run)
    assert install._elevated("netsh", 'advfirewall firewall delete rule name="ClipBridge"') is True
    (call,) = run.calls
    assert call[:3] == ["powershell", "-NoProfile", "-Command"]
    script = call[3]
    assert "-FilePath 'netsh'" in script and "-Verb RunAs" in script and "-Wait" in script
    assert "exit $p.ExitCode" in script and 'name="ClipBridge"' in script
    monkeypatch.setattr(install, "_run", Runner(fail=("powershell",)))
    assert install._elevated("cmd", "/c x") is False


def test_task_registered_and_firewall_rule_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Runner()
    monkeypatch.setattr(install, "_run", run)
    assert install.task_registered() and install._firewall_rule_exists()
    assert run.calls == [
        ["schtasks", "/Query", "/TN", "ClipBridge"],
        ["netsh", "advfirewall", "firewall", "show", "rule", "name=ClipBridge"],
    ]
    monkeypatch.setattr(install, "_run", Runner(fail=("schtasks", "netsh")))
    assert not install.task_registered() and not install._firewall_rule_exists()


# -- registry verb (in-memory winreg) ------------------------------------------
class FakeWinreg:
    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, str]] = {}

    def CreateKeyEx(self, root: str, path: str, reserved: int, access: int) -> Any:
        assert root == "HKCU"
        parts = path.split("\\")
        for i in range(1, len(parts) + 1):
            self.keys.setdefault("\\".join(parts[:i]), {})
        values = self.keys[path]

        class Key:
            def __enter__(self_: Any) -> Any:
                return values

            def __exit__(self_: Any, *a: object) -> None:
                pass

        return Key()

    def SetValueEx(self, key: dict[str, str], name: str, reserved: int, kind: int, value: str) -> None:
        assert kind == self.REG_SZ
        key[name] = value

    def DeleteKey(self, root: str, path: str) -> None:
        if path not in self.keys:
            raise FileNotFoundError(path)
        if any(k.startswith(path + "\\") for k in self.keys):
            raise PermissionError(path)  # like the real API: no subkeys allowed
        del self.keys[path]

    def OpenKey(self, root: str, path: str) -> str:
        if path not in self.keys:
            raise FileNotFoundError(path)
        return path

    def CloseKey(self, key: str) -> None:
        pass


@pytest.fixture
def reg(monkeypatch: pytest.MonkeyPatch) -> FakeWinreg:
    fake = FakeWinreg()
    monkeypatch.setattr(install, "winreg", fake)
    return fake


def test_verb_write_detect_delete_round_trip(reg: FakeWinreg) -> None:
    assert not install._verb_present()
    assert install._delete_verb() is False
    install._write_verb(install.verb_values(PYW))
    assert install._verb_present()
    assert reg.keys[install.VERB_KEY][""] == "Send to phone"
    assert reg.keys[install.VERB_KEY + r"\command"][""].endswith(' -m clipbridge send "%1"')
    assert install._delete_verb() is True
    assert reg.keys == {r"Software": {}, r"Software\Classes": {}, r"Software\Classes\*": {}, r"Software\Classes\*\shell": {}}
    assert not install._verb_present() and install._delete_verb() is False


def test_write_shortcut_creates_loadable_lnk_in_tmp(tmp_path: Path) -> None:
    import pythoncom
    from win32com.shell import shell

    spec = install.shortcut_spec(PYW, tmp_path)
    install._write_shortcut(spec)
    assert spec.path.is_file()
    link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
    link.QueryInterface(pythoncom.IID_IPersistFile).Load(str(spec.path))
    assert link.GetArguments() == "-m clipbridge send"
    assert link.GetDescription() == spec.description


# -- install() ----------------------------------------------------------------
class World:
    """Everything install()/uninstall() touch, faked and recorded in call order."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail: tuple[str, ...] = ()) -> None:
        self.events: list[str] = []
        self.commands: list[list[str]] = []
        self.elevated_ok = True
        self.verb = False
        self.shortcut = tmp_path / "SendTo" / install.SHORTCUT_NAME
        self.shortcut.parent.mkdir()
        self.fail = fail
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        monkeypatch.setattr(install, "pythonw_path", lambda: PYW)
        monkeypatch.setattr(install, "_user", lambda: r"PC\me")
        monkeypatch.setattr(install, "_sendto_dir", lambda: self.shortcut.parent)
        monkeypatch.setattr(install, "_run", self.run)
        monkeypatch.setattr(install, "_write_verb", self.write_verb)
        monkeypatch.setattr(install, "_delete_verb", self.delete_verb)
        monkeypatch.setattr(install, "_write_shortcut", self.write_shortcut)
        monkeypatch.setattr(install, "_elevated", self.elevated)
        monkeypatch.setattr(install, "_firewall_rule_exists", lambda: True)

    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(args)
        self.events.append(f"{args[0]} {args[1]}")
        code = 1 if any(f in " ".join(args) for f in self.fail) else 0
        return subprocess.CompletedProcess(args, code, "", "denied" if code else "")

    def write_verb(self, values: list[install.RegValue]) -> None:
        self.events.append("verb+")
        self.verb = True

    def delete_verb(self) -> bool:
        self.events.append("verb-")
        existed, self.verb = self.verb, False
        return existed

    def write_shortcut(self, spec: install.ShortcutSpec) -> None:
        self.events.append("shortcut+")
        spec.path.write_bytes(b"lnk")

    def elevated(self, command: str, args: str) -> bool:
        self.events.append(f"elevated {command}")
        return self.elevated_ok


def test_install_order_and_task_xml_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path, monkeypatch)
    seen_xml: list[str] = []
    real_run = world.run

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        path = Path(args[args.index("/XML") + 1])
        seen_xml.append(path.read_bytes().decode("utf-16"))
        return real_run(args)

    monkeypatch.setattr(install, "_run", run)
    assert install.install() == 0
    assert world.events == ["schtasks /Create", "verb+", "shortcut+", "elevated cmd"]
    assert world.commands[0][:6] == ["schtasks", "/Create", "/TN", "ClipBridge", "/XML", world.commands[0][5]]
    assert world.commands[0][-1] == "/F"
    assert "-m clipbridge run" in seen_xml[0]
    assert not Path(world.commands[0][5]).exists()  # temp XML cleaned up
    assert "Firewall rule 'ClipBridge' added" in capsys.readouterr().out


def test_install_stops_when_task_registration_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path, monkeypatch, fail=("/Create",))
    assert install.install() == 1
    assert world.events == ["schtasks /Create"]
    assert "Task Scheduler registration failed" in capsys.readouterr().out
    assert list(tmp_path.glob("*.xml")) == []


def test_install_uac_declined_returns_1_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path, monkeypatch)
    world.elevated_ok = False
    assert install.install() == 1
    assert "Firewall rule NOT added" in capsys.readouterr().out


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="bug: install() returns 1 when UAC is declined (install.py:197-199) but leaves the task, verb and shortcut in place",
)
def test_install_uac_declined_rolls_back_task_verb_and_shortcut(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(tmp_path, monkeypatch)
    world.elevated_ok = False
    assert install.install() == 1
    assert ["schtasks", "/Delete", "/TN", "ClipBridge", "/F"] in world.commands
    assert world.verb is False
    assert not world.shortcut.exists()


# -- uninstall() --------------------------------------------------------------
def _installed(world: World) -> None:
    world.verb = True
    world.shortcut.write_bytes(b"lnk")


def test_uninstall_removes_everything_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path, monkeypatch)
    _installed(world)
    assert install.uninstall() == 0
    assert world.events == ["schtasks /End", "schtasks /Delete", "verb-", "elevated netsh"]
    assert not world.verb and not world.shortcut.exists()
    out = capsys.readouterr().out
    assert "Task removed." in out and "Firewall rule removed." in out and "SendTo shortcut removed." in out


def test_uninstall_when_nothing_installed_reports_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path, monkeypatch, fail=("schtasks",))
    monkeypatch.setattr(install, "_firewall_rule_exists", lambda: False)
    assert install.uninstall() == 0
    assert "elevated netsh" not in world.events  # no UAC prompt when there is no rule
    out = capsys.readouterr().out
    assert "Task not found" in out and "verb not found" in out and "SendTo shortcut not found" in out


def test_uninstall_uac_declined_returns_1_but_still_removes_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path, monkeypatch)
    _installed(world)
    world.elevated_ok = False
    assert install.uninstall() == 1
    assert not world.verb and not world.shortcut.exists()
    assert "Firewall rule NOT removed" in capsys.readouterr().out


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    roaming, local = tmp_path / "Roaming", tmp_path / "Local"
    (roaming / "ClipBridge").mkdir(parents=True)
    (local / "ClipBridge" / "logs").mkdir(parents=True)
    (local / "ClipBridge" / "partial").mkdir()
    (local / "ClipBridge" / "partial" / "x.part").write_bytes(b"half")
    received = tmp_path / "Downloads" / "ClipShare"
    pictures = tmp_path / "Pictures" / "ClipBridge"
    for d in (received, pictures):
        d.mkdir(parents=True)
        (d / "keep.bin").write_bytes(b"mine")
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    config.DeviceRegistry().pair("android-1")  # roaming/ClipBridge/devices.bin
    return SimpleNamespace(roaming=roaming, local=local, received=received, pictures=pictures)


def test_uninstall_without_purge_keeps_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tree: SimpleNamespace) -> None:
    World(tmp_path, monkeypatch)
    assert install.uninstall() == 0
    assert (tree.roaming / "ClipBridge" / "devices.bin").exists() and (tree.local / "ClipBridge").exists()


def test_uninstall_purge_deletes_app_data_but_not_received_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tree: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    World(tmp_path, monkeypatch)
    assert install.uninstall(purge=True) == 0
    assert not (tree.roaming / "ClipBridge").exists() and not (tree.local / "ClipBridge").exists()
    assert (tree.received / "keep.bin").read_bytes() == b"mine"
    assert (tree.pictures / "keep.bin").read_bytes() == b"mine"
    assert tree.roaming.exists() and tree.local.exists()  # only the ClipBridge subfolders go
    assert "were kept" in capsys.readouterr().out


def test_uninstall_purge_with_missing_dirs_is_fine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    World(tmp_path, monkeypatch)
    monkeypatch.setenv("APPDATA", str(tmp_path / "nope1"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "nope2"))
    assert install.uninstall(purge=True) == 0


def test_uninstall_purge_reports_undeletable_dir_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tree: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    World(tmp_path, monkeypatch)
    real = install.shutil.rmtree

    def rmtree(path: Path, *a: Any, **k: Any) -> None:
        if Path(path) == tree.roaming / "ClipBridge":
            raise PermissionError(13, "Access is denied")
        real(path, *a, **k)

    monkeypatch.setattr(install.shutil, "rmtree", rmtree)
    assert install.uninstall(purge=True) == 1
    assert (tree.roaming / "ClipBridge" / "devices.bin").exists()
    assert not (tree.local / "ClipBridge").exists()  # the next directory was still purged
    assert "Could not delete" in capsys.readouterr().out


# -- status() ---------------------------------------------------------------------
@pytest.mark.parametrize("on", [True, False])
def test_status_reports_each_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tree: SimpleNamespace, capsys: pytest.CaptureFixture[str], on: bool
) -> None:
    world = World(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "is_running", lambda: on)
    monkeypatch.setattr(install, "task_registered", lambda: on)
    monkeypatch.setattr(install, "_firewall_rule_exists", lambda: on)
    monkeypatch.setattr(install, "_verb_present", lambda: on)
    if on:
        world.shortcut.write_bytes(b"lnk")
    else:
        (tree.roaming / "ClipBridge" / "devices.bin").unlink()
    assert install.status() == 0
    lines = dict(line.split(":", 1) for line in capsys.readouterr().out.splitlines())
    values = {k.strip(): v.strip() for k, v in lines.items()}
    assert values["running"] == str(on)
    assert values["task"] == ("registered" if on else "not registered")
    assert values["firewall rule"] == ("present" if on else "missing")
    assert values["context menu"] == ("present" if on else "missing")
    assert values["SendTo"] == ("present" if on else "missing")
    assert values["devices"] == ("1" if on else "none (run pair)")
    assert values["log"] == str(tree.local / "ClipBridge" / "logs" / "clipbridge.log")
