// kAIm56 KatAgent — Android client for the kAIm56 agent platform
// Copyright (C) 2026 Ulrich Neidel
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Plays the manager's speech synthesis (WAV) through an AudioTrack in 20-ms
// chunks. Unlike MediaPlayer this hands every chunk to a listener BEFORE it
// is written — that copy is the far-end reference the echo canceller needs
// to take the assistant's own voice out of the microphone (barge-in).
package de.kat56.agent

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import android.os.Handler
import android.os.Looper

class TtsPlayer(private val onDone: () -> Unit) {
    @Volatile private var stopped = false
    private var track: AudioTrack? = null
    private val main = Handler(Looper.getMainLooper())
    var rate = 0
        private set

    /** Starts playback on its own thread; false when the WAV is not PCM 16-bit. */
    fun play(wav: ByteArray, farEnd: ((ShortArray, Int) -> Unit)? = null): Boolean {
        val p = Wav.parse(wav) ?: return false
        val pcm = Wav.mono16(wav, p)
        rate = p.rate
        val chunk = p.rate / 50                                       // 20 ms
        val minBuf = AudioTrack.getMinBufferSize(p.rate, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
        // A small buffer keeps the far-end reference close to what is really
        // playing; the canceller's tail (512 ms) covers the rest of the delay.
        val bufBytes = maxOf(minBuf, chunk * 2 * 3)
        val t = AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
            .setAudioFormat(AudioFormat.Builder()
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setSampleRate(p.rate)
                .setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
            .setBufferSizeInBytes(bufBytes)
            .setTransferMode(AudioTrack.MODE_STREAM)
            .build()
        if (t.state != AudioTrack.STATE_INITIALIZED) { runCatching { t.release() }; return false }
        track = t
        Thread({
            runCatching {
                t.play()
                var off = 0
                val buf = ShortArray(chunk)
                while (!stopped && off < pcm.size) {
                    val n = minOf(chunk, pcm.size - off)
                    System.arraycopy(pcm, off, buf, 0, n)
                    if (n < chunk) java.util.Arrays.fill(buf, n, chunk, 0)
                    farEnd?.invoke(buf, chunk)
                    var written = 0
                    while (!stopped && written < chunk) {
                        val w = t.write(buf, written, chunk - written)
                        if (w < 0) { stopped = true; break }
                        written += w
                    }
                    off += n
                }
                // Drain: the last buffer is still playing after the last write.
                val total = pcm.size
                while (!stopped && runCatching { t.playbackHeadPosition }.getOrDefault(total) < total) Thread.sleep(20)
            }
            runCatching { t.stop() }; runCatching { t.release() }
            if (track === t) track = null
            if (!stopped) main.post { onDone() }
        }, "tts-play").start()
        return true
    }

    fun stop() {
        stopped = true
        val t = track ?: return
        runCatching { t.pause() }; runCatching { t.flush() }
    }
}
