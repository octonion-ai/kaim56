// kAIm56 KatAgent — Android client for the kAIm56 agent platform
// Copyright (C) 2026 Ulrich Neidel
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Barge-in decision: is the user talking over the assistant's spoken reply?
// Fed one 20-ms frame at a time with what the echo canceller left of the
// microphone signal (its VAD flag and the frame's RMS). Pure logic, no
// Android — testable without a device.
package de.kat56.agent

class BargeInGate(
    /** Frames after playback start in which nothing counts: the canceller is
     *  still converging and the residual echo is loud (500 ms). */
    private val warmupFrames: Int = 25,
    /** Frames after that with the strict rule: every frame of the window voiced. */
    private val strictFrames: Int = 40,
    /** Voiced frames within the window that make it speech (120 ms of 160). */
    private val startFrames: Int = 6,
    private val window: Int = 8,
    /** Silence that ends an utterance (900 ms). */
    private val hangFrames: Int = 45,
    /** Emergency brake: an utterance is over after this many frames (30 s). */
    private val maxFrames: Int = 1500,
    /** Absolute floor: residual hiss and whispers never count. */
    private val minRms: Double = 300.0,
) {
    enum class Event { NONE, START, END }

    var speaking = false
        private set
    private var frames = 0
    private var spokenFrames = 0
    private var quiet = 0
    private var ended = false
    private val recent = ArrayDeque<Boolean>()
    /** Running noise floor of the cancelled signal: the quietest recent frame,
     *  drifting up slowly so a change of room does not pin it forever. */
    private var floor = -1.0

    fun frame(vad: Boolean, rms: Double): Event {
        frames++
        if (floor < 0) floor = rms.coerceAtLeast(1.0)
        else if (rms < floor) floor = rms.coerceAtLeast(1.0)
        else floor *= 1.002
        if (ended) return Event.NONE
        val margin = if (frames <= strictFrames) 6.0 else 3.0
        val voiced = vad && rms >= minRms && rms > floor * margin
        if (!speaking) {
            if (frames <= warmupFrames) return Event.NONE
            recent.addLast(voiced)
            while (recent.size > window) recent.removeFirst()
            val need = if (frames <= strictFrames) window else startFrames
            if (recent.size == window && recent.count { it } >= need) {
                speaking = true; spokenFrames = 0; quiet = 0
                return Event.START
            }
            return Event.NONE
        }
        spokenFrames++
        if (voiced) quiet = 0 else quiet++
        if (quiet >= hangFrames || spokenFrames >= maxFrames) {
            speaking = false; ended = true
            return Event.END
        }
        return Event.NONE
    }
}
