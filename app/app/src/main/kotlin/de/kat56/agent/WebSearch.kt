// kAIm56 KatAgent — Android client for the kAIm56 agent platform
// Copyright (C) 2026 Ulrich Neidel
// SPDX-License-Identifier: AGPL-3.0-or-later
package de.kat56.agent

import java.net.HttpURLConnection
import java.net.URL
import java.net.URLDecoder
import java.net.URLEncoder

/** Web-Zugriff für den On-Device-Modus: DuckDuckGo-Suche (kein API-Key) +
 *  Seiten-Text abrufen. Ergebnis wird Gemma als Kontext vorangestellt. */
object WebSearch {

    data class Result(val title: String, val url: String, val snippet: String)

    private fun http(urlStr: String, maxChars: Int = 1_000_000): String {
        val conn = URL(urlStr).openConnection() as HttpURLConnection
        return try {
            conn.connectTimeout = 15000
            conn.readTimeout = 20000
            conn.instanceFollowRedirects = true
            conn.setRequestProperty("User-Agent",
                "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0")
            conn.inputStream.bufferedReader().use { it.readText() }.take(maxChars)
        } finally {
            conn.disconnect()
        }
    }

    private fun clean(s: String) =
        s.replace(Regex("<[^>]+>"), "").replace("&amp;", "&").replace("&#x27;", "'")
            .replace("&quot;", "\"").replace("&lt;", "<").replace("&gt;", ">").trim()

    private fun realUrl(h: String): String {
        val m = Regex("uddg=([^&]+)").find(h) ?: return h
        return try { URLDecoder.decode(m.groupValues[1], "UTF-8") } catch (e: Exception) { h }
    }

    fun search(query: String, n: Int = 4): List<Result> {
        val html = try {
            http("https://html.duckduckgo.com/html/?q=" + URLEncoder.encode(query, "UTF-8"))
        } catch (e: Exception) { return emptyList() }
        val hrefs = Regex("class=\"result__a\"[^>]*href=\"([^\"]+)\"").findAll(html).map { it.groupValues[1] }.toList()
        val titles = Regex("class=\"result__a\"[^>]*>([\\s\\S]*?)</a>").findAll(html).map { clean(it.groupValues[1]) }.toList()
        val snips = Regex("class=\"result__snippet\"[^>]*>([\\s\\S]*?)</a>").findAll(html).map { clean(it.groupValues[1]) }.toList()
        val out = ArrayList<Result>()
        for (i in 0 until minOf(n, hrefs.size)) {
            out.add(Result(titles.getOrElse(i) { "" }, realUrl(hrefs[i]), snips.getOrElse(i) { "" }))
        }
        return out
    }

    fun fetchText(url: String, maxChars: Int = 2500): String {
        val html = try { http(url) } catch (e: Exception) { return "" }
        val noScript = html.replace(Regex("(?s)<(script|style|noscript)[^>]*>.*?</\\1>"), " ")
        return noScript.replace(Regex("(?s)<[^>]+>"), " ")
            .replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&#x27;", "'").replace("&quot;", "\"").replace("&nbsp;", " ")
            .replace(Regex("\\s+"), " ").trim().take(maxChars)
    }

    /** Baut den Web-Kontext für einen Prompt: enthält die Nachricht eine URL,
     *  wird deren Inhalt geholt; sonst wird gesucht (+ Auszug der Top-Seite). */
    fun buildContext(message: String): String {
        val urlInMsg = Regex("https?://\\S+").find(message)?.value
        if (urlInMsg != null) {
            val t = fetchText(urlInMsg, 3000)
            return if (t.isNotBlank()) "Inhalt von $urlInMsg:\n$t" else "(Seite nicht abrufbar)"
        }
        val res = search(message, 4)
        if (res.isEmpty()) return "(keine Web-Ergebnisse)"
        val sb = StringBuilder()
        res.forEach { sb.append("• ${it.title}\n  ${it.url}\n  ${it.snippet}\n") }
        res.firstOrNull()?.url?.let { top ->
            val body = fetchText(top, 1800)
            if (body.isNotBlank()) sb.append("\nAuszug (${res.first().title}):\n$body\n")
        }
        return sb.toString()
    }
}
