// kAIm56 KatAgent — Android client for the kAIm56 agent platform
// Copyright (C) 2026 Ulrich Neidel
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// JNI bridge for barge-in: an acoustic echo canceller (speexdsp mdf) with the
// preprocessor (denoise, residual echo suppression, VAD) on the microphone,
// and a resampler that turns the played TTS audio into the canceller's
// far-end reference. All state lives in one handle; the Kotlin side
// (EchoMic.kt) owns the threads and the decisions.
//
// Frames are 16-bit mono at the capture rate (16 kHz, 20 ms = 320 samples).
#ifndef KATECHO_HOST_TEST
#include <jni.h>
#endif
#include <stdlib.h>
#include <string.h>
#include "speex/speex_echo.h"
#include "speex/speex_preprocess.h"
#include "speex/speex_resampler.h"

typedef struct {
    SpeexEchoState *echo;
    SpeexPreprocessState *pre;
    SpeexResamplerState *rs;      /* far-end: playback rate -> capture rate */
    int frame;                    /* samples per frame at the capture rate */
} Echo;

/* Core (no JNI) — also built by the host self-test. */
static Echo *echo_create(int rate, int frame, int tail, int play_rate) {
    Echo *e = calloc(1, sizeof(Echo));
    if (!e) return NULL;
    e->frame = frame;
    e->echo = speex_echo_state_init(frame, tail);
    e->pre = speex_preprocess_state_init(frame, rate);
    if (!e->echo || !e->pre) { free(e); return NULL; }
    speex_echo_ctl(e->echo, SPEEX_ECHO_SET_SAMPLING_RATE, &rate);
    speex_preprocess_ctl(e->pre, SPEEX_PREPROCESS_SET_ECHO_STATE, e->echo);
    int on = 1, sup = -40, sup_active = -15;
    speex_preprocess_ctl(e->pre, SPEEX_PREPROCESS_SET_DENOISE, &on);
    speex_preprocess_ctl(e->pre, SPEEX_PREPROCESS_SET_VAD, &on);
    speex_preprocess_ctl(e->pre, SPEEX_PREPROCESS_SET_ECHO_SUPPRESS, &sup);
    speex_preprocess_ctl(e->pre, SPEEX_PREPROCESS_SET_ECHO_SUPPRESS_ACTIVE, &sup_active);
    if (play_rate != rate) {
        int err = 0;
        e->rs = speex_resampler_init(1, (spx_uint32_t)play_rate, (spx_uint32_t)rate, 5, &err);
    }
    return e;
}

static void echo_destroy(Echo *e) {
    if (!e) return;
    if (e->echo) speex_echo_state_destroy(e->echo);
    if (e->pre) speex_preprocess_state_destroy(e->pre);
    if (e->rs) speex_resampler_destroy(e->rs);
    free(e);
}

/* mic + far-end -> out (echo removed, denoised); returns the VAD flag (1 = speech). */
static int echo_cancel(Echo *e, const spx_int16_t *mic, const spx_int16_t *far, spx_int16_t *out) {
    speex_echo_cancellation(e->echo, mic, far, out);
    return speex_preprocess_run(e->pre, out);
}

/* Playback samples (play_rate) -> far-end samples (capture rate). Returns the
 * number of output samples written; out must hold ceil(n * rate / play_rate) + 1. */
static int echo_resample(Echo *e, const spx_int16_t *in, int n, spx_int16_t *out, int out_cap) {
    if (!e->rs) {                                  /* same rate: copy through */
        if (n > out_cap) n = out_cap;
        memcpy(out, in, (size_t)n * sizeof(spx_int16_t));
        return n;
    }
    spx_uint32_t in_len = (spx_uint32_t)n, out_len = (spx_uint32_t)out_cap;
    speex_resampler_process_int(e->rs, 0, in, &in_len, out, &out_len);
    return (int)out_len;
}

#ifndef KATECHO_HOST_TEST
#define JF(name) Java_de_kat56_agent_EchoMic_##name

JNIEXPORT jlong JNICALL JF(nativeCreate)(JNIEnv *env, jclass cls, jint rate, jint frame, jint tail, jint playRate) {
    (void)env; (void)cls;
    return (jlong)(intptr_t)echo_create(rate, frame, tail, playRate);
}

JNIEXPORT void JNICALL JF(nativeDestroy)(JNIEnv *env, jclass cls, jlong h) {
    (void)env; (void)cls;
    echo_destroy((Echo *)(intptr_t)h);
}

JNIEXPORT jint JNICALL JF(nativeCancel)(JNIEnv *env, jclass cls, jlong h, jshortArray mic, jshortArray far, jshortArray out) {
    (void)cls;
    Echo *e = (Echo *)(intptr_t)h;
    if (!e) return 0;
    jshort *m = (*env)->GetShortArrayElements(env, mic, NULL);
    jshort *f = (*env)->GetShortArrayElements(env, far, NULL);
    jshort *o = (*env)->GetShortArrayElements(env, out, NULL);
    int vad = 0;
    if (m && f && o) vad = echo_cancel(e, (const spx_int16_t *)m, (const spx_int16_t *)f, (spx_int16_t *)o);
    if (m) (*env)->ReleaseShortArrayElements(env, mic, m, JNI_ABORT);
    if (f) (*env)->ReleaseShortArrayElements(env, far, f, JNI_ABORT);
    if (o) (*env)->ReleaseShortArrayElements(env, out, o, 0);
    return vad;
}

JNIEXPORT jint JNICALL JF(nativeResample)(JNIEnv *env, jclass cls, jlong h, jshortArray in, jint n, jshortArray out) {
    (void)cls;
    Echo *e = (Echo *)(intptr_t)h;
    if (!e) return 0;
    jshort *i = (*env)->GetShortArrayElements(env, in, NULL);
    jshort *o = (*env)->GetShortArrayElements(env, out, NULL);
    int cap = (int)(*env)->GetArrayLength(env, out);
    int wrote = 0;
    if (i && o) wrote = echo_resample(e, (const spx_int16_t *)i, n, (spx_int16_t *)o, cap);
    if (i) (*env)->ReleaseShortArrayElements(env, in, i, JNI_ABORT);
    if (o) (*env)->ReleaseShortArrayElements(env, out, o, 0);
    return wrote;
}
#endif
