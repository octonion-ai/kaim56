// kAIm56 KatAgent — Android client for the kAIm56 agent platform
// Copyright (C) 2026 Ulrich Neidel
// SPDX-License-Identifier: AGPL-3.0-or-later
package de.kat56.agent

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.launch
import java.io.File

/** Download-Zustand, den die UI beobachtet (auch nach Wieder-Öffnen der App). */
sealed class Dl {
    data object Idle : Dl()
    data class Progress(val done: Long, val total: Long) : Dl()
    data class Done(val path: String) : Dl()
    data class Error(val msg: String) : Dl()
}

object DownloadBus {
    val state = MutableStateFlow<Dl>(Dl.Idle)
}

/** Foreground-Service: laedt das Modell im Hintergrund weiter, auch bei
 *  gesperrtem Display / geschlossener App. Zeigt eine Fortschritts-Notification. */
class DownloadService : Service() {
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private var job: Job? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val url = intent?.getStringExtra("url")
        val token = intent?.getStringExtra("token") ?: ""
        if (url.isNullOrBlank()) { stopSelf(); return START_NOT_STICKY }

        ensureChannel()
        startForeground(NOTIF_ID, build("Modell-Download", "startet…", 0, 0, true))

        job?.cancel()
        job = scope.launch {
            try {
                val name = url.substringAfterLast('/').substringBefore('?')
                    .let { if (it.endsWith(".litertlm")) it else "$it.litertlm" }
                val dir = File(filesDir, "models").apply { mkdirs() }
                val out = File(dir, name)
                var lastPct = -1
                ModelDownloader.download(url, token, out) { done, total ->
                    DownloadBus.state.value = Dl.Progress(done, total)
                    val pct = if (total > 0) (done * 100 / total).toInt() else 0
                    if (pct != lastPct) {
                        lastPct = pct
                        notify(build("Modell-Download: $name", "$pct %  (${done / 1_000_000}/${total / 1_000_000} MB)",
                            100, pct, false))
                    }
                }
                Prefs(this@DownloadService).activeModel = out.name
                DownloadBus.state.value = Dl.Done(out.absolutePath)
                notify(build("Modell geladen ✅", "Download fertig", 0, 0, false))
            } catch (e: Exception) {
                DownloadBus.state.value = Dl.Error(e.message ?: "Fehler")
                notify(build("Download abgebrochen", e.message ?: "Fehler — erneut starten setzt fort", 0, 0, false))
            } finally {
                stopForeground(STOP_FOREGROUND_DETACH)
                stopSelf()
            }
        }
        return START_NOT_STICKY
    }

    private fun ensureChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val ch = NotificationChannel(CHAN, "Modell-Download", NotificationManager.IMPORTANCE_LOW)
            (getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager).createNotificationChannel(ch)
        }
    }

    private fun build(title: String, text: String, max: Int, prog: Int, indeterminate: Boolean) =
        NotificationCompat.Builder(this, CHAN)
            .setContentTitle(title)
            .setContentText(text)
            .setSmallIcon(android.R.drawable.stat_sys_download)
            .setOngoing(indeterminate || max > 0)
            .setOnlyAlertOnce(true)
            .apply { if (max > 0 || indeterminate) setProgress(max, prog, indeterminate) }
            .build()

    private fun notify(n: android.app.Notification) =
        (getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager).notify(NOTIF_ID, n)

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    companion object {
        private const val CHAN = "model_download"
        private const val NOTIF_ID = 42

        fun start(context: Context, url: String, token: String) {
            val i = Intent(context, DownloadService::class.java)
                .putExtra("url", url).putExtra("token", token)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) context.startForegroundService(i)
            else context.startService(i)
        }
    }
}
