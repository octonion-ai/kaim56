// kAIm56 KatAgent — Android client for the kAIm56 agent platform
// Copyright (C) 2026 Ulrich Neidel
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Barge-in: cut the spoken reply when the user talks — and ONLY then. The
// expensive mistakes are a reply that stops on its own residual echo, and a
// user who talks and is not heard.
package de.kat56.agent

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class BargeInGateTest {

    private fun run(gate: BargeInGate, frames: List<Pair<Boolean, Double>>): List<BargeInGate.Event> =
        frames.map { (vad, rms) -> gate.frame(vad, rms) }

    private fun quiet(n: Int) = List(n) { false to 80.0 }
    private fun speech(n: Int) = List(n) { true to 2500.0 }

    @Test
    fun `nothing during warm-up, even loud voiced frames`() {
        val ev = run(BargeInGate(), speech(25))
        assertTrue(ev.none { it == BargeInGate.Event.START })
    }

    @Test
    fun `sustained speech after warm-up starts once and ends after the hang`() {
        val ev = run(BargeInGate(), quiet(30) + speech(20) + quiet(60))
        assertEquals(1, ev.count { it == BargeInGate.Event.START })
        assertEquals(1, ev.count { it == BargeInGate.Event.END })
        val start = ev.indexOf(BargeInGate.Event.START)
        val end = ev.indexOf(BargeInGate.Event.END)
        assertTrue("start within the strict window needs 8 voiced frames", start in 37..39)
        assertEquals("900 ms of silence (45 frames) end it", 50 + 45 - 1, end)
    }

    @Test
    fun `a short blip is not speech`() {
        val ev = run(BargeInGate(), quiet(50) + speech(3) + quiet(30))
        assertTrue(ev.none { it == BargeInGate.Event.START })
    }

    @Test
    fun `steady residual echo at a constant level never triggers`() {
        // The canceller has not converged: the VAD says speech, the level is
        // steady. It is the floor, not a voice above the floor.
        val ev = run(BargeInGate(), List(200) { true to 1500.0 })
        assertTrue(ev.none { it == BargeInGate.Event.START })
    }

    @Test
    fun `whispers below the absolute floor do not count`() {
        val ev = run(BargeInGate(), quiet(50) + List(20) { true to 200.0 })
        assertTrue(ev.none { it == BargeInGate.Event.START })
    }

    @Test
    fun `after the end the gate stays quiet`() {
        val g = BargeInGate()
        run(g, quiet(50) + speech(20) + quiet(50))
        assertFalse(g.speaking)
        assertTrue(run(g, speech(30)).none { it != BargeInGate.Event.NONE })
    }

    @Test
    fun `the emergency brake ends a very long utterance`() {
        val ev = run(BargeInGate(maxFrames = 100), quiet(50) + speech(200))
        assertEquals(1, ev.count { it == BargeInGate.Event.END })
    }
}
