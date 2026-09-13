// kAIm56 KatAgent — Android client for the kAIm56 agent platform
// Copyright (C) 2026 Ulrich Neidel
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Der ganze Weg ohne Brille: Verbinden, Module laden, Aufnahme, Spracherkennung,
// Instanz, Antwort auf dem Display. Statt Bluetooth steht HaloDryLink dahinter —
// dieselbe Attrappe, mit der sich die Anbindung auch in der App trocken
// durchklicken laesst.
package de.kat56.agent

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class HaloControllerTest {

    private fun controller(
        link: HaloDryLink = HaloDryLink(),
        heard: String? = "wie ist das Wetter",
        answer: String = "Sonnig, 21 Grad.",
        status: MutableList<String> = mutableListOf(),
    ): Pair<HaloController, HaloDryLink> {
        val c = HaloController(
            luaSource = { name -> "-- $name\nreturn {}" },
            transcribe = { heard },
            ask = { answer },
            onStatus = { status.add(it) },
        )
        c.attach(link)
        return c to link
    }

    @Test
    fun `beim Verbinden gehen alle Module in der richtigen Reihenfolge hoch`() {
        val (c, link) = controller()
        assertEquals(c.modules, link.uploaded)
        // katagent zuletzt: es verlangt die anderen beim Start.
        assertEquals("katagent", link.uploaded.last())
    }

    @Test
    fun `nach dem Verbinden ist das Display leer`() {
        val (_, link) = controller()
        assertTrue(link.display.isEmpty())
    }

    @Test
    fun `Zuhoeren schaltet das Mikrofon ein und sagt es an`() {
        val (c, link) = controller()
        c.startListening()
        assertTrue("Mikrofon muss laufen", link.recording)
        assertEquals(listOf("… hört zu"), link.display)
    }

    @Test
    fun `der ganze Weg endet mit der Antwort auf dem Display`() {
        val status = mutableListOf<String>()
        val (c, link) = controller(status = status)
        c.startListening()
        val answer = c.stopAndAsk(ByteArray(64))
        assertEquals("Sonnig, 21 Grad.", answer)
        assertTrue("Mikrofon muss aus sein", !link.recording)
        assertEquals(listOf("Sonnig, 21 Grad."), link.display)
        assertTrue("die Frage wird zwischendurch angezeigt",
            status.any { it.contains("wie ist das Wetter") })
    }

    @Test
    fun `wenn nichts verstanden wurde, wird nicht gefragt`() {
        var asked = 0
        val link = HaloDryLink()
        val c = HaloController(
            luaSource = { "" },
            transcribe = { null },                 // Spracherkennung liefert nichts
            ask = { asked++; "sollte nicht passieren" },
        )
        c.attach(link)
        c.startListening()
        assertNull(c.stopAndAsk(ByteArray(8)))
        assertEquals("es darf keine Frage rausgehen", 0, asked)
        assertEquals(listOf("nichts verstanden"), link.display)
    }

    @Test
    fun `ohne Verbindung passiert nichts, statt zu stuerzen`() {
        val c = HaloController({ "" }, { "x" }, { "y" })
        c.startListening()                         // keine Sitzung -> still
        assertNull(c.stopAndAsk(ByteArray(4)))
        c.show("egal")
    }

    // ---- Foto per Sprachbefehl -------------------------------------------

    @Test
    fun `gesprochenes Foto loest die Kamera aus und schickt das Bild mit`() {
        val link = HaloDryLink()
        var withImage: Pair<String, Int>? = null
        val c = HaloController(
            luaSource = { "" },
            transcribe = { "Foto, was ist das?" },
            ask = { "sollte nicht als reine Frage laufen" },
            askWithImage = { q, jpeg -> withImage = q to jpeg.size; "Das ist ein Kaffeebecher." },
            awaitPhoto = { link.photo },
        )
        c.attach(link)
        val answer = c.stopAndAsk(ByteArray(16))
        assertEquals("Das ist ein Kaffeebecher.", answer)
        // Die mitgesprochene Frage geht mit dem Bild an die Instanz.
        assertEquals("was ist das", withImage?.first)
        assertEquals(3, withImage?.second)
        assertEquals(listOf("Das ist ein Kaffeebecher."), link.display)
    }

    @Test
    fun `ohne mitgesprochene Frage wird die naheliegende gestellt`() {
        val link = HaloDryLink()
        var asked = ""
        val c = HaloController({ "" }, { "Foto" }, { "" },
            askWithImage = { q, _ -> asked = q; "ein Baum" }, awaitPhoto = { link.photo })
        c.attach(link)
        c.stopAndAsk(ByteArray(4))
        assertEquals("Was siehst du auf diesem Bild?", asked)
    }

    @Test
    fun `bleibt das Bild aus, wird das gesagt statt still zu scheitern`() {
        val link = HaloDryLink()
        var asked = 0
        val c = HaloController({ "" }, { "Foto" }, { "" },
            askWithImage = { _, _ -> asked++; "" }, awaitPhoto = { null })   // Kamera antwortet nicht
        c.attach(link)
        assertNull(c.photoAndAsk())
        assertEquals(0, asked)
        assertEquals(listOf("kein Bild bekommen"), link.display)
    }

    @Test
    fun `eine echte Frage laeuft weiter zur Instanz, nicht in die Kamera`() {
        val link = HaloDryLink()
        var plain = ""
        val c = HaloController({ "" }, { "Schick mir das Foto von gestern" },
            ask = { plain = it; "hier ist es" },
            askWithImage = { _, _ -> "FALSCH: Kamera ausgeloest" },
            awaitPhoto = { link.photo })
        c.attach(link)
        assertEquals("hier ist es", c.stopAndAsk(ByteArray(4)))
        assertEquals("Schick mir das Foto von gestern", plain)
        assertNull("die Kamera darf nicht ausgeloest worden sein", link.photo)
    }

    @Test
    fun `Abbrechen stoppt die Aufnahme und raeumt das Display`() {
        val link = HaloDryLink()
        val c = HaloController({ "" }, { "Stopp" }, { "sollte nicht gefragt werden" })
        c.attach(link)
        c.startListening()
        assertNull(c.stopAndAsk(ByteArray(4)))
        assertTrue(!link.recording)
        assertTrue(link.display.isEmpty())
    }

    // ---- Zeilenumbruch ---------------------------------------------------

    @Test
    fun `lange Antworten werden auf die Displaybreite umgebrochen`() {
        val (c, _) = controller()
        val wrapped = c.wrap("Der Agent antwortet hier mit einem laengeren Satz, " +
            "der nicht in eine Zeile passt.", width = 20)
        wrapped.split("\n").forEach {
            assertTrue("zu lang: '$it'", it.length <= 20)
        }
        // Kein Wort darf dabei verloren gehen.
        assertEquals("Der Agent antwortet hier mit einem laengeren Satz, der nicht in eine Zeile passt.",
            wrapped.replace("\n", " "))
    }

    @Test
    fun `ueberlange Woerter werden hart getrennt statt aus dem Bild zu laufen`() {
        val (c, _) = controller()
        val wrapped = c.wrap("Siehe https://agents.example.com/sehr/langer/pfad/zur/datei.txt", width = 16)
        wrapped.split("\n").forEach { assertTrue("zu lang: '$it'", it.length <= 16) }
        assertTrue(wrapped.replace("\n", "").contains("datei.txt"))
    }

    @Test
    fun `vorhandene Zeilenumbrueche bleiben erhalten`() {
        val (c, _) = controller()
        assertEquals("eins\nzwei\ndrei", c.wrap("eins\nzwei\ndrei", width = 20))
    }

    @Test
    fun `die Anzeige wird auf die Hoehe des Displays gekuerzt`() {
        val (c, link) = controller()
        c.show((1..20).joinToString("\n") { "Zeile $it" }, maxLines = 8)
        assertEquals(8, link.display.size)
        assertEquals("Zeile 1", link.display.first())
    }
}
