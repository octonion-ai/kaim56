// kAIm56 KatAgent — Android client for the kAIm56 agent platform
// Copyright (C) 2026 Ulrich Neidel
// SPDX-License-Identifier: AGPL-3.0-or-later
package de.kat56.agent

// Bausteine des Prototyps "KatAgent Prototype". Ersetzt die Industry-Bausteine
// (blueprintFrame/BlueprintBox mit Registermarken) — der Prototyp kennt keine
// Registermarken, sondern Karten mit Haarlinie und 14-px-Radius.

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.RowScope
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.LocalTextStyle
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.animation.core.animateDpAsState
import androidx.compose.animation.animateColorAsState

/** Klickflaeche ohne Welligkeits-Kringel — der Prototyp faerbt nur den Grund. */
@Composable
fun Modifier.tap(enabled: Boolean = true, onClick: () -> Unit): Modifier {
    val src = remember { MutableInteractionSource() }
    return this.clickable(interactionSource = src, indication = null, enabled = enabled, onClick = onClick)
}

/** Abschnittstitel: versal, gesperrt, gedaempft (11.5/600, 0.08em). */
@Composable
fun Kicker(text: String, modifier: Modifier = Modifier) {
    Text(
        text.uppercase(),
        modifier,
        style = MaterialTheme.typography.labelSmall,
        color = Kat.textSubtle,
    )
}

/** Karte: #171D26, Haarlinie, Radius 14. */
@Composable
fun KatCard(
    modifier: Modifier = Modifier,
    padding: PaddingValues = PaddingValues(horizontal = 12.dp, vertical = 14.dp),
    spacing: Dp = 12.dp,
    content: @Composable ColumnScope.() -> Unit,
) {
    Column(
        modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(14.dp))
            .background(Kat.surface)
            .border(1.dp, Kat.hairlineStrong, RoundedCornerShape(14.dp))
            .padding(padding),
        verticalArrangement = Arrangement.spacedBy(spacing),
        content = content,
    )
}

/** Runder 44-dp-Knopf des Kopfes und der Eingabezeile. */
@Composable
fun RoundIconButton(
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    size: Dp = 44.dp,
    background: Color = Color.Transparent,
    enabled: Boolean = true,
    content: @Composable BoxScope.() -> Unit,
) {
    Box(
        modifier
            .size(size)
            .clip(CircleShape)
            .background(background)
            .tap(enabled, onClick),
        contentAlignment = Alignment.Center,
        content = content,
    )
}

/** Haarlinie (rgba(255,255,255,0.06)). */
@Composable
fun Hairline(modifier: Modifier = Modifier, color: Color = Kat.hairline) {
    Box(modifier.fillMaxWidth().height(1.dp).background(color))
}

/** Schalter 44x26 mit 20-dp-Knopf, Bahn #2D5C96 / #262F3C. */
@Composable
fun KatSwitch(checked: Boolean, onToggle: () -> Unit, modifier: Modifier = Modifier) {
    val knob by animateDpAsState(if (checked) 21.dp else 3.dp, label = "knob")
    val track by animateColorAsState(if (checked) Kat.accent else Kat.border, label = "track")
    Box(
        modifier
            .size(44.dp, 26.dp)
            .clip(RoundedCornerShape(13.dp))
            .background(track)
            .tap { onToggle() }
    ) {
        Box(
            Modifier
                .offset(x = knob, y = 3.dp)
                .size(20.dp)
                .clip(CircleShape)
                .background(Kat.onAccent)
        )
    }
}

/** Statusplakette der Aufgabenliste: 11/600, Radius 9, Flaeche + Rand + Schrift. */
@Composable
fun StatusBadge(text: String, bg: Color, fg: Color, borderColor: Color, modifier: Modifier = Modifier) {
    Box(
        modifier
            .clip(RoundedCornerShape(9.dp))
            .background(bg)
            .border(1.dp, borderColor, RoundedCornerShape(9.dp))
            .padding(horizontal = 9.dp, vertical = 3.dp)
    ) {
        Text(text, fontSize = 11.sp, fontWeight = FontWeight.SemiBold, fontFamily = Plex, color = fg, maxLines = 1)
    }
}

