"""CLI: run | pair | send | install | uninstall | status."""

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


def _report(text: str) -> None:
    """Print, or show a message box when started by pythonw (Explorer) without a console."""
    if sys.stdout is not None:
        print(text)
        return
    import win32api
    import win32con

    win32api.MessageBox(0, text, "ClipBridge", win32con.MB_OK | win32con.MB_ICONWARNING)


def send(args: list[str]) -> int:
    from clipbridge.features.files import pipe

    files, errors = pipe.validate_paths(args, Path.cwd())
    if files:
        try:
            pipe.send_request(files)
        except pipe.ServerNotRunning:
            errors.append("ClipBridge is not running, so nothing was sent. Start it (task 'ClipBridge') and try again.")
            files = []
    if errors:
        _report("\n".join(errors))
    return 0 if files and len(files) == len(args) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clipbridge")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run the server")
    sub.add_parser("pair", help="push the key to the phone via adb and print it")
    send_parser = sub.add_parser("send", help="send files to the phone through the running server")
    send_parser.add_argument("files", nargs="+")
    sub.add_parser("install", help="register task, firewall rule (UAC prompt), Explorer 'Send to phone'")
    uninstall_parser = sub.add_parser("uninstall", help="remove everything install added (UAC prompt)")
    uninstall_parser.add_argument(
        "--purge", action="store_true", help="also delete config, key, logs and unfinished transfers"
    )
    sub.add_parser("status", help="show running/installed state")
    args = parser.parse_args(argv)
    command = args.command

    if command == "run":
        from clipbridge import app

        return app.run()
    if command == "pair":
        return pair()
    if command == "send":
        return send(args.files)
    from clipbridge import install

    if command == "uninstall":
        return install.uninstall(purge=args.purge)
    return {"install": install.install, "status": install.status}[command]()

if __name__ == "__main__":
    sys.exit(main())
