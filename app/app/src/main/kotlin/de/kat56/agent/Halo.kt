// kAIm56 KatAgent — Android client for the kAIm56 agent platform
// Copyright (C) 2026 Ulrich Neidel
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Protokollschicht fuer die Brilliant-Labs-Brillen (Halo/Frame).
//
// Portiert aus dem Brilliant SDK (BSD-3-Clause, (c) 2025 CitizenOneX,
// github.com/brilliantlabsAR/brilliant_sdk, Paket `brilliant_ble`). Warum
// portiert und nicht eingebunden: das offizielle Mobile-SDK ist Flutter/Dart.
// Eine zweite Laufzeitumgebung nur fuer einen BLE-Client waere in dieser
// Compose-App ein Fremdkoerper — das Protokoll selbst ist klein.
//
// Diese Datei ist bewusst FREI von Android-Abhaengigkeiten: sie laesst sich
// ohne Geraet und ohne Emulator testen (siehe app/src/test/.../HaloTest.kt).
// Der Bluetooth-Teil sitzt hinter HaloLink und kommt separat.
package de.kat56.agent

/** Rahmung, Adressen und Hilfsfunktionen der Brillen-Verbindung. */
object Halo {

    // ---- GATT ------------------------------------------------------------
    /** Dienst, unter dem beide Brillen ihre Datenkanaele anbieten. */
    const val SERVICE = "7a230001-5475-a6a4-654c-8431f6ad49c4"
    /** Telefon -> Brille. */
    const val CHAR_TX = "7a230002-5475-a6a4-654c-8431f6ad49c4"
    /** Brille -> Telefon (Notifications). */
    const val CHAR_RX = "7a230003-5475-a6a4-654c-8431f6ad49c4"
    /** Nur Halo: eigener Audiokanal. Dient zugleich der Geraeteerkennung —
     *  Frame hat diese Characteristic nicht. */
    const val CHAR_AUDIO_TX = "7a230005-5475-a6a4-654c-8431f6ad49c4"
    /** Firmware-Update laeuft ueber einen eigenen Dienst (hier nicht genutzt). */
    const val DFU_SERVICE = "8ec90001-f315-4f60-9fb8-838830daea50"

    /** Erstes Byte jedes Datenpakets: "rohe Nutzdaten" (im Gegensatz zu Lua-Text). */
    const val DATA_FLAG: Byte = 0x01
    /** Audio-Antworten der Brille: laufendes Stueck bzw. letztes Stueck. */
    const val AUDIO_CHUNK: Byte = 0x05
    const val AUDIO_FINAL: Byte = 0x06
    /** Foto-Antworten: weiteres Stueck bzw. letztes Stueck. */
    const val PHOTO_CHUNK: Byte = 0x07
    const val PHOTO_FINAL: Byte = 0x08

    // ---- Empfangsseite ---------------------------------------------------
    // Die Brille schickt zweierlei ueber dieselbe Characteristic:
    //   [0x01, rest…]  -> Daten (Quittung, Audio, Foto)
    //   alles andere   -> Text aus dem Lua-Interpreter (print, Fehlermeldungen)
    // Nach dem Abschneiden des 0x01 beginnt der Datenrahmen mit seinem Flag.
    // ALLE Pruefungen hier arbeiten auf dem abgeschnittenen Rahmen.
    const val ACK_FLAG: Byte = 0x00

    fun isDataFrame(raw: ByteArray): Boolean = raw.isNotEmpty() && raw[0] == DATA_FLAG

    /** Den 0x01-Kopf entfernen; Ergebnis ist [flag, rest…]. */
    fun frameOf(raw: ByteArray): ByteArray =
        if (isDataFrame(raw)) raw.copyOfRange(1, raw.size) else raw

    // Die Brille bestaetigt JEDES empfangene Paket ("receiver-paced flow
    // control"): erst nach der Bestaetigung darf das naechste raus, sonst
    // laeuft ihr Puffer ueber. Auf der Leitung 01 00 00, im Rahmen 00 00.
    /** true, wenn der Rahmen eine Paketbestaetigung ist (ok oder Fehler). */
    fun isAck(frame: ByteArray): Boolean =
        frame.size >= 2 && frame[0] == ACK_FLAG &&
            (frame[1] == 0x00.toByte() || frame[1] == 0x01.toByte())

