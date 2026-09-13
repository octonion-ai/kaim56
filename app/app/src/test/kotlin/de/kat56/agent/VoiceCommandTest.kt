// kAIm56 KatAgent — Android client for the kAIm56 agent platform
// Copyright (C) 2026 Ulrich Neidel
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Sprachbefehle erkennen — und vor allem: NICHT erkennen, wo eine echte Frage
// an den Agenten steht. Der teure Fehler waere, eine Frage zu verschlucken.
package de.kat56.agent

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class VoiceCommandTest {

    @Test
    fun `der blanke Befehl wird erkannt`() {
        assertEquals(VoiceCommand.Action.SCREENSHOT, VoiceCommand.parse("Screenshot")?.action)
        assertEquals(VoiceCommand.Action.PHOTO, VoiceCommand.parse("Foto")?.action)
        assertEquals(VoiceCommand.Action.STOP, VoiceCommand.parse("Stopp!")?.action)
    }

    @Test
    fun `Hoeflichkeit und Verben davor stoeren nicht`() {
        val screenshots = listOf(
            "Mach mal bitte einen Screenshot",
            "mach einen screenshot",
            "Kannst du bitte einen Screenshot machen",
            "Bildschirmfoto",
        )
        for (s in screenshots) {
            assertEquals("nicht erkannt: '$s'",
                VoiceCommand.Action.SCREENSHOT, VoiceCommand.parse(s)?.action)
        }
        for (s in listOf("nimm ein Foto", "Take a photo", "mach ein Bild aufnehmen")) {
            assertEquals("nicht erkannt: '$s'",
                VoiceCommand.Action.PHOTO, VoiceCommand.parse(s)?.action)
        }
    }

    @Test
    fun `die mitgesprochene Frage bleibt erhalten`() {
        val p = VoiceCommand.parse("Screenshot, was steht da?")
        assertEquals(VoiceCommand.Action.SCREENSHOT, p?.action)
        assertEquals("was steht da", p?.rest)
    }

    @Test
    fun `Fuellwoerter vor der Restfrage fallen weg`() {
        assertEquals("was ist das", VoiceCommand.parse("Foto und was ist das?")?.rest)
    }

    @Test
    fun `echte Fragen an den Agenten werden NICHT abgefangen`() {
        // Das ist der teure Fehlerfall: eine Frage, die im Telefon haengen bleibt.
        for (s in listOf(
            "Was war auf dem Screenshot von gestern?",
            "Schick mir das Foto von der Wanderung",
            "Erklaer mir, wie ein Screenshot funktioniert",
            "Wie ist das Wetter?",
            "Stoppuhr auf drei Minuten stellen",
        )) {
            assertNull("faelschlich als Befehl erkannt: '$s'", VoiceCommand.parse(s))
        }
    }

    @Test
    fun `leere oder blosse Floskeln sind kein Befehl`() {
        assertNull(VoiceCommand.parse(""))
        assertNull(VoiceCommand.parse("   "))
        assertNull(VoiceCommand.parse("bitte"))
        assertNull(VoiceCommand.parse("mach"))
    }
}