/** Kleine Plakette ohne Rand (Preset-Marke). */
@Composable
fun MiniTag(text: String, bg: Color, fg: Color, modifier: Modifier = Modifier) {
    Box(
        modifier
            .clip(RoundedCornerShape(8.dp))
            .background(bg)
            .padding(horizontal = 8.dp, vertical = 2.dp)
    ) {
        Text(text, fontSize = 10.5.sp, fontWeight = FontWeight.SemiBold, fontFamily = Plex, color = fg, maxLines = 1)
    }
}

/**
 * Eingabefeld der Einstellungen: 40 dp hoch, Radius 10, Grund #0E1218 auf der
 * Karte. Bewusst BasicTextField statt OutlinedTextField — Material zeichnet
 * sonst seinen eigenen Rahmen und sein eigenes Label darueber.
 */
@Composable
fun KatField(
    value: String,
    onValueChange: (String) -> Unit,
    modifier: Modifier = Modifier,
    mono: Boolean = false,
    password: Boolean = false,
    fontSize: Float = 13f,
    height: Dp = 40.dp,
    placeholder: String = "",
    keyboardOptions: KeyboardOptions = KeyboardOptions.Default,
) {
    val style = TextStyle(
        fontFamily = if (mono) PlexMono else Plex,
        fontSize = fontSize.sp,
        color = Kat.text,
    )
    Box(
        modifier
            .fillMaxWidth()
            .height(height)
            .clip(RoundedCornerShape(10.dp))
            .background(Kat.bg)
            .border(1.dp, Kat.border, RoundedCornerShape(10.dp))
            .padding(horizontal = 12.dp),
        contentAlignment = Alignment.CenterStart,
    ) {
        BasicTextField(
            value = value,
            onValueChange = onValueChange,
            textStyle = style,
            singleLine = true,
            cursorBrush = SolidColor(Kat.accentText),
            visualTransformation = if (password) PasswordVisualTransformation() else VisualTransformation.None,
            keyboardOptions = keyboardOptions,
            modifier = Modifier.fillMaxWidth(),
            decorationBox = { inner ->
                if (value.isEmpty() && placeholder.isNotEmpty())
                    Text(placeholder, style = style.copy(color = Kat.textSubtle), maxLines = 1)
                inner()
            },
        )
    }
}

/** Beschriftetes Feld: 12-px-Label in #7A8598 ueber dem Eingabefeld. */
@Composable
fun LabeledField(
    label: String,
    value: String,
    onValueChange: (String) -> Unit,
    modifier: Modifier = Modifier,
    mono: Boolean = false,
    password: Boolean = false,
    fontSize: Float = 13f,
) {
    Column(modifier, verticalArrangement = Arrangement.spacedBy(5.dp)) {
        Text(label, fontSize = 12.sp, fontFamily = Plex, color = Kat.textFaint)
        KatField(value, onValueChange, mono = mono, password = password, fontSize = fontSize)
    }
}

/** Gefuellte Aktion: 42 dp, Radius 21, #2D5C96. */
@Composable
fun FilledPill(
    text: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    height: Dp = 42.dp,
    leading: @Composable (RowScope.() -> Unit)? = null,
    trailing: @Composable (RowScope.() -> Unit)? = null,
) {
    Row(
        modifier
            .height(height)
            .clip(RoundedCornerShape(height / 2))
            .background(if (enabled) Kat.accent else Kat.tile)
            .tap(enabled, onClick)
            .padding(horizontal = 16.dp),
        horizontalArrangement = Arrangement.spacedBy(8.dp, Alignment.CenterHorizontally),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        leading?.invoke(this)
        Text(
            text, fontSize = 14.sp, fontWeight = FontWeight.SemiBold, fontFamily = Plex,
            color = if (enabled) Kat.onAccent else Kat.textSubtle, maxLines = 1, overflow = TextOverflow.Ellipsis,
        )
        trailing?.invoke(this)
    }
}

/** Zweite Aktion: nur Rand #35507A, Schrift #A9CBF2. */
@Composable
fun OutlinePill(
    text: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    height: Dp = 42.dp,
) {
    Box(
        modifier
            .height(height)
            .clip(RoundedCornerShape(height / 2))
            .border(1.dp, Kat.borderFocus, RoundedCornerShape(height / 2))
            .tap(enabled, onClick)
            .padding(horizontal = 16.dp),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            text, fontSize = 14.sp, fontWeight = FontWeight.SemiBold, fontFamily = Plex,
            color = Kat.accentBright, maxLines = 1, overflow = TextOverflow.Ellipsis,
        )
    }
}
