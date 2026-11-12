"""CLI: run | pair | install | uninstall | status."""

from __future__ import annotations

import argparse
import base64
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from clipbridge import config

ANDROID_ACTIVITY = "com.clipbridge/.ui.PairActivity"


def _primary_ip() -> str:
    """LAN address used for the default route (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))
        return str(s.getsockname()[0])


def _find_adb() -> str | None:
    found = shutil.which("adb")
    if found:
        return found
    roots = [os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT")]
    roots.append(str(Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk"))
    for root in filter(None, roots):
        candidate = Path(root) / "platform-tools" / "adb.exe"
        if candidate.exists():
            return str(candidate)
    return None


def pair() -> int:
    psk_b64 = base64.b64encode(config.get_or_create_psk()).decode("ascii")
    host = _primary_ip()
    print(f"PSK (b64): {psk_b64}\nHost:      {host}")
    adb = _find_adb()
    if adb is None:
        print("adb not found; enter the key and host in the app manually.")
        return 1
    cmd = [adb, "shell", "am", "start", "-n", ANDROID_ACTIVITY, "--es", "psk", psk_b64, "--es", "host", host]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or "Error" in result.stdout:
        print("adb pairing failed:", (result.stdout + result.stderr).strip())
        return 1
    print("Pushed to phone.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clipbridge")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, text in [
        ("run", "run the server"),
        ("pair", "push the key to the phone via adb and print it"),
        ("install", "register Task Scheduler task + firewall rule (UAC prompt)"),
        ("uninstall", "remove task and firewall rule (UAC prompt)"),
        ("status", "show running/installed state"),
    ]:
        sub.add_parser(name, help=text)
    command = parser.parse_args(argv).command

    if command == "run":
        from clipbridge import app

        return app.run()
    if command == "pair":
        return pair()
    from clipbridge import install

    return {"install": install.install, "uninstall": install.uninstall, "status": install.status}[command]()


if __name__ == "__main__":
    sys.exit(main())
