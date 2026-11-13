# Build, install and prepare ClipBridge on the connected phone (adb, USB or wireless).
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$env:JAVA_HOME = 'C:\Program Files\Android\Android Studio\jbr'   # system Java is too new for AGP

& "$root\gradlew.bat" -p $root :app:assembleDebug
if ($LASTEXITCODE -ne 0) { throw 'build failed' }

adb install -r "$root\app\build\outputs\apk\debug\app-debug.apk"
if ($LASTEXITCODE -ne 0) { throw 'install failed' }

adb shell appops set com.clipbridge SYSTEM_ALERT_WINDOW allow
adb shell dumpsys deviceidle whitelist +com.clipbridge
adb shell am start -n com.clipbridge/.MainActivity   # opens the app and starts the service

Write-Host ''
Write-Host 'Manual vivo step (needed or BOOT_COMPLETED is not delivered):'
Write-Host '  Settings > Apps > ClipBridge > Battery usage > Background power control > "Allow background power usage"'
Write-Host ''
Write-Host 'Pair from the PC:  clipbridge pair   (gives this phone its device id and key; or paste the pairing code it prints into the app)'
