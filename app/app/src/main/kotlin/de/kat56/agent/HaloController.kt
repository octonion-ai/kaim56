// kAIm56 KatAgent — Android client for the kAIm56 agent platform
// Copyright (C) 2026 Ulrich Neidel
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Die Brille am Agenten: verbinden, Lua-Module hochladen, Klick -> Mitschnitt
// -> Spracherkennung -> Instanz -> Antwort aufs Display.
//
// Bewusst OHNE Android-Abhaengigkeiten und ohne Netz: alles, was von aussen
// kommt (Bluetooth, Spracherkennung, der Agent, die Lua-Dateien), steckt hinter
// Funktionen, die der Aufrufer stellt. Dadurch laesst sich der ganze Ablauf
// trocken durchspielen — mit HaloDryLink statt echter Brille.
package de.kat56.agent

/**
 * Attrappe der Brille fuer den Trockenlauf: nimmt dieselben Pakete entgegen wie
 * die echte, setzt sie wieder zusammen und fuehrt Buch darueber, was auf dem
 * Display staende. So laesst sich der ganze Weg ohne Hardware durchklicken.
 */
class HaloDryLink(
    override val mtu: Int = 247,
    override val isHalo: Boolean = true,
    /** Was die Kamera im Trockenlauf liefert. */
    private val cannedPhoto: ByteArray = byteArrayOf(0xFF.toByte(), 0xD8.toByte(), 0xFF.toByte()),
) : HaloLink {
    private val pending = mutableListOf<ByteArray>()

    /** Das zuletzt "aufgenommene" Foto, null solange keins ausgeloest wurde. */
    var photo: ByteArray? = null
        private set

    /** Was gerade auf dem Display der Brille stuende (zeilenweise). */
    val display = mutableListOf<String>()
    /** Hochgeladene Lua-Module, in der Reihenfolge des Hochladens. */
    val uploaded = mutableListOf<String>()
    /** Laeuft das Mikrofon? */
    var recording = false
        private set
    /** Alle empfangenen Nachrichten als (Code, Nutzdaten) — fuer Tests. */
    val messages = mutableListOf<Pair<Int, ByteArray>>()

    override fun write(packet: ByteArray) {
        pending.add(packet)
        // Ein Paket mit vollstaendiger Nachricht? Dann auswerten und leeren.
        val done = runCatching { Halo.reassemble(pending.toList()) }.getOrNull() ?: return
        pending.clear()
        messages.add(done)
        val (code, payload) = done
        when (code) {
            HaloSession.Code.TEXT -> {
                val text = String(payload, 6, payload.size - 6, Charsets.UTF_8)
                display.clear()
                display.addAll(text.split("\n").filter { it.isNotEmpty() })
            }
            HaloSession.Code.CLEAR -> display.clear()
            HaloSession.Code.AUDIO_START -> recording = true
            HaloSession.Code.AUDIO_STOP -> recording = false
            HaloSession.Code.PHOTO -> photo = cannedPhoto
        }
    }

    override fun writeString(text: String) {
        // Das Ende eines Uploads verraet den Modulnamen: …open('name.lua','w')…
        Regex("""open\('([^']+)\.lua'""").find(text)?.let { uploaded.add(it.groupValues[1]) }
    }
}

/**
 * Ablauf und Zustand der Brillenanbindung. Kennt weder Bluetooth noch HTTP —
 * beides reicht der Aufrufer herein.
 */
