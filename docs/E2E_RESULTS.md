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
| 6a | `clipbridge install` (task + firewall) | NOT RUN | blocked by auto-mode classifier; user to run manually |
| 6b | Phone reboot auto-start | NOT RUN | needs vivo "Allow background power usage" first |

Fix made: Android now logs clip send/receive (length + hash only, tag `ClipBridge`) so e2e is observable (commit "Android: log clip send/receive").

Open observation: one unexplained "auth failed (bad MAC)" from the phone at 14:30:44 while the user was toggling Wi-Fi; not reproduced in a controlled Wi-Fi toggle. Self-recovered.
