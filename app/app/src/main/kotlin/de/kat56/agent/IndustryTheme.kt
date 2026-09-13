// kAIm56 KatAgent — Android client for the kAIm56 agent platform
// Copyright (C) 2026 Ulrich Neidel
// SPDX-License-Identifier: AGPL-3.0-or-later
package de.kat56.agent

// Farben, Schrift und Kanten aus dem Prototyp "KatAgent Prototype" (claude.ai/design).
//
// Dieser Prototyp loest die Industry-Tokens ab, die hier vorher standen. Er ist
// die neuere Vorlage und ausdruecklich fuer DIESE App gezeichnet, also gewinnt
// er, wo er widerspricht:
//   Schrift   IBM Plex Sans / IBM Plex Mono   statt Barlow / Barlow Condensed
//   Band      dunkel (#0E1218)                statt Slate-auf-Hell
//   Kanten    rund (10-22 px)                 statt durchgehend eckig
//   Rahmen    Haarlinie ohne Registermarken   statt Blueprint-Ecken
//
// Der Prototyp ist in genau EINEM Band definiert (dark="true", #0E1218). Ein
// helles Band steht dort nicht — die App laeuft deshalb immer dunkel, statt
// eine zweite Palette zu erfinden.

import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Shapes
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.Font
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

val Plex = FontFamily(
    Font(R.font.plex_sans_regular, FontWeight.Normal),
    Font(R.font.plex_sans_medium, FontWeight.Medium),
    Font(R.font.plex_sans_semibold, FontWeight.SemiBold),
    Font(R.font.plex_sans_bold, FontWeight.Bold),
)

val PlexMono = FontFamily(
    Font(R.font.plex_mono_regular, FontWeight.Normal),
    Font(R.font.plex_mono_medium, FontWeight.Medium),
)

/**
 * Die Farbwerte des Prototyps, 1:1. Namen nach Rolle, nicht nach Ton, damit
 * Aenderungen an der Vorlage hier genau eine Stelle treffen.
 */
object Kat {
    val bg = Color(0xFF0E1218)            // Bildschirmgrund
    val surface = Color(0xFF171D26)       // Karte, Menue, Agenten-Blase, Eingabe
    val elevated = Color(0xFF12161D)      // Schublade, Anhang-Blatt
    val tile = Color(0xFF1B2430)          // Avatar-/Icon-Kachel, inaktiver Senden-Knopf
    val tileBorder = Color(0xFF26303E)
    val border = Color(0xFF262F3C)
    val borderFocus = Color(0xFF35507A)

    val accent = Color(0xFF2D5C96)        // gefuellte Aktion, eigene Nachricht
    val accentHover = Color(0xFF356BAD)
    val accentText = Color(0xFF7FB0E8)    // Akzent auf dunklem Grund
    val accentBright = Color(0xFFA9CBF2)
    val onAccent = Color(0xFFF0F5FB)
    val chipSel = Color(0xFF20304A)       // gewaehlter Chip / gewaehltes Preset
    val rowSel = Color(0xFF1B2836)        // offener Chat in der Schublade

    val text = Color(0xFFE8ECF2)
    val textStrong = Color(0xFFDCE3EC)
    val textDim = Color(0xFFB7C0CE)
    val textMuted = Color(0xFF8A94A6)
    val textFaint = Color(0xFF7A8598)
    val textSubtle = Color(0xFF5C6879)
    val textGhost = Color(0xFF3D4655)

    val green = Color(0xFF4CC38A)
    val red = Color(0xFFE06C75)

    // rgba(255,255,255,a) aus der Vorlage
    val hairline = Color(0x0FFFFFFF)      // 0.06
    val hairlineStrong = Color(0x12FFFFFF) // 0.07
    val hover = Color(0x0DFFFFFF)         // 0.05
    val wash = Color(0x0AFFFFFF)          // 0.04
    val scrim = Color(0x80000000)         // 0.5

    /**
     * Agentenfarben aus der Vorlage. Unbekannte Namen bleiben neutral — genau
     * wie im Prototyp (`agentColor[c.agent] || '#8A94A6'`).
     */
    fun agent(name: String): Color = when (name) {
        "orchestrator" -> Color(0xFF7FB0E8)
        "hass" -> Color(0xFF5BC4B0)
        "ephemeral" -> Color(0xFFC9A15B)
        "research" -> Color(0xFFB58BE8)
        else -> textMuted
    }
}

val KatColors = darkColorScheme(
    primary = Kat.accent,
    onPrimary = Kat.onAccent,
    primaryContainer = Kat.chipSel,
    onPrimaryContainer = Kat.accentBright,
    secondary = Kat.accentText,
    onSecondary = Kat.bg,
    background = Kat.bg,
    onBackground = Kat.text,
    surface = Kat.surface,
    onSurface = Kat.textStrong,
    surfaceVariant = Kat.tile,
    onSurfaceVariant = Kat.textFaint,
    outline = Kat.border,
    outlineVariant = Kat.hairlineStrong,
    error = Kat.red,
    onError = Kat.bg,
)

/**
 * Die Radien, die der Prototyp tatsaechlich nennt: 10 px an Eingabefeldern und
 * Menuezeilen, 14 px an Karten, 18 px an Chips, 22 px an den runden Knoepfen.
 */
val KatShapes = Shapes(
    extraSmall = RoundedCornerShape(8.dp),
    small = RoundedCornerShape(10.dp),
    medium = RoundedCornerShape(14.dp),
    large = RoundedCornerShape(18.dp),
    extraLarge = RoundedCornerShape(22.dp),
)

private fun sans(size: Double, weight: FontWeight = FontWeight.Normal, lh: Double = 1.4, ls: Double = 0.0) =
    TextStyle(
        fontFamily = Plex, fontWeight = weight,
        fontSize = size.sp, lineHeight = (size * lh).sp, letterSpacing = ls.sp,
    )

/**
 * Die Groessen stehen so in der Vorlage: 17/600 im Kopf, 15 in der Blase,
 * 14.5 in Listenzeilen, 13.5 an den Chips, 12 an Unterzeilen, 11 an der
 * Fussnote. 11.5/600 versal mit 0.08em Sperrung ist der Abschnittstitel.
 */
val KatTypography = Typography(
    displayLarge = sans(32.0, FontWeight.SemiBold, 1.15, -0.32),
    displayMedium = sans(25.0, FontWeight.SemiBold, 1.15, -0.25),
    displaySmall = sans(20.0, FontWeight.SemiBold, 1.2, -0.2),
    headlineLarge = sans(25.0, FontWeight.SemiBold, 1.2, -0.25),
    headlineMedium = sans(20.0, FontWeight.SemiBold, 1.2, -0.2),
    headlineSmall = sans(18.0, FontWeight.SemiBold, 1.25, -0.18),
    titleLarge = sans(18.0, FontWeight.SemiBold, 1.3, -0.18),
    titleMedium = sans(17.0, FontWeight.SemiBold, 1.3, -0.17),
    titleSmall = sans(14.5, FontWeight.Medium, 1.35),
    bodyLarge = sans(15.0, FontWeight.Normal, 1.5),
    bodyMedium = sans(14.0, FontWeight.Normal, 1.45),
    bodySmall = sans(13.0, FontWeight.Normal, 1.45),
    labelLarge = sans(14.0, FontWeight.SemiBold, 1.3),
    labelMedium = sans(12.0, FontWeight.Normal, 1.35),
    // Abschnittstitel: versal, gesperrt
    labelSmall = sans(11.5, FontWeight.SemiBold, 1.3, 0.92),
)