class HaloController(
    /** Liefert den Quelltext eines Lua-Moduls (aus den Assets). */
    private val luaSource: (String) -> String,
    /** Aufnahme -> erkannter Text (im Manager: /api/stt). Null = fehlgeschlagen. */
    private val transcribe: (ByteArray) -> String?,
    /** Erkannten Text an die Instanz geben; liefert die Antwort. */
    private val ask: (String) -> String,
    /** Frage MIT Bild an die Instanz (JPEG, wie es die Brille liefert). */
    private val askWithImage: (String, ByteArray) -> String = { q, _ -> ask(q) },
    /** Wartet auf das naechste fertige Foto; null = keines gekommen. */
    private val awaitPhoto: (Long) -> ByteArray? = { null },
    /** Fortschritt fuer die Oberflaeche. */
    private val onStatus: (String) -> Unit = {},
) {
    /** Module, die beim Verbinden auf die Brille wandern. Reihenfolge zaehlt:
     *  katagent.lua braucht die anderen beim Start. */
    val modules = listOf("data.min", "plain_text.min", "audio.min", "camera.min",
                         "code.min", "katagent")

    var session: HaloSession? = null
        private set

    // Am Emulator des Herstellers gemessen (tools/halo/emu_test.py), nicht
    // geraten: auf die 256 px Breite passen 59 'M' nebeneinander, und bei
    // 20 px Zeilenabstand 13 Zeilen untereinander. Etwas Rand bleibt.
    /** Zeichen je Zeile. */
    var columns = 56
    /** Zeilen, die auf das Display passen. */
    var rows = 12

    fun attach(link: HaloLink) {
        val s = HaloSession(link)
        session = s
        onStatus("Module werden geladen…")
        modules.forEach { s.uploadLua(it, luaSource(it)) }
        s.clear()
        onStatus("Brille bereit")
    }

    fun detach() {
        session = null
    }

    /** Aufnahme starten (Klick an der Brille oder Knopf in der App). */
    fun startListening() {
        val s = session ?: return
        s.showText("… hört zu")
        s.startAudio()
        onStatus("Aufnahme laeuft")
    }

    /**
     * Aufnahme beenden und den ganzen Weg gehen: Mitschnitt -> Spracherkennung
     * -> Instanz -> Antwort auf das Display. Blockiert; gehoert auf einen
     * Hintergrund-Thread. Rueckgabe: die Antwort oder null.
     */
    fun stopAndAsk(recordingWav: ByteArray): String? {
        val s = session ?: return null
        s.stopAudio()
        onStatus("Spracherkennung…")
        val heard = transcribe(recordingWav)
        if (heard.isNullOrBlank()) {
            s.showText("nichts verstanden")
            onStatus("nichts verstanden")
            return null
        }
        return handle(heard)
    }

    /**
     * Eine Aeusserung ausfuehren: erst pruefen, ob ein lokaler Befehl darin
     * steckt (Foto, Abbrechen), sonst geht sie als Frage an die Instanz.
     */
    fun handle(heard: String): String? {
        val s = session ?: return null
        when (VoiceCommand.parse(heard)?.action) {
            VoiceCommand.Action.PHOTO -> return photoAndAsk(VoiceCommand.parse(heard)!!.rest)
            VoiceCommand.Action.STOP -> {
                s.stopAudio()
                s.clear()
                onStatus("abgebrochen")
                return null
            }
            // Bildschirmfoto des Telefons: noch nicht angeschlossen. Lieber
            // sagen als still nichts tun.
            VoiceCommand.Action.SCREENSHOT -> {
                show("Screenshot kann ich noch nicht")
                onStatus("Screenshot ist noch nicht angeschlossen")
                return null
            }
            null -> {}
        }
        onStatus("Frage: $heard")
        s.showText(wrap("> $heard"))
        val answer = ask(heard)
        show(answer)
        onStatus("")
        return answer
    }

    /**
     * Foto mit der Kamera der Brille und mit der Frage an die Instanz schicken.
     * Ohne mitgesprochene Frage wird die naheliegende gestellt.
     */
    fun photoAndAsk(question: String = "", timeoutMs: Long = 20000): String? {
        val s = session ?: return null
        onStatus("Foto…")
        s.showText("… Foto")
        s.takePhoto()
        val jpeg = awaitPhoto(timeoutMs)
        if (jpeg == null || jpeg.isEmpty()) {
            show("kein Bild bekommen")
            onStatus("kein Bild bekommen")
            return null
        }
        val q = question.ifBlank { "Was siehst du auf diesem Bild?" }
        onStatus("Bild an den Agenten…")
        val answer = askWithImage(q, jpeg)
        show(answer)
        onStatus("")
        return answer
    }

    /** Antwort auf die Brille bringen, umgebrochen und auf die Hoehe gekuerzt. */
    fun show(text: String, maxLines: Int = rows) {
        val s = session ?: return
        val lines = wrap(text).split("\n")
        s.showText(lines.take(maxLines).joinToString("\n"))
    }

    /**
     * Zeilenumbruch fuers Display. Die Brille bricht nicht selbst um — kaeme
     * der Text am Stueck, liefe er rechts einfach aus dem Bild.
     */
    fun wrap(text: String, width: Int = columns): String {
        val out = StringBuilder()
        for (para in text.split("\n")) {
            if (para.isEmpty()) { out.append('\n'); continue }
            var line = StringBuilder()
            for (word in para.split(" ")) {
                // Ein einzelnes ueberlanges Wort (URL, Pfad) hart trennen.
                var w = word
                while (w.length > width) {
                    if (line.isNotEmpty()) { out.append(line).append('\n'); line = StringBuilder() }
                    out.append(w.substring(0, width)).append('\n')
                    w = w.substring(width)
                }
                when {
                    line.isEmpty() -> line.append(w)
                    line.length + 1 + w.length <= width -> line.append(' ').append(w)
                    else -> { out.append(line).append('\n'); line = StringBuilder(w) }
                }
            }
            out.append(line).append('\n')
        }
        return out.toString().trimEnd('\n')
    }
}
