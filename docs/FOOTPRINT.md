# What ClipBridge puts on your devices

Every file, setting and dependency ClipBridge adds, and how to remove it. Anything new must be
listed here (see `CLAUDE.md`).

## Laptop (Windows)

| Item | Where | Removed by |
|---|---|---|
| Source code + Python dependencies (`websockets`, `zeroconf`, `pywin32`, `cryptography`, `Pillow`, dev: `pytest`, `pytest-asyncio`) | this repo folder; dependencies only in `windows\.venv` | deleting the repo folder |
| Scheduled task `ClipBridge` (starts the server at logon / unlock) | Task Scheduler | `clipbridge uninstall` |
| Firewall rule `ClipBridge` (inbound TCP 8765, Private profile, local subnet) | Windows Defender Firewall | `clipbridge uninstall` (one UAC prompt) |
| Context-menu verb "Send to phone" | `HKCU\Software\Classes\*\shell\ClipBridge` | `clipbridge uninstall` |
| SendTo shortcut "Phone (ClipBridge)" | `shell:sendto` (`%APPDATA%\Microsoft\Windows\SendTo`) | `clipbridge uninstall` |
| Config + DPAPI-encrypted key | `%APPDATA%\ClipBridge` | `clipbridge uninstall --purge` |
| Logs + unfinished transfers | `%LOCALAPPDATA%\ClipBridge` | `clipbridge uninstall --purge` |
| Android build cache (only if you build the APK) | `%USERPROFILE%\.gradle` (shared with other Android projects) | delete manually if nothing else uses it |
| **Your data**: received files, received clipboard images | `Downloads\ClipShare`, `Pictures\ClipBridge` | never deleted automatically |

Not installed by ClipBridge (pre-existing tools it uses): Python 3.10, Android Studio + SDK, adb.

### Full removal
Run this from the repo, using the venv's Python:

```
windows\.venv\Scripts\python.exe -m clipbridge uninstall --purge
```

Then delete the repo folder. Keep or delete `Downloads\ClipShare` and `Pictures\ClipBridge` yourself.

## Phone (Android)

| Item | Removed by |
|---|---|
| App `com.clipbridge`, its settings, key and unfinished transfers | uninstalling the app |
| adb grants from setup (`READ_LOGS`, overlay app-op, battery-optimization whitelist) | uninstalling the app |
| **Your data**: received files in `Downloads/ClipShare` | never deleted automatically |
