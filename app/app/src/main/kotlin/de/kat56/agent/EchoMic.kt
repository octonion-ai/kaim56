// kAIm56 KatAgent — Android client for the kAIm56 agent platform
// Copyright (C) 2026 Ulrich Neidel
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The microphone during a spoken reply. It listens while the assistant talks,
// takes the assistant's own voice out of what it hears (speexdsp echo
// canceller, native — libkatecho), and decides with BargeInGate whether the
// user started talking. Then it cuts the playback (onBargeIn), keeps the
// utterance — with 300 ms of pre-roll so the first syllable survives — and
// hands it over as a WAV for /api/stt (onUtterance).
//
// Threads: the capture loop runs on its own thread; the player feeds the
// far-end reference from its thread (feedFarEnd); callbacks arrive on the
// main looper.
package de.kat56.agent

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Handler
import android.os.Looper
import android.os.SystemClock

class EchoMic(
    private val playRate: Int,
    private val onBargeIn: () -> Unit,
    private val onUtterance: (ByteArray) -> Unit,
    private val onIdle: () -> Unit,
) {
    companion object {
        const val RATE = 16000
        const val FRAME = 320          // 20 ms
        const val TAIL = 8192          // 512 ms of echo path
        private const val PREROLL = 15 // frames kept before the decision (300 ms)
        private const val GRACE_MS = 800L
        @Volatile private var libOk: Boolean? = null

        fun available(): Boolean {
            libOk?.let { return it }
            val ok = runCatching { System.loadLibrary("katecho") }.isSuccess
            libOk = ok
            return ok
        }

        @JvmStatic private external fun nativeCreate(rate: Int, frame: Int, tail: Int, playRate: Int): Long
        @JvmStatic private external fun nativeDestroy(handle: Long)
        @JvmStatic private external fun nativeCancel(handle: Long, mic: ShortArray, far: ShortArray, out: ShortArray): Int
        @JvmStatic private external fun nativeResample(handle: Long, input: ShortArray, n: Int, out: ShortArray): Int
    }

    private val main = Handler(Looper.getMainLooper())
    @Volatile private var running = false
    @Volatile private var playbackEndedAt = 0L
    private var handle = 0L
    private var record: AudioRecord? = null
    private val lock = Any()
    // Far-end FIFO at the capture rate: what the speaker is (about to be) playing.
    private val far = ShortArray(RATE * 4)
    private var farHead = 0
    private var farCount = 0
    private val resampled = ShortArray(RATE / 10)

    /** True when listening started. */
    @SuppressLint("MissingPermission")   // the caller checks RECORD_AUDIO
    fun start(): Boolean {
        if (!available()) return false
        handle = nativeCreate(RATE, FRAME, TAIL, playRate)
        if (handle == 0L) return false
        val minBuf = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val r = runCatching {
            // VOICE_RECOGNITION: the raw signal, no platform processing on top
            // of our own canceller.
            AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, RATE, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT, maxOf(minBuf, FRAME * 2 * 8))
        }.getOrNull()
        if (r == null || r.state != AudioRecord.STATE_INITIALIZED) {
            runCatching { r?.release() }; nativeDestroy(handle); handle = 0L; return false
        }
        record = r
        running = true
        Thread(::loop, "echo-mic").start()
        return true
    }

    /** Playback samples at [playRate]; called by the player before each write. */
    fun feedFarEnd(pcm: ShortArray, n: Int) {
        if (!running) return
        synchronized(lock) {
            if (handle == 0L) return
            val got = nativeResample(handle, pcm, n, resampled)
            for (i in 0 until got) {
                if (farCount == far.size) break               // player far ahead: drop, the tail covers it
                far[(farHead + farCount) % far.size] = resampled[i]; farCount++
            }
        }
    }

    /** The player is done: listen a little longer, then go idle. */
    fun playbackEnded() { playbackEndedAt = SystemClock.elapsedRealtime() }

    fun stop() {
        running = false
    }

    private fun takeFar(out: ShortArray) {
        synchronized(lock) {
            for (i in 0 until FRAME) {
                if (farCount > 0) { out[i] = far[farHead]; farHead = (farHead + 1) % far.size; farCount-- }
                else out[i] = 0
            }
        }
    }

    private fun loop() {
        val r = record ?: return
        val mic = ShortArray(FRAME); val farFrame = ShortArray(FRAME); val out = ShortArray(FRAME)
        val gate = BargeInGate()
        val preroll = ArrayDeque<ShortArray>()
        val utterance = ArrayList<ShortArray>()
        var result: ByteArray? = null
        runCatching {
            r.startRecording()
            while (running) {
                var got = 0
                while (running && got < FRAME) {
                    val n = r.read(mic, got, FRAME - got)
                    if (n <= 0) { running = false; break }
                    got += n
                }
                if (!running) break
                takeFar(farFrame)
                val vad = synchronized(lock) { if (handle == 0L) 0 else nativeCancel(handle, mic, farFrame, out) }
                when (gate.frame(vad == 1, Wav.rms(out, FRAME))) {
                    BargeInGate.Event.START -> {
                        synchronized(lock) { farHead = 0; farCount = 0 }   // the speaker goes quiet now
                        utterance.addAll(preroll); preroll.clear()
                        utterance.add(out.copyOf())
                        main.post(onBargeIn)
                    }
                    BargeInGate.Event.END -> {
                        utterance.add(out.copyOf())
                        val n = utterance.size * FRAME
                        val pcm = ShortArray(n)
                        for ((i, f) in utterance.withIndex()) System.arraycopy(f, 0, pcm, i * FRAME, FRAME)
                        result = Wav.build(pcm, n, RATE)
                        running = false
                    }
                    BargeInGate.Event.NONE -> {
                        if (gate.speaking) utterance.add(out.copyOf())
                        else {
                            preroll.addLast(out.copyOf())
                            while (preroll.size > PREROLL) preroll.removeFirst()
                            val ended = playbackEndedAt
                            if (ended != 0L && SystemClock.elapsedRealtime() - ended > GRACE_MS) running = false
                        }
                    }
                }
            }
        }
        runCatching { r.stop() }; runCatching { r.release() }
        synchronized(lock) { if (handle != 0L) { nativeDestroy(handle); handle = 0L } }
        val wav = result
        main.post { if (wav != null) onUtterance(wav) else onIdle() }
    }
}