    /** true, wenn die Brille das Paket als fehlerhaft bestaetigt hat. */
    fun isAckError(frame: ByteArray): Boolean = isAck(frame) && frame[1] == 0x01.toByte()

    /** Groesse, die wir beim Verbinden anfragen; ausgehandelt wird ggf. weniger. */
    const val MTU_REQUEST = 517

    // ---- Groessen --------------------------------------------------------
    // Aus dem SDK: maxString = mtu-3, maxData = mtu-4. Bei Halo nochmals -2,
    // weil das Geraet 517 meldet, aber real weniger vertraegt.
    fun maxStringLength(mtu: Int, isHalo: Boolean): Int = mtu - 3 - (if (isHalo) 2 else 0)

    fun maxDataLength(mtu: Int, isHalo: Boolean): Int = mtu - 4 - (if (isHalo) 2 else 0)

    /** Groesstes Paket, das ueber die TX-Characteristic gehen darf. */
    fun maxPacket(maxDataLength: Int): Int = maxDataLength + 1

    // ---- Rahmung ---------------------------------------------------------
    /**
     * Zerlegt eine Nachricht in BLE-Pakete — byte-gleich zur Vorlage im SDK:
     *
     *   erstes Paket:   [0x01, code, laenge_hi, laenge_lo, nutzdaten…]
     *   weitere Pakete: [0x01, code, nutzdaten…]
     *
     * Das fuehrende 0x01 nimmt der Bluetooth-Stapel der Brille weg, bevor der
     * Lua-Handler die Daten sieht — dort beginnt das Paket also mit dem Code.
     *
     * Die Laenge ist die des GESAMTEN Payloads (16 Bit, big endian); der
     * Lua-Handler auf der Brille setzt daran die Stuecke wieder zusammen.
     * Maximal 65535 Byte Nutzdaten.
     */
    fun packets(msgCode: Int, payload: ByteArray, maxDataLength: Int): List<ByteArray> {
        require(payload.size <= 65535) { "Nutzdaten laenger als 65535 Byte" }
        require(maxDataLength >= 8) { "maxDataLength zu klein: $maxDataLength" }
        val code = (msgCode and 0xFF).toByte()
        val chunk = maxDataLength - 1          // wie im SDK: eine Reserve bleibt frei
        val out = ArrayList<ByteArray>()
        var sent = 0
        var first = true
        // Leere Nutzdaten sind eine gueltige Nachricht (z.B. "Anzeige loeschen"):
        // ein Paket mit Laenge 0 und ohne Rumpf.
        do {
            val rest = payload.size - sent
            val take = if (first) minOf(rest, chunk - 2) else minOf(rest, chunk)
            val head = if (first) 4 else 2
            val p = ByteArray(head + take)
            p[0] = DATA_FLAG
            p[1] = code
            if (first) {
                p[2] = (payload.size ushr 8).toByte()
                p[3] = (payload.size and 0xFF).toByte()
            }
            payload.copyInto(p, head, sent, sent + take)
            out.add(p)
            sent += take
            first = false
        } while (sent < payload.size)
        return out
    }

    /**
     * Gegenstueck zu [packets] — was der Lua-Handler auf der Brille tut.
     * Nur fuer Tests: damit laesst sich die Rahmung ohne Geraet gegenpruefen.
     * Gibt (code, nutzdaten) zurueck oder wirft, wenn der Strom nicht stimmt.
     */
    fun reassemble(packets: List<ByteArray>): Pair<Int, ByteArray> {
        require(packets.isNotEmpty()) { "keine Pakete" }
        val first = packets.first()
        require(first.size >= 4) { "erstes Paket zu kurz" }
        require(first[0] == DATA_FLAG) { "erstes Paket ohne 0x01-Kopf" }
        val code = first[1].toInt() and 0xFF
        val total = ((first[2].toInt() and 0xFF) shl 8) or (first[3].toInt() and 0xFF)
        val buf = ByteArray(total)
        var at = 0
        packets.forEachIndexed { i, p ->
            require(p[0] == DATA_FLAG) { "Paket $i ohne 0x01-Kopf" }
            require((p[1].toInt() and 0xFF) == code) { "Paket $i mit fremdem Code" }
            val head = if (i == 0) 4 else 2
            val n = p.size - head
            require(at + n <= total) { "Paket $i laeuft ueber die angekuendigte Laenge" }
            p.copyInto(buf, at, head, p.size)
            at += n
        }
        require(at == total) { "unvollstaendig: $at von $total Byte" }
        return code to buf
    }

