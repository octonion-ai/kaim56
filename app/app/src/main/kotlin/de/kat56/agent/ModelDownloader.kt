// kAIm56 KatAgent — Android client for the kAIm56 agent platform
// Copyright (C) 2026 Ulrich Neidel
// SPDX-License-Identifier: AGPL-3.0-or-later
package de.kat56.agent

import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/** Laedt ein (ggf. HuggingFace-gated) .task-Modell herunter — **resume-faehig**.
 *
 *  - Geladen wird in <ziel>.part; bei Abbruch bleibt der Teil erhalten und wird
 *    beim naechsten Aufruf per HTTP-Range fortgesetzt (nicht neu gestartet).
 *  - Eine Marker-Datei <ziel>.part.url merkt sich, zu welcher URL der Teil
 *    gehoert; bei anderer URL wird der alte Teil verworfen.
 *  - HuggingFace-resolve -> 302 auf signierte CDN-URL: Auth-Header nur an
 *    huggingface.co, Range-Header an beide.
 */
object ModelDownloader {
    fun download(
        url: String,
        token: String,
        outFile: File,
        onProgress: (done: Long, total: Long) -> Unit,
    ): String {
        val part = File(outFile.absolutePath + ".part")
        val marker = File(outFile.absolutePath + ".part.url")

        // Gehoert der vorhandene Teil zu DIESER URL? Sonst neu anfangen.
        val resumable = part.exists() && marker.exists() && marker.readText() == url
        if (!resumable) {
            part.delete()
            marker.writeText(url)
        }
        var startAt = if (part.exists()) part.length() else 0L

        var current = url
        var redirects = 0
        while (redirects < 6) {
            val conn = URL(current).openConnection() as HttpURLConnection
            conn.instanceFollowRedirects = false
            conn.connectTimeout = 20000
            conn.readTimeout = 60000
            if (redirects == 0 && token.isNotBlank()) {
                conn.setRequestProperty("Authorization", "Bearer $token")
            }
            if (startAt > 0) conn.setRequestProperty("Range", "bytes=$startAt-")

            val code = conn.responseCode
            if (code in 300..399) {
                val loc = conn.getHeaderField("Location") ?: throw IOException("Redirect ohne Location")
                conn.disconnect(); current = loc; redirects++; continue
            }

            val append: Boolean
            val total: Long
            when (code) {
                HttpURLConnection.HTTP_PARTIAL -> {           // 206 -> fortsetzen
                    append = true
                    val cr = conn.getHeaderField("Content-Range")   // bytes start-end/total
                    total = cr?.substringAfterLast('/')?.toLongOrNull()
                        ?: (startAt + conn.contentLengthLong)
                }
                HttpURLConnection.HTTP_OK -> {                // 200 -> Server ignoriert Range: neu
                    append = false
                    startAt = 0
                    total = conn.contentLengthLong
                }
                else -> {
                    val err = conn.errorStream?.bufferedReader()?.use { it.readText() }?.take(300)
                    conn.disconnect()
                    throw IOException("HTTP $code: ${err ?: ""}")
                }
            }

            FileOutputStream(part, append).use { out ->
                conn.inputStream.use { inp ->
                    val buf = ByteArray(1 shl 16)
                    var done = startAt
                    while (true) {
                        val n = inp.read(buf)
                        if (n < 0) break
                        out.write(buf, 0, n)
                        done += n
                        onProgress(done, total)
                    }
                }
            }
            conn.disconnect()

            // WICHTIG: nur finalisieren, wenn wirklich vollstaendig. Sonst bleibt
            // die .part-Datei erhalten und wird beim naechsten Mal fortgesetzt —
            // verhindert eine kaputte (abgeschnittene) model.task ("Unable to open
            // zip archive").
            if (total > 0 && part.length() < total) {
                throw IOException("Download unvollständig (${part.length()}/$total B) — erneut starten setzt fort.")
            }

            if (outFile.exists()) outFile.delete()
            if (!part.renameTo(outFile)) throw IOException("Umbenennen der fertigen Datei fehlgeschlagen")
            marker.delete()
            return outFile.absolutePath
        }
        throw IOException("Zu viele Redirects")
    }
}
