# E2E results (step 5)

Device: vivo V2502, Android 16. Server on Windows, LAN 192.168.1.x.

| # | Item | Result | Notes |
|---|------|--------|-------|
| 1 | Start server, `pair`, phone connects | PASS | connected ~5 s after pair |
| 2 | PC -> phone text incl. emoji | PASS | 3/3 landed; phone log within the same second as PC send (phone/PC clocks skewed, so no precise ms figure) |
| 3a | Phone -> PC via QS tile | PASS | tile must be added via the QS edit screen |
| 3b | Phone -> PC via notification action | PASS | |
| 3c | Phone -> PC via share sheet | PASS | |
| 3d | No ping-pong | PASS | no "local clip" on PC after receiving a phone clip |
| 4 | Wrong-PSK client rejected | PASS | close code 4401, log "auth failed (bad MAC)" |
| 5a | Phone Wi-Fi off/on reconnect | PASS | ~5 s after enable |
| 5b | Server restart, phone reconnects | PASS | ~1 s |
| 6a | `clipbridge install` (task + firewall) | PASS | user ran it; task registered, firewall rule present, `schtasks /Run` starts pythonw daemon, phone connects |
| 6b | Phone reboot auto-start | PASS | service up and connected ~30 s after boot (vivo background power usage allowed) |

Fix made: Android now logs clip send/receive (length + hash only, tag `ClipBridge`) so e2e is observable (commit "Android: log clip send/receive").

Gotcha: the Claude desktop tool shell virtualizes %APPDATA%/%LOCALAPPDATA%, so `pair` run from it writes a different PSK than the real daemon uses (symptom: endless "bad MAC"). Run `pair` from a normal terminal.

Not tested: PC sleep/resume, PC reboot/logon trigger, kill-pythonw restart.

Earlier observation (probably the same PSK mismatch): one unexplained "auth failed (bad MAC)" from the phone at 14:30:44 while the user was toggling Wi-Fi; not reproduced in a controlled Wi-Fi toggle. Self-recovered.

## Images, share target, screenshot tile (2026-11-12)
Setup: merged `claude/win-images-*` and `claude/android-images-*`; Pillow installed in `windows/.venv` (`pip install -e windows`); APK via `setup.ps1`; `pm grant com.clipbridge android.permission.READ_MEDIA_IMAGES`.

- Phone to PC, share sheet (adb `SEND image/jpeg` of the newest screenshot, 1080p JPEG): phone logged `send image len=251788`; PC saved `Pictures\ClipBridge\clip_20261112_152208.png` (251 788 bytes) and set the clipboard. No echo back to the phone. PASS
- PC to phone: image set on the Windows clipboard (320x200 PNG). Phone logged `remote image len=672 applied=true`; `cache/clips/latest.png` written. No echo back. PASS
- Text sync still works after the merge (phone received a 455-char PC clip). PASS

Not tested yet (needs hands on the phone): pasting the received image into an app; "Send screenshot" tile (must be added to the QS panel first, `cmd statusbar click-tile` did nothing when it was not added); share from a real app UI; 10 MB photo; >16 MiB skip.

Note: .NET `Clipboard.SetImage` threw "Requested Clipboard operation did not succeed" while the data was still delivered; likely the daemon opening the clipboard during the setter's flush. Watch for this with real apps.
Gotcha: the tool shell's `%LOCALAPPDATA%` is virtualized too, so the real daemon log cannot be read from it.

## Automated device run (step 13, 2026-11-16)
`windows/e2e/device.py` (full sizes) and `--pairing`, from a normal terminal, vivo V2502 on USB, same 5 GHz Wi-Fi,
debug APK built from the step 13 commit.

| # | Scenario | Result | Notes |
|---|---|---|---|
| 1 | PC→phone 10 KB | PASS | |
| 2 | PC→phone 1 GB | PASS | 18.6 MB/s |
| 3 | Phone→PC photo 4 MB | PASS | |
| 4 | Phone→PC 5 photos + PDF, one share | PASS | 20 MB, 5.0 MB/s |
| 5 | PC→phone mixed small + 200 MB | PASS | |
| 6 | Empty file both ways | PASS | |
| 7 | Name clash ` (1)`, both ways, different content | PASS | |
| 8 | Clips during a 1 GB transfer | PASS | 15 ms PC→phone, 32 ms phone→PC, transfer moving at each clip |
| 9 | Small file during a big one, opposite directions | FAIL once, then PASS twice | first run: the 1 GB PC→phone transfer did not start within 30 s; `--only 9` and `--only 8,9` passed (2 GB, ~14 MB/s); not reproduced |
| 10 | Wi-Fi off/on mid-transfer | PASS | had 20 % at drop, resumed at 20 % |
| 11 | No clip ping-pong | PASS | phone and PC logs clean |
| 12 | Small during big, same direction | XFAIL (step 14) | small started 0.1 s after the big one finished, 46 s after it was sent |
| 13 | PC server restart | PASS | link back 2.5 s after task start |
| 14 | App force-stop / `am kill` | PASS | link 1.8 s after relaunch; survives `am kill` |
| 15 | Idle Wi-Fi off/on | PASS | reconnect 10.0 s |
| 16 | PC clip right after the phone vanished | XFAIL (step 14) | PC log shows the clip sent to the still-held dead peer; never arrived |
| 16b | Offline clip flush | PASS | PC dropped the peer 19 s after Wi-Fi off; only the latest clip arrived |
| 17 | Connected state + hello | PASS | |
| 18 | Dead manual host | PASS | dead dial seen; link 5.0 s after Wi-Fi on |
| 19 | Wrong PSK | PASS | PC log shows the rejection |
| 20 | Unknown device | PASS | PC log shows the rejection |

Observed: while rejected (19, 20) the phone was dialling the PC's link-local address `169.254.171.177`, which the PC
advertises alongside its LAN address (on purpose, for router-less links); see "Known bugs" in `docs/PLAN.md`.

## Automated device run (step 14, 2026-11-16)
`windows/e2e/device.py` (full sizes, no `--pairing`), same setup, debug APK and PC server from the step 14 commit.
All scenarios not listed here: PASS, as in step 13.

| # | Scenario | Result | Notes |
|---|---|---|---|
| 2 | PC→phone 1 GB | PASS | 9.0 MB/s |
| 9 | Small file during a big one, opposite directions | PASS | big proven in flight before and after the small file, both directions |
| 12 | Small during big, same direction | PASS | `--only 12`; small transfer started while the 1 GB one was still running |
| 16 | PC clip right after the phone vanished | PASS | the clip set while the PC still held the dead peer arrived after reconnect |
| 16b | Offline clip flush | PASS | only the latest offline clip arrived; reconnect 4.2 s |

Gotcha: `Start-ScheduledTask ClipBridge` does nothing while the task is already running, so the first full run's
scenarios 1-12 hit the old server (12 failed as FIFO) until scenario 13 restarted it. Stop the `clipbridge run`
processes first, then start the task.