    // ---- Audio -----------------------------------------------------------
    /**
     * Sammelt die Audioantworten der Brille. Jede Notification traegt vorn ein
     * Flag: 0x05 = weiteres Stueck, 0x06 = letztes Stueck. Alles danach sind
     * rohe PCM-Bytes.
     */
    class AudioCollector {
        private val buf = java.io.ByteArrayOutputStream()
        /** true, sobald das Schlussstueck kam. */
        var complete: Boolean = false
            private set

        /** Verarbeitet ein Paket; liefert dessen PCM-Anteil (fuer Live-Weitergabe). */
        fun feed(packet: ByteArray): ByteArray {
            if (packet.isEmpty()) return ByteArray(0)
            when (packet[0]) {
                AUDIO_CHUNK -> {}
                AUDIO_FINAL -> complete = true
                else -> return ByteArray(0)     // keine Audioantwort -> ignorieren
            }
            val pcm = packet.copyOfRange(1, packet.size)
            buf.write(pcm)
            return pcm
        }

        fun pcm(): ByteArray = buf.toByteArray()
    }

    /**
     * Sammelt ein Foto. Wie beim Ton kommt es stueckweise: 0x07 = weiteres
     * Stueck, 0x08 = Schluss. Die Brille liefert vollstaendiges JPEG (der
     * sparsame Rohmodus spart 623 Byte Kopf, verlangt dafuer aber, dass die
     * App je Qualitaet und Aufloesung einen Kopf vorhaelt — das ist die
     * Ersparnis nicht wert).
     */
    class PhotoCollector {
        private val buf = java.io.ByteArrayOutputStream()
        var complete: Boolean = false
            private set

        /** Verarbeitet ein Paket; true, wenn das Bild damit vollstaendig ist. */
        fun feed(frame: ByteArray): Boolean {
            if (frame.isEmpty()) return false
            when (frame[0]) {
                PHOTO_CHUNK -> buf.write(frame, 1, frame.size - 1)
                PHOTO_FINAL -> {
                    // Das Schlussstueck kann leer sein — dann steht das Bild schon.
                    if (frame.size > 1) buf.write(frame, 1, frame.size - 1)
                    complete = true
                }
                else -> return false
            }
            return complete
        }

        fun jpeg(): ByteArray = buf.toByteArray()

        fun reset() { buf.reset(); complete = false }
    }

    /**
     * WAV-Kopf vor die PCM-Daten setzen — damit geht der Mitschnitt unveraendert
     * an /api/stt im Manager, der schon fuer die App-Diktate zustaendig ist.
     */
    fun wav(pcm: ByteArray, sampleRate: Int = 8000, bitsPerSample: Int = 16,
            channels: Int = 1): ByteArray {
        val byteRate = sampleRate * channels * bitsPerSample / 8
        val blockAlign = channels * bitsPerSample / 8
        val head = java.io.ByteArrayOutputStream(44)
        fun ascii(s: String) = head.write(s.toByteArray(Charsets.US_ASCII))
        fun le32(v: Int) { head.write(v and 0xFF); head.write((v ushr 8) and 0xFF)
                           head.write((v ushr 16) and 0xFF); head.write((v ushr 24) and 0xFF) }
        fun le16(v: Int) { head.write(v and 0xFF); head.write((v ushr 8) and 0xFF) }
        ascii("RIFF");  le32(36 + pcm.size); ascii("WAVE")
        ascii("fmt ");  le32(16); le16(1)               // 16 = PCM-Kopf, 1 = unkomprimiert
        le16(channels); le32(sampleRate); le32(byteRate); le16(blockAlign); le16(bitsPerSample)
        ascii("data");  le32(pcm.size)
        return head.toByteArray() + pcm
    }

    /**
     * 8-Bit-Mitschnitte der Brille kommen VORZEICHENBEHAFTET, WAV erwartet bei
     * 8 Bit aber vorzeichenlos. Deshalb diese Verschiebung. (Wir fahren das
     * Mikrofon standardmaessig mit 16 Bit, wo sich die Frage nicht stellt —
     * die Funktion ist fuer den sparsamen Modus da.)
     */
    fun signed8ToUnsigned8(pcm: ByteArray): ByteArray =
        ByteArray(pcm.size) { ((pcm[it].toInt() and 0xFF) xor 0x80).toByte() }
}

