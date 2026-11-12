"""Task Scheduler task + firewall rule (install / uninstall) and status checks."""

from __future__ import annotations

import getpass
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

TASK_NAME = "ClipBridge"
FIREWALL_RULE = "ClipBridge"
PORT = 8765


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


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True)


def _elevated(command: str, args: str) -> bool:
    """Run a command through one UAC prompt; True if it exited 0."""
    ps = (
        f"$p = Start-Process -FilePath '{command}' -ArgumentList '{args}' "
        "-Verb RunAs -Wait -PassThru -WindowStyle Hidden; exit $p.ExitCode"
    )
    return _run(["powershell", "-NoProfile", "-Command", ps]).returncode == 0


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
    print("Next: a Windows UAC prompt will appear to add the firewall rule. Please approve it.")
    rule = (
        f'advfirewall firewall add rule name="{FIREWALL_RULE}" dir=in action=allow '
        f"protocol=TCP localport={PORT} profile=private,public"
    )
    if not _elevated("netsh", rule):
        print("Firewall rule NOT added (UAC declined or failed). Re-run install.")
        return 1
    print(f"Firewall rule '{FIREWALL_RULE}' added for TCP {PORT}.")
    return 0


def uninstall() -> int:
    code = 0
    result = _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    print("Task removed." if result.returncode == 0 else "Task not found / not removed.")
    if _firewall_rule_exists():
        print("Next: a Windows UAC prompt will appear to remove the firewall rule. Please approve it.")
        if _elevated("netsh", f'advfirewall firewall delete rule name="{FIREWALL_RULE}"'):
            print("Firewall rule removed.")
        else:
            print("Firewall rule NOT removed (UAC declined or failed).")
            code = 1
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
    print(f"PSK:           {'configured' if config.load_psk() else 'not generated (run pair)'}")
    print(f"log:           {config.log_dir() / 'clipbridge.log'}")
    return 0
