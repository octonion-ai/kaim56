// kAIm56 KatAgent — Android client for the kAIm56 agent platform
// Copyright (C) 2026 Ulrich Neidel
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Tests der Brillen-Protokollschicht. Laufen auf der JVM, ohne Geraet und ohne
// Emulator: `gradle testDebugUnitTest`. Geprueft wird genau der Teil, der ohne
// Halo auf dem Tisch pruefbar ist — Rahmung, Zusammensetzen, Audio-Flags,
// WAV-Kopf. Ungetestet bleibt nur der Bluetooth-Unterbau.
package de.kat56.agent

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class HaloTest {

    /** Attrappe statt Bluetooth: merkt sich, was geschrieben wurde. */
    private class FakeLink(override val mtu: Int = 247,
                           override val isHalo: Boolean = true) : HaloLink {
        val packets = mutableListOf<ByteArray>()
        val strings = mutableListOf<String>()
        override fun write(packet: ByteArray) { packets.add(packet) }
        override fun writeString(text: String) { strings.add(text) }
    }

    // ---- Groessen --------------------------------------------------------

    @Test
    fun `Halo meldet mehr MTU als es vertraegt, deshalb zwei Byte Abzug`() {
        assertEquals(243, Halo.maxDataLength(247, isHalo = false))
        assertEquals(241, Halo.maxDataLength(247, isHalo = true))
        assertEquals(244, Halo.maxStringLength(247, isHalo = false))
        assertEquals(242, Halo.maxStringLength(247, isHalo = true))
    }

    // ---- Rahmung ---------------------------------------------------------

    @Test
    fun `kurze Nachricht passt in ein Paket mit Laengenkopf`() {
        val payload = "hallo".toByteArray()
        val p = Halo.packets(0x0a, payload, maxDataLength = 64)
        assertEquals(1, p.size)
        assertEquals(Halo.DATA_FLAG, p[0][0])
        assertEquals(0x0a.toByte(), p[0][1])
        assertEquals(0, p[0][2].toInt())              // Laenge hi
        assertEquals(5, p[0][3].toInt())              // Laenge lo
        assertArrayEquals(payload, p[0].copyOfRange(4, p[0].size))
    }

    @Test
    fun `leere Nutzdaten ergeben ein Paket mit Laenge null`() {
        val p = Halo.packets(0x0b, ByteArray(0), maxDataLength = 64)
        assertEquals(1, p.size)
        assertEquals(4, p[0].size)
        assertEquals(0, p[0][2].toInt())
        assertEquals(0, p[0][3].toInt())
    }

    @Test
    fun `lange Nachricht wird zerlegt und kommt vollstaendig wieder zusammen`() {
        val payload = ByteArray(5000) { (it % 251).toByte() }
        val maxData = 241
        val p = Halo.packets(0x0a, payload, maxData)
        assertTrue("mehrere Pakete erwartet", p.size > 1)
        // Kein Paket ueberschreitet, was die Characteristic vertraegt.
        p.forEach { assertTrue("Paket zu gross: ${it.size}", it.size <= Halo.maxPacket(maxData)) }
        // Jedes Paket traegt Kopf und Code.
        p.forEach {
            assertEquals(Halo.DATA_FLAG, it[0])
            assertEquals(0x0a.toByte(), it[1])
        }
        val (code, back) = Halo.reassemble(p)
        assertEquals(0x0a, code)
        assertArrayEquals(payload, back)
    }

    @Test
    fun `Grenzfaelle an der Paketgrenze bleiben verlustfrei`() {
        val maxData = 32
        val chunk = maxData - 1
        // genau die Groessen rund um die Uebergaenge: erstes Paket fasst
        // chunk-2 Byte, jedes weitere chunk.
        for (n in listOf(chunk - 3, chunk - 2, chunk - 1, chunk, chunk + 1,
                         2 * chunk - 2, 2 * chunk - 1, 2 * chunk)) {
            val payload = ByteArray(n) { (it and 0x7F).toByte() }
            val p = Halo.packets(0x22, payload, maxData)
            p.forEach { assertTrue("n=$n Paket zu gross", it.size <= Halo.maxPacket(maxData)) }
            val (_, back) = Halo.reassemble(p)
            assertArrayEquals("n=$n kam veraendert zurueck", payload, back)
        }
    }

    @Test
    fun `Nutzdaten ueber 65535 Byte werden abgelehnt`() {
        var thrown = false
        try {
            Halo.packets(0x0a, ByteArray(65536), maxDataLength = 241)
        } catch (e: IllegalArgumentException) {
            thrown = true
        }
        assertTrue("zu grosse Nachricht muss auffallen", thrown)
    }

    // ---- Sitzung ---------------------------------------------------------

    @Test
    fun `Sitzung schreibt Text im Format der SDK-Vorlage`() {
        val link = FakeLink()
        val s = HaloSession(link)
        s.showText("Antwort des Agenten", x = 1, y = 1, color = 1, spacing = 4)
        assertEquals(1, link.packets.size)
        val (code, payload) = Halo.reassemble(link.packets)
        assertEquals(HaloSession.Code.TEXT, code)
        // Kopf wie in plain_text.lua: x, y (je 16 Bit), Farbe, Zeilenabstand
        assertEquals(1, ((payload[0].toInt() and 0xFF) shl 8) or (payload[1].toInt() and 0xFF))
        assertEquals(1, ((payload[2].toInt() and 0xFF) shl 8) or (payload[3].toInt() and 0xFF))
        assertEquals(1, payload[4].toInt())
        assertEquals(4, payload[5].toInt())
        assertEquals("Antwort des Agenten",
            String(payload, 6, payload.size - 6, Charsets.UTF_8))
    }

    @Test
    fun `Anzeige loeschen sendet den Code ohne Rumpf`() {
        val link = FakeLink()
        HaloSession(link).clear()
        val (code, payload) = Halo.reassemble(link.packets)
        assertEquals(HaloSession.Code.CLEAR, code)
        assertEquals(0, payload.size)
    }

    @Test
    fun `Mikrofonstart traegt Abtastrate und Bittiefe`() {
        val link = FakeLink()
        HaloSession(link).startAudio(sampleRate = 8000, bitDepth = 16)
        val (code, payload) = Halo.reassemble(link.packets)
        assertEquals(HaloSession.Code.AUDIO_START, code)
        assertEquals(3, payload.size)
        assertEquals(8000, ((payload[0].toInt() and 0xFF) shl 8) or (payload[1].toInt() and 0xFF))
        assertEquals(16, payload[2].toInt())
    }

    @Test
    fun `langer Text wird in mehrere Pakete zerlegt und bleibt lesbar`() {
        val link = FakeLink(mtu = 100)
        val s = HaloSession(link)
        val text = "Lange Antwort. ".repeat(60)          // ~900 Zeichen
        s.showText(text, limit = 2000)
        assertTrue(link.packets.size > 1)
        val (_, payload) = Halo.reassemble(link.packets)
        assertEquals(text, String(payload, 6, payload.size - 6, Charsets.UTF_8))
    }

    @Test
    fun `Lua-Modul geht in Haeppchen und wird auf der Brille gespeichert`() {
        val link = FakeLink(mtu = 100)
        HaloSession(link).uploadLua("plain_text", "x".repeat(500))
        assertTrue("mehrere Zeilen erwartet", link.strings.size > 3)
        assertTrue(link.strings.first().startsWith("__m="))
        assertTrue(link.strings.last().contains("plain_text.lua"))
        assertTrue(link.strings.last().contains("f:close()"))
        // Alle Haeppchen zusammen ergeben wieder die Quelle.
        val body = link.strings.filter { it.contains("[==[") }
            .joinToString("") { it.substringAfter("[==[").substringBeforeLast("]==]") }
        assertEquals("x".repeat(500), body)
    }

    // ---- Audio -----------------------------------------------------------

    @Test
    fun `Audiostuecke werden gesammelt bis das Schlussstueck kommt`() {
        val c = Halo.AudioCollector()
        c.feed(byteArrayOf(Halo.AUDIO_CHUNK, 1, 2, 3))
        assertTrue("noch nicht fertig", !c.complete)
        val live = c.feed(byteArrayOf(Halo.AUDIO_CHUNK, 4, 5))
        assertArrayEquals(byteArrayOf(4, 5), live)
        c.feed(byteArrayOf(Halo.AUDIO_FINAL, 6))
        assertTrue("Schlussstueck muss beenden", c.complete)
        assertArrayEquals(byteArrayOf(1, 2, 3, 4, 5, 6), c.pcm())
    }

    @Test
    fun `fremde Antworten landen nicht im Mitschnitt`() {
        val c = Halo.AudioCollector()
        c.feed(byteArrayOf(Halo.AUDIO_CHUNK, 9))
        c.feed(byteArrayOf(0x02, 7, 7, 7))            // z.B. eine Tap-Meldung
        c.feed(byteArrayOf(Halo.AUDIO_FINAL, 8))
        assertArrayEquals(byteArrayOf(9, 8), c.pcm())
    }

    @Test
    fun `WAV-Kopf beschreibt die Daten korrekt`() {
        val pcm = ByteArray(1000) { 7 }
        val w = Halo.wav(pcm, sampleRate = 8000, bitsPerSample = 16, channels = 1)
        assertEquals(44 + pcm.size, w.size)
        assertEquals("RIFF", String(w, 0, 4, Charsets.US_ASCII))
        assertEquals("WAVE", String(w, 8, 4, Charsets.US_ASCII))
        assertEquals("fmt ", String(w, 12, 4, Charsets.US_ASCII))
        assertEquals("data", String(w, 36, 4, Charsets.US_ASCII))
        fun le32(at: Int) = (w[at].toInt() and 0xFF) or ((w[at + 1].toInt() and 0xFF) shl 8) or
                ((w[at + 2].toInt() and 0xFF) shl 16) or ((w[at + 3].toInt() and 0xFF) shl 24)
        fun le16(at: Int) = (w[at].toInt() and 0xFF) or ((w[at + 1].toInt() and 0xFF) shl 8)
        assertEquals(36 + pcm.size, le32(4))          // Dateigroesse ohne die ersten 8 Byte
        assertEquals(16, le32(16))                    // PCM-Kopf
        assertEquals(1, le16(20))                     // unkomprimiert
        assertEquals(1, le16(22))                     // mono
        assertEquals(8000, le32(24))                  // Abtastrate
        assertEquals(16000, le32(28))                 // Byte pro Sekunde
        assertEquals(2, le16(32))                     // Blockgroesse
        assertEquals(16, le16(34))                    // Bit pro Wert
        assertEquals(pcm.size, le32(40))
    }

    // ---- Bestaetigungen --------------------------------------------------

    @Test
    fun `Datenrahmen und Interpretertext werden auseinandergehalten`() {
        // Die Brille schickt beides ueber dieselbe Characteristic.
        val ackWire = byteArrayOf(0x01, 0x00, 0x00)
        val luaText = "Fehler in Zeile 3".toByteArray()
        assertTrue(Halo.isDataFrame(ackWire))
        assertTrue(!Halo.isDataFrame(luaText))
        assertArrayEquals(byteArrayOf(0x00, 0x00), Halo.frameOf(ackWire))
        assertArrayEquals(luaText, Halo.frameOf(luaText))     // Text bleibt unangetastet
    }

    @Test
    fun `Paketbestaetigungen werden erkannt und von Nutzdaten unterschieden`() {
        // Auf der Leitung 01 00 00 -> im Rahmen 00 00.
        assertTrue(Halo.isAck(Halo.frameOf(byteArrayOf(0x01, 0x00, 0x00))))
        assertTrue(Halo.isAck(Halo.frameOf(byteArrayOf(0x01, 0x00, 0x01))))
        assertTrue(Halo.isAckError(Halo.frameOf(byteArrayOf(0x01, 0x00, 0x01))))
        assertTrue(!Halo.isAckError(Halo.frameOf(byteArrayOf(0x01, 0x00, 0x00))))
        // Audio- und Fotoantworten duerfen nicht als Bestaetigung durchgehen.
        assertTrue(!Halo.isAck(byteArrayOf(Halo.AUDIO_CHUNK, 1, 2)))
        assertTrue(!Halo.isAck(byteArrayOf(Halo.PHOTO_FINAL, 1, 2)))
        assertTrue(!Halo.isAck(byteArrayOf(Halo.ACK_FLAG)))   // zu kurz
    }

    @Test
    fun `8-Bit-Mitschnitt wird fuer WAV ins Vorzeichenlose verschoben`() {
        val signed = byteArrayOf(0, 127, -128, -1)
        val unsigned = Halo.signed8ToUnsigned8(signed)
        assertEquals(128, unsigned[0].toInt() and 0xFF)   // 0 -> Mitte
        assertEquals(255, unsigned[1].toInt() and 0xFF)   // Maximum
        assertEquals(0, unsigned[2].toInt() and 0xFF)     // Minimum
        assertEquals(127, unsigned[3].toInt() and 0xFF)
    }
}
