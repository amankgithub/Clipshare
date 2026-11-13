package com.clipbridge

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings as AndroidSettings
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.clipbridge.link.Pairing

/** One-screen UI: status, manual host, pairing-code paste, permission checklist with fix buttons. */
class MainActivity : ComponentActivity() {
    private val tick = mutableIntStateOf(0)

    private val mediaPerm = registerForActivityResult(ActivityResultContracts.RequestPermission()) { tick.intValue++ }

    private val notifPerm = registerForActivityResult(ActivityResultContracts.RequestPermission()) { tick.intValue++ }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        BridgeService.start(this)
        val settings = Settings(this)
        setContent {
            val colors = if (isSystemInDarkTheme()) darkColorScheme() else lightColorScheme()
            MaterialTheme(colorScheme = colors) {
                Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) { Screen(settings) }
            }
        }
    }

    override fun onResume() {
        super.onResume()
        tick.intValue++
    }

    @Composable
    private fun Screen(settings: Settings) {
        val status by BridgeService.statusFlow.collectAsState()
        var host by remember { mutableStateOf(settings.manualHost) }
        var code by remember { mutableStateOf("") }
        var msg by remember { mutableStateOf("") }
        val t = tick.intValue // re-read checklist on resume

        Column(
            Modifier.verticalScroll(rememberScrollState()).padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Text("ClipBridge", style = MaterialTheme.typography.headlineSmall)
            Text(BridgeService.describe(status))
            Text(settings.pairing?.let { "Paired as ${it.device}" } ?: "Not paired")

            OutlinedTextField(host, { host = it }, Modifier.fillMaxWidth(), singleLine = true,
                label = { Text("PC host (optional, ip or ip:port)") })
            Button({ settings.manualHost = host; BridgeService.reconfigure(this@MainActivity) }) { Text("Save host") }

            OutlinedTextField(code, { code = it }, Modifier.fillMaxWidth(), singleLine = true,
                label = { Text("Paste pairing code (device:key)") })
            Button({
                val pairing = Pairing.parseCode(code)
                if (pairing != null) { settings.pairing = pairing; code = ""; msg = "Paired"; BridgeService.reconfigure(this@MainActivity) }
                else msg = "Invalid pairing code (need <device>:<32-byte base64 key>)"
            }) { Text("Save pairing") }
            if (msg.isNotEmpty()) Text(msg)

            Text("Permissions", style = MaterialTheme.typography.titleMedium)
            Check("Notifications", t.let { granted(Manifest.permission.POST_NOTIFICATIONS) }) {
                notifPerm.launch(Manifest.permission.POST_NOTIFICATIONS)
            }
            Check("Draw over other apps (needed to read clipboard in background)", AndroidSettings.canDrawOverlays(this@MainActivity)) {
                startActivity(Intent(AndroidSettings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName")))
            }
            Check("Photos access (for Send screenshot tile)",
                t.let { granted(com.clipbridge.features.clipboard.SendScreenshotTile.permission()) }) {
                mediaPerm.launch(com.clipbridge.features.clipboard.SendScreenshotTile.permission())
            }
            Check("Battery optimization exempt",
                getSystemService(PowerManager::class.java).isIgnoringBatteryOptimizations(packageName)) {
                startActivity(Intent(AndroidSettings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
            }
            Text("vivo: App info > Battery usage > Background power control > Allow background power usage.",
                style = MaterialTheme.typography.bodySmall)
        }
    }

    @Composable
    private fun Check(label: String, ok: Boolean, fix: () -> Unit) {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
            Text((if (ok) "[ok] " else "[  ] ") + label, Modifier.weight(1f))
            if (!ok) Button(fix) { Text("Fix") }
        }
    }

    private fun granted(p: String) = checkSelfPermission(p) == PackageManager.PERMISSION_GRANTED
}
