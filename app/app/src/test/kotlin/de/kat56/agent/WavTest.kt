// kAIm56 KatAgent — Android client for the kAIm56 agent platform
// Copyright (C) 2026 Ulrich Neidel
// SPDX-License-Identifier: AGPL-3.0-or-later
package de.kat56.agent

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class WavTest {

    @Test
    fun `build and parse round-trip`() {
        val pcm = ShortArray(500) { (it * 37 % 2000 - 1000).toShort() }
        val wav = Wav.build(pcm, pcm.size, 16000)
        val p = Wav.parse(wav)!!
        assertEquals(16000, p.rate); assertEquals(1, p.channels); assertEquals(16, p.bits)
        assertEquals(1000, p.dataLength)
        assertArrayEquals(pcm, Wav.mono16(wav, p))
    }

    @Test
    fun `stereo takes the left channel, other formats are refused`() {
        val stereo = Wav.build(ShortArray(4) { 1 }, 4, 22050).clone()
        // patch the header: 2 channels, data holds 2 frames of L/R
        stereo[22] = 2
        val p = Wav.parse(stereo)!!
        assertEquals(2, p.channels)
        assertEquals(2, Wav.mono16(stereo, p).size)
        assertNull(Wav.parse("not a wav".toByteArray()))
        val eightBit = Wav.build(ShortArray(4), 4, 8000).clone(); eightBit[34] = 8
        assertNull(Wav.parse(eightBit))
    }

    @Test
    fun `rms of silence is zero and of a square wave its amplitude`() {
        assertEquals(0.0, Wav.rms(ShortArray(320), 320), 0.0)
        assertEquals(1000.0, Wav.rms(ShortArray(320) { if (it % 2 == 0) 1000 else -1000 }, 320), 0.01)
    }
}
