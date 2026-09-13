// kAIm56 KatAgent — Android client for the kAIm56 agent platform
// Copyright (C) 2026 Ulrich Neidel
// SPDX-License-Identifier: AGPL-3.0-or-later
package de.kat56.agent

import android.content.Context
import android.graphics.Bitmap
import com.google.ai.edge.litertlm.Backend
import com.google.ai.edge.litertlm.Content
import com.google.ai.edge.litertlm.Contents
import com.google.ai.edge.litertlm.ConversationConfig
import com.google.ai.edge.litertlm.Engine
import com.google.ai.edge.litertlm.EngineConfig
import com.google.ai.edge.litertlm.SamplerConfig
import kotlinx.coroutines.flow.collect
import java.io.File

/** On-Device-LLM via LiteRT-LM. Laedt .litertlm-Modelle (inkl. Gemma 4).
 *  gemma-3n/gemma-4 sind multimodal -> optionaler Bild-Input. */
class LocalGemma(private val context: Context) {
    private var engine: Engine? = null
    var loadedPath: String = ""
        private set

    fun isReady(): Boolean = engine != null

    /** Modell laden (Pfad auf eine .litertlm-Datei). Kann mehrere Sekunden dauern. */
    fun load(modelPath: String) {
        close()
        val cfg = EngineConfig(
            modelPath = modelPath,
            backend = Backend.CPU(),
            visionBackend = Backend.CPU(),   // Bild-Input (multimodal) aktivieren
        )
        val e = Engine(cfg)
        e.initialize()
        engine = e
        loadedPath = modelPath
    }

    /** Streaming: onPartial fuer jedes Teilstueck, am Ende onDone. */
    /** Streaming per Flow: ruft onPartial je Token; kehrt zurueck wenn fertig.
     *  Der Aufrufer setzt danach 'busy=false' (im finally) -> haengt nicht mehr. */
    suspend fun generateStreaming(prompt: String, image: Bitmap?, onPartial: (String) -> Unit) {
        val eng = engine ?: throw IllegalStateException("Kein Modell geladen.")
        val conversation = eng.createConversation(
            ConversationConfig(samplerConfig = SamplerConfig(topK = 40, topP = 0.95, temperature = 0.8))
        )
        try {
            val flow = if (image != null) {
                val imgFile = File(context.cacheDir, "input_image.jpg")
                imgFile.outputStream().use { image.compress(Bitmap.CompressFormat.JPEG, 90, it) }
                conversation.sendMessageAsync(Contents.of(Content.ImageFile(imgFile.absolutePath), Content.Text(prompt)))
            } else {
                conversation.sendMessageAsync(prompt)
            }
            flow.collect { onPartial(it.toString()) }
        } finally {
            try { conversation.close() } catch (_: Exception) {}
        }
    }

    fun close() {
        try {
            engine?.close()
        } catch (_: Exception) {
        }
        engine = null
        loadedPath = ""
    }
}
