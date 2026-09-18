// kAIm56 KatAgent — Android client for the kAIm56 agent platform
// Copyright (C) 2026 Ulrich Neidel
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Self-update from GitHub Releases. The release pipeline (.github/workflows/
// apk.yml) publishes every app version as a release tagged app-v<version>
// with katagent-<version>.apk attached, signed with the stable key — so the
// package installer accepts it as an in-place update. Android never installs
// silently for a normal app: the last step is the system's install prompt.
package de.kat56.agent

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.core.content.FileProvider
import org.json.JSONArray
import java.io.File
import java.net.HttpURLConnection
import java.net.URL

object AppUpdate {
    const val DEFAULT_REPO = "uneidel/kaim56"
    private val TAG = Regex("^app-v(\\d+(?:\\.\\d+)*)$")

    data class Release(val version: String, val tag: String, val apkUrl: String, val notes: String)

    /** Newest app release in GitHub's release list that carries an APK; null when none. */
    fun parseReleases(json: String): Release? {
        val arr = runCatching { JSONArray(json) }.getOrNull() ?: return null
        var best: Release? = null
        for (i in 0 until arr.length()) {
            val r = arr.optJSONObject(i) ?: continue
            if (r.optBoolean("draft") || r.optBoolean("prerelease")) continue
            val tag = r.optString("tag_name")
            val version = TAG.find(tag)?.groupValues?.get(1) ?: continue
            val assets = r.optJSONArray("assets") ?: continue
            var url = ""
            for (j in 0 until assets.length()) {
                val a = assets.optJSONObject(j) ?: continue
                if (a.optString("name").endsWith(".apk")) { url = a.optString("browser_download_url"); break }
            }
            if (url.isBlank()) continue
            if (best == null || isNewer(best.version, version))
                best = Release(version, tag, url, r.optString("body").take(2000))
        }
        return best
    }

    /** "5.38" is newer than "5.9"; numeric per part, missing parts count as 0. */
    fun isNewer(installed: String, candidate: String): Boolean {
        val a = parts(installed); val b = parts(candidate)
        if (a == null || b == null) return false
        for (i in 0 until maxOf(a.size, b.size)) {
            val x = a.getOrElse(i) { 0 }; val y = b.getOrElse(i) { 0 }
            if (x != y) return y > x
        }
        return false
    }

    private fun parts(v: String): List<Int>? {
        val m = Regex("^v?(\\d+(?:\\.\\d+)*)").find(v.trim()) ?: return null
        return m.groupValues[1].split(".").map { it.toInt() }
    }

    /** Asks GitHub; null on any error (offline, rate limit, no release). */
    fun fetch(repo: String): Release? = runCatching {
        val conn = URL("https://api.github.com/repos/${repo.trim('/')}/releases?per_page=20").openConnection() as HttpURLConnection
        try {
            conn.connectTimeout = 10000; conn.readTimeout = 20000
            conn.setRequestProperty("Accept", "application/vnd.github+json")
            conn.setRequestProperty("User-Agent", "katagent")
            if (conn.responseCode !in 200..299) return null
            parseReleases(conn.inputStream.bufferedReader().use { it.readText() })
        } finally { conn.disconnect() }
    }.getOrNull()

    /** Downloads the APK into the app's cache (update/); false on error. */
    fun download(url: String, dest: File, onProgress: (Long, Long) -> Unit = { _, _ -> }): Boolean = runCatching {
        dest.parentFile?.mkdirs()
        val conn = URL(url).openConnection() as HttpURLConnection
        try {
            conn.connectTimeout = 15000; conn.readTimeout = 60000
            conn.instanceFollowRedirects = true
            conn.setRequestProperty("User-Agent", "katagent")
            if (conn.responseCode !in 200..299) return false
            val total = conn.contentLengthLong
            val tmp = File(dest.path + ".part")
            conn.inputStream.use { inp -> tmp.outputStream().use { out ->
                val buf = ByteArray(64 * 1024); var done = 0L
                while (true) {
                    val n = inp.read(buf); if (n < 0) break
                    out.write(buf, 0, n); done += n; onProgress(done, total)
                }
            } }
            if (total > 0 && tmp.length() != total) { tmp.delete(); return false }
            tmp.renameTo(dest)
        } finally { conn.disconnect() }
    }.getOrDefault(false)

    fun updateFile(ctx: Context, version: String) = File(ctx.cacheDir, "update/katagent-$version.apk")

    /** May this app hand an APK to the package installer? (Android 8+ asks once per app.) */
    fun canInstall(ctx: Context): Boolean =
        Build.VERSION.SDK_INT < 26 || ctx.packageManager.canRequestPackageInstalls()

    /** Opens the system page where the user allows installs from KatAgent. */
    fun openInstallPermission(ctx: Context) {
        runCatching {
            ctx.startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${ctx.packageName}"))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        }
    }

    /** Hands the downloaded APK to the package installer (system prompt follows). */
    fun install(ctx: Context, apk: File): Boolean = runCatching {
        val uri = FileProvider.getUriForFile(ctx, ctx.packageName + ".files", apk)
        ctx.startActivity(Intent(Intent.ACTION_VIEW)
            .setDataAndType(uri, "application/vnd.android.package-archive")
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK))
        true
    }.getOrDefault(false)
}
