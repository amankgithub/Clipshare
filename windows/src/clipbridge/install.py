"""Install / uninstall everything ClipBridge adds to Windows (see docs/FOOTPRINT.md) and status checks.

Task Scheduler task, firewall rule (one UAC prompt), and the per-user Explorer entry points for
sending files: the "Send to phone" context-menu verb and the SendTo shortcut.
"""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
import tempfile
import winreg
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

TASK_NAME = "ClipBridge"
FIREWALL_RULE = "ClipBridge"
PORT = 8765
VERB_KEY = r"Software\Classes\*\shell\ClipBridge"
VERB_TEXT = "Send to phone"
SHORTCUT_NAME = "Phone (ClipBridge).lnk"
SEND_ARGS = "-m clipbridge send"


def pythonw_path() -> Path:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return candidate if candidate.exists() else exe


def _user() -> str:
    return f"{os.environ.get('USERDOMAIN', '')}\\{getpass.getuser()}".lstrip("\\")


def task_xml(pythonw: Path, user: str) -> str:
    u = escape(user)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>ClipBridge clipboard sync server</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>{u}</UserId></LogonTrigger>
    <SessionStateChangeTrigger>
      <Enabled>true</Enabled><StateChange>SessionUnlock</StateChange><UserId>{u}</UserId>
    </SessionStateChangeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><UserId>{u}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{escape(str(pythonw))}</Command><Arguments>-m clipbridge run</Arguments></Exec>
  </Actions>
