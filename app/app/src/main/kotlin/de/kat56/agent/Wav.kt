// kAIm56 KatAgent — Android client for the kAIm56 agent platform
// Copyright (C) 2026 Ulrich Neidel
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Just enough WAV: read what the manager's speech synthesis returns (RIFF,
// PCM 16-bit) and write what the echo-cancelled microphone captured for
// /api/stt. No Android dependency — testable on the JVM.
package de.kat56.agent

import java.nio.ByteBuffer
import java.nio.ByteOrder

object Wav {
    class Pcm(val rate: Int, val channels: Int, val bits: Int, val dataOffset: Int, val dataLength: Int)

    /** Header of a RIFF/WAVE file, or null when it is not PCM 16-bit. */
    fun parse(bytes: ByteArray): Pcm? {
        if (bytes.size < 12 || String(bytes, 0, 4) != "RIFF" || String(bytes, 8, 4) != "WAVE") return null
        val b = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN)
        var pos = 12
        var rate = 0; var channels = 0; var bits = 0; var format = 0
        while (pos + 8 <= bytes.size) {
            val id = String(bytes, pos, 4)
            val size = b.getInt(pos + 4)
            val body = pos + 8
            if (id == "fmt " && body + 16 <= bytes.size) {
                format = b.getShort(body).toInt(); channels = b.getShort(body + 2).toInt()
                rate = b.getInt(body + 4); bits = b.getShort(body + 14).toInt()
            } else if (id == "data") {
                if (format != 1 || bits != 16 || channels < 1 || rate <= 0) return null
                val len = if (size < 0 || body + size > bytes.size) bytes.size - body else size
                return Pcm(rate, channels, bits, body, len)
            }
            pos = body + size + (size and 1)
        }
        return null
    }

    /** Channel 0 as 16-bit samples. */
    fun mono16(bytes: ByteArray, p: Pcm): ShortArray {
        val frames = p.dataLength / (2 * p.channels)
        val out = ShortArray(frames)
        val b = ByteBuffer.wrap(bytes, p.dataOffset, p.dataLength).order(ByteOrder.LITTLE_ENDIAN)
        for (i in 0 until frames) out[i] = b.getShort(p.dataOffset + i * 2 * p.channels)
        return out
    }

    /** A 16-bit mono WAV file from [n] samples of [pcm]. */
    fun build(pcm: ShortArray, n: Int, rate: Int): ByteArray {
        val data = n * 2
        val b = ByteBuffer.allocate(44 + data).order(ByteOrder.LITTLE_ENDIAN)
        b.put("RIFF".toByteArray()).putInt(36 + data).put("WAVE".toByteArray())
        b.put("fmt ".toByteArray()).putInt(16).putShort(1).putShort(1).putInt(rate)
            .putInt(rate * 2).putShort(2).putShort(16)
        b.put("data".toByteArray()).putInt(data)
        for (i in 0 until n) b.putShort(pcm[i])
        return b.array()
    }

    /** Root mean square of the first [n] samples. */
    fun rms(pcm: ShortArray, n: Int): Double {
        if (n <= 0) return 0.0
        var acc = 0.0
        for (i in 0 until n) { val v = pcm[i].toDouble(); acc += v * v }
        return Math.sqrt(acc / n)
    }
}
