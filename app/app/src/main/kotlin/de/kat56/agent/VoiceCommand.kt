// kAIm56 KatAgent — Android client for the kAIm56 agent platform
// Copyright (C) 2026 Ulrich Neidel
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Sprachbefehle: was die Spracherkennung liefert, wird ERST hier geprueft und
// nur dann an den Agenten geschickt, wenn kein lokaler Befehl darinsteckt.
// Grund: "mach einen Screenshot" muss das Telefon tun, nicht das Modell
// beantworten.
//
// Reine Textverarbeitung, keine Android-Abhaengigkeit — damit pruefbar, ohne
// etwas aufzunehmen.
package de.kat56.agent

object VoiceCommand {

    enum class Action {
        /** Bildschirm des Telefons aufnehmen. */
        SCREENSHOT,
        /** Foto mit der Kamera der Brille. */
        PHOTO,
        /** Laufende Antwort/Aufnahme abbrechen. */
        STOP,
    }

    /** [rest] ist der Text ohne den Befehl — die Frage zum Bild, falls eine
     *  mitgesprochen wurde ("Screenshot, was steht da?"). */
    data class Parsed(val action: Action, val rest: String)

    // Ausloeser, deutsch und englisch. Absichtlich knapp gehalten: je mehr
    // Wendungen erkannt werden, desto haeufiger verschluckt der Erkenner eine
    // echte Frage an den Agenten.
    private val triggers = listOf(
        Action.SCREENSHOT to listOf(
            "screenshot", "bildschirmfoto", "bildschirm aufnehmen",
            "screen shot", "capture screen"),
        Action.PHOTO to listOf(
            "foto", "photo", "bild aufnehmen", "take a picture", "take a photo",
            "kamera", "camera"),
        Action.STOP to listOf(
            "stopp", "stop", "abbrechen", "halt", "cancel"),
    )

    // Hoeflichkeitsfloskeln, die vor dem Befehl stehen duerfen.
    private val fillers = listOf(
        "bitte", "mal", "mir", "einen", "eine", "ein", "das", "den", "die",
        "kannst du", "koenntest du", "kannst du mal", "please", "can you",
        "could you", "just", "a", "an", "the",
    )

    /**
     * Sucht einen Befehl am ANFANG der Aeusserung. Nur dort: mitten im Satz
     * ("... und dann war da ein Foto von Oma") waere es fast immer falsch.
     * Rueckgabe null = kein Befehl, der Text gehoert dem Agenten.
     */
    fun parse(text: String): Parsed? {
        var s = normalize(text)
        if (s.isEmpty()) return null
        // Einleitende Verben und Floskeln abraeumen: "mach mir bitte einen …"
        var changed = true
        while (changed) {
            changed = false
            for (w in listOf("mach", "mache", "machen", "nimm", "nimm auf",
                             "erstelle", "erstell", "zeig", "zeige", "make", "take") + fillers) {
                if (s == w) return null                     // nur Floskel, kein Befehl
                if (s.startsWith("$w ")) { s = s.removePrefix("$w ").trim(); changed = true }
            }
        }
        for ((action, words) in triggers) {
            for (w in words) {
                if (s == w) return Parsed(action, "")
                if (s.startsWith("$w ")) return Parsed(action, cleanRest(s.removePrefix("$w ")))
                // "screenshot, was steht da" — Komma faellt in normalize() weg,
                // hier bleibt nur der Rest.
            }
        }
        return null
    }

    /** Kleinschreibung, Satzzeichen weg, Mehrfachleerzeichen zusammen. */
    private fun normalize(text: String): String =
        text.lowercase()
            .replace(Regex("[.,!?;:]+"), " ")
            .replace(Regex("\\s+"), " ")
            .trim()

    /** Fuellwoerter am Anfang der Restfrage entfernen ("und was steht da"). */
    private fun cleanRest(rest: String): String {
        var r = rest.trim()
        for (w in listOf("und", "dann", "and", "then")) {
            if (r.startsWith("$w ")) r = r.removePrefix("$w ").trim()
        }
        return r
    }
}