/**
 * Der Bluetooth-Unterbau, hinter einer Schnittstelle: so laesst sich alles
 * darueber ohne Geraet testen (Testfaelle setzen eine Attrappe ein).
 */
interface HaloLink {
    /** Ausgehandelte MTU der bestehenden Verbindung. */
    val mtu: Int
    /** true = Halo (hat den Audiokanal), false = Frame. */
    val isHalo: Boolean
    /** Ein Paket auf die TX-Characteristic schreiben. */
    fun write(packet: ByteArray)
    /** Zeile an den Lua-Interpreter der Brille (ohne 0x01-Kopf). */
    fun writeString(text: String)
}

/**
 * Sitzung auf einer verbundenen Brille: kennt die ausgehandelten Groessen und
 * schickt getypte Nachrichten. Die Codes muessen zu den Lua-Handlern passen,
 * die beim Verbinden auf die Brille geladen werden.
 */
class HaloSession(private val link: HaloLink) {

    val maxData: Int get() = Halo.maxDataLength(link.mtu, link.isHalo)
    val maxString: Int get() = Halo.maxStringLength(link.mtu, link.isHalo)

    /** Nachrichtencodes dieser App (Telefon -> Brille). TEXT und CLEAR wie in
     *  der SDK-Vorlage, damit deren unveraenderte Lua-Module passen. */
    object Code {
        const val TEXT = 0x12           // Text anzeigen (plain_text.lua)
        const val CLEAR = 0x10          // Anzeige loeschen (code.lua)
        const val AUDIO_START = 0x30    // Mikrofon an
        const val AUDIO_STOP = 0x31     // Mikrofon aus
        const val PHOTO = 0x0d          // Foto ausloesen
    }

    fun send(msgCode: Int, payload: ByteArray) {
        Halo.packets(msgCode, payload, maxData).forEach(link::write)
    }

    /**
     * Antworttext auf die Brille. Das Format ist das der SDK-Vorlage, damit
     * deren `plain_text.lua` den Rumpf unveraendert lesen kann:
     * [x_hi, x_lo, y_hi, y_lo, Farbe, Zeilenabstand, Text…].
     * Laenge deckeln — das Display ist klein, und eine Nachricht darf 65535
     * Byte nicht ueberschreiten.
     */
    fun showText(text: String, x: Int = 1, y: Int = 1, color: Int = 1,
                 spacing: Int = 4, limit: Int = 1024) {
        val body = text.take(limit).toByteArray(Charsets.UTF_8)
        val p = ByteArray(6 + body.size)
        p[0] = (x ushr 8).toByte(); p[1] = (x and 0xFF).toByte()
        p[2] = (y ushr 8).toByte(); p[3] = (y and 0xFF).toByte()
        p[4] = (color and 0xFF).toByte()      // Index in die Palette der Brille
        p[5] = (spacing and 0xFF).toByte()
        body.copyInto(p, 6)
        send(Code.TEXT, p)
    }

    fun clear() = send(Code.CLEAR, ByteArray(0))

    /** Mikrofon starten. Vorgabe 8 kHz/16 Bit: klein genug fuer BLE, sauber
     *  fuer die Spracherkennung, und keine Vorzeichenfrage wie bei 8 Bit. */
    fun startAudio(sampleRate: Int = 8000, bitDepth: Int = 16) {
        send(Code.AUDIO_START, byteArrayOf(
            (sampleRate ushr 8).toByte(), (sampleRate and 0xFF).toByte(), bitDepth.toByte()))
    }

    fun stopAudio() = send(Code.AUDIO_STOP, ByteArray(0))

    fun takePhoto() = send(Code.PHOTO, ByteArray(0))

    /** Lua-Modul auf die Brille schieben (beim Verbinden, einmal je Sitzung). */
    fun uploadLua(name: String, source: String) {
        // Der Interpreter nimmt Zeilen entgegen; laengere Module gehen in
        // Haeppchen, die kleiner sind als die maximale Zeichenkette.
        val chunk = maxString - 32
        var i = 0
        link.writeString("__m='' ")
        while (i < source.length) {
            val part = source.substring(i, minOf(source.length, i + chunk))
            link.writeString("__m=__m..[==[$part]==] ")
            i += chunk
        }
        link.writeString("f=frame.file.open('$name.lua','w');f:write(__m);f:close();__m=nil ")
    }
}