</Task>
"""


@dataclass(frozen=True)
class RegValue:
    key: str  # under HKEY_CURRENT_USER
    name: str  # "" = the key's default value
    value: str


@dataclass(frozen=True)
class ShortcutSpec:
    path: Path
    target: Path
    arguments: str
    description: str


def verb_values(pythonw: Path) -> list[RegValue]:
    """Registry values of the context-menu verb (one process per selected file)."""
    return [
        RegValue(VERB_KEY, "", VERB_TEXT),
        RegValue(VERB_KEY, "MultiSelectModel", "Player"),
        RegValue(VERB_KEY, "Icon", str(pythonw)),
        RegValue(VERB_KEY + r"\command", "", f'"{pythonw}" {SEND_ARGS} "%1"'),
    ]


def shortcut_spec(pythonw: Path, sendto_dir: Path) -> ShortcutSpec:
    """SendTo shortcut; Explorer appends all selected files as arguments."""
    return ShortcutSpec(sendto_dir / SHORTCUT_NAME, pythonw, SEND_ARGS, "Send files to the phone with ClipBridge")


def purge_dirs() -> list[Path]:
    """Directories `uninstall --purge` deletes. Received files live elsewhere and are never touched."""
    from clipbridge import config

    return [config.config_dir(), Path(os.environ["LOCALAPPDATA"]) / "ClipBridge"]


def _sendto_dir() -> Path:
    from win32com.shell import shell, shellcon

    return Path(shell.SHGetFolderPath(0, shellcon.CSIDL_SENDTO, None, 0))


def _write_verb(values: list[RegValue]) -> None:
    for v in values:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, v.key, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, v.name, 0, winreg.REG_SZ, v.value)


def _delete_verb() -> bool:
    """Delete the verb key tree; True if it existed."""
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, VERB_KEY + r"\command")
    except FileNotFoundError:
        pass
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, VERB_KEY)
    except FileNotFoundError:
        return False
    return True


def _verb_present() -> bool:
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, VERB_KEY))
    except FileNotFoundError:
        return False
    return True


def _write_shortcut(spec: ShortcutSpec) -> None:
    import pythoncom
    from win32com.shell import shell

    link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
    link.SetPath(str(spec.target))
    link.SetArguments(spec.arguments)
    link.SetDescription(spec.description)
    link.SetIconLocation(str(spec.target), 0)
    link.QueryInterface(pythoncom.IID_IPersistFile).Save(str(spec.path), 0)


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True)


def _elevated(command: str, args: str) -> bool:
    """Run a command through one UAC prompt; True if it exited 0."""
    ps = (
        f"$p = Start-Process -FilePath '{command}' -ArgumentList '{args}' "
        "-Verb RunAs -Wait -PassThru -WindowStyle Hidden; exit $p.ExitCode"
    )
    return _run(["powershell", "-NoProfile", "-Command", ps]).returncode == 0


def firewall_args() -> str:
    """cmd.exe args: drop any rule of this name (netsh allows duplicates), then add the private-only one.

    The rule only admits the local subnet on private networks. The exit code is the add's.
    """
    delete = f'netsh advfirewall firewall delete rule name="{FIREWALL_RULE}"'
    add = (
        f'netsh advfirewall firewall add rule name="{FIREWALL_RULE}" dir=in action=allow '
        f"protocol=TCP localport={PORT} profile=private remoteip=localsubnet"
    )
    return f"/c {delete} & {add}"


def install() -> int:
    xml = task_xml(pythonw_path(), _user())
    with tempfile.NamedTemporaryFile("w", suffix=".xml", encoding="utf-16", delete=False) as f:
        f.write(xml)
        path = f.name
    try:
        result = _run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", path, "/F"])
    finally:
        os.unlink(path)
    if result.returncode != 0:
        print("Task Scheduler registration failed:", result.stderr.strip())
        return 1
    print(f"Task '{TASK_NAME}' registered (Logon + SessionUnlock).")
    pythonw = pythonw_path()
    _write_verb(verb_values(pythonw))
    print(f"Explorer context-menu verb '{VERB_TEXT}' added (HKCU).")
    spec = shortcut_spec(pythonw, _sendto_dir())
    _write_shortcut(spec)
    print(f"SendTo shortcut added: {spec.path}")
    print("Next: a Windows UAC prompt will appear to add the firewall rule. Please approve it.")
    if not _elevated("cmd", firewall_args()):
        print("Firewall rule NOT added (UAC declined or failed). Re-run install.")
        return 1
    print(f"Firewall rule '{FIREWALL_RULE}' added for TCP {PORT}.")
    return 0


def uninstall(purge: bool = False) -> int:
    code = 0
    _run(["schtasks", "/End", "/TN", TASK_NAME])  # stop a running server so its files can go
    result = _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    print("Task removed." if result.returncode == 0 else "Task not found / not removed.")
    print("Context-menu verb removed." if _delete_verb() else "Context-menu verb not found.")
    shortcut = shortcut_spec(pythonw_path(), _sendto_dir()).path
    if shortcut.exists():
        shortcut.unlink()
        print("SendTo shortcut removed.")
    else:
        print("SendTo shortcut not found.")
    if _firewall_rule_exists():
        print("Next: a Windows UAC prompt will appear to remove the firewall rule. Please approve it.")
        if _elevated("netsh", f'advfirewall firewall delete rule name="{FIREWALL_RULE}"'):
            print("Firewall rule removed.")
        else:
            print("Firewall rule NOT removed (UAC declined or failed).")
            code = 1
    if purge:
        for directory in purge_dirs():
            if not directory.exists():
                continue
            try:
                shutil.rmtree(directory)
                print(f"Deleted {directory}")
            except OSError as e:
                print(f"Could not delete {directory}: {e.strerror}")
                code = 1
        print(r"Received files (Downloads\ClipShare, Pictures\ClipBridge) were kept.")
    return code


def _firewall_rule_exists() -> bool:
    return _run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={FIREWALL_RULE}"]).returncode == 0


def task_registered() -> bool:
    return _run(["schtasks", "/Query", "/TN", TASK_NAME]).returncode == 0


def status() -> int:
    from clipbridge import app, config

    print(f"running:       {app.is_running()}")
    print(f"task:          {'registered' if task_registered() else 'not registered'}")
    print(f"firewall rule: {'present' if _firewall_rule_exists() else 'missing'}")
    print(f"context menu:  {'present' if _verb_present() else 'missing'}")
    shortcut = shortcut_spec(pythonw_path(), _sendto_dir()).path
    print(f"SendTo:        {'present' if shortcut.exists() else 'missing'}")
    print(f"devices:       {len(config.DeviceRegistry().load()) or 'none (run pair)'}")
    print(f"log:           {config.log_dir() / 'clipbridge.log'}")
    return 0
