package com.clipbridge

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.IBinder
import android.util.Log
import com.clipbridge.features.clipboard.ClipboardFeature
import com.clipbridge.features.clipboard.ClipboardWriter
import com.clipbridge.features.clipboard.ReaderActivityTrigger
import com.clipbridge.link.BridgeClient
import com.clipbridge.link.Discovery
import com.clipbridge.link.Endpoint
import com.clipbridge.link.FeatureRouter
import com.clipbridge.link.LinkState
import com.clipbridge.link.NetworkWatcher
import com.clipbridge.link.Status
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

/** Foreground service (connectedDevice): composition root. Owns link + features. */
class BridgeService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private lateinit var settings: Settings
    private lateinit var discovery: Discovery
    private lateinit var network: NetworkWatcher
    private lateinit var client: BridgeClient
    private lateinit var clipboard: ClipboardFeature

    override fun onCreate() {
        super.onCreate()
        settings = Settings(this)
        createChannel()
        startForeground(NOTIF_ID, notification(Status(LinkState.SEARCHING)), ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)

        discovery = Discovery(this)
        val router = FeatureRouter { Log.w(TAG, it) }
        client = BridgeClient(
            scope = scope,
            deviceId = settings.deviceId,
            pskProvider = { settings.psk },
            endpointsProvider = { Endpoint.order(settings.manual, discovery.found.value, settings.cached) },
            router = router,
            onGoodEndpoint = { settings.cached = it },
            log = { Log.i(TAG, it) },
        )
        clipboard = ClipboardFeature(client, ClipboardWriter(this))
        router.register(clipboard)

        network = NetworkWatcher(this, onAvailable = { client.retryNow() }, onLost = { client.networkLost() })
        network.start()
        discovery.start()
        scope.launch { discovery.found.collect { if (it != null) client.retryNow() } }
        scope.launch {
            client.status.collect {
                status.value = it
                getSystemService(NotificationManager::class.java).notify(NOTIF_ID, notification(it))
            }
        }
        client.start()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_SEND_TEXT -> intent.getStringExtra(EXTRA_TEXT)?.let { text ->
                scope.launch { clipboard.onLocalClip(text) }
            }
            ACTION_RECONFIG -> client.reconnect()
        }
        return START_STICKY
    }

    override fun onDestroy() {
        network.stop(); discovery.stop(); client.stop(); scope.cancel()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun createChannel() {
        val ch = NotificationChannel(CHANNEL, "ClipBridge link", NotificationManager.IMPORTANCE_LOW).apply {
            setShowBadge(false); setSound(null, null); enableVibration(false)
        }
        getSystemService(NotificationManager::class.java).createNotificationChannel(ch)
    }

    private fun notification(s: Status): Notification {
        val send = PendingIntent.getActivity(
            this, 0, ReaderActivityTrigger.readerIntent(this),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val open = PendingIntent.getActivity(
            this, 1, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_tile)
            .setContentTitle("ClipBridge")
            .setContentText(describe(s))
            .setContentIntent(open)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .addAction(Notification.Action.Builder(null, "Send clipboard", send).build())
            .build()
    }

    companion object {
        private const val TAG = "ClipBridge"
        private const val CHANNEL = "link"
        private const val NOTIF_ID = 1
        const val ACTION_SEND_TEXT = "com.clipbridge.SEND_TEXT"
        const val ACTION_RECONFIG = "com.clipbridge.RECONFIG"
        const val EXTRA_TEXT = "text"

        val status = MutableStateFlow(Status(LinkState.SEARCHING)).also { }
        val statusFlow: StateFlow<Status> get() = status

        fun describe(s: Status): String = when (s.state) {
            LinkState.NOT_PAIRED -> "Not paired"
            LinkState.SEARCHING -> "Searching for PC" + if (s.detail.isNotEmpty()) " (${s.detail})" else ""
            LinkState.CONNECTING -> "Connecting to ${s.detail}"
            LinkState.CONNECTED -> "Connected to ${s.detail}"
        }

        fun start(context: Context) {
            context.startForegroundService(Intent(context, BridgeService::class.java))
        }

        /** Hand a local clip to the running service (starts it if needed). */
        fun submit(context: Context, text: String) {
            context.startForegroundService(
                Intent(context, BridgeService::class.java).setAction(ACTION_SEND_TEXT).putExtra(EXTRA_TEXT, text)
            )
        }

        fun reconfigure(context: Context) {
            context.startForegroundService(Intent(context, BridgeService::class.java).setAction(ACTION_RECONFIG))
        }
    }
}
