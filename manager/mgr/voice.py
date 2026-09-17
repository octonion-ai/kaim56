# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""Voice: what of a reply is worth speaking (speakable_text) and the recent speech-to-text results kept for the UI.

Part of the mgr package: no import from manager.py. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import collections
import re
import threading
import time


_SPK_THINK = re.compile(r"⟦think⟧.*?(?:⟦/think⟧|$)", re.S)
_SPK_TOOL = re.compile(r"^[ \t]*🔧.*$", re.M)
_SPK_FENCE = re.compile(r"```.*?(?:```|$)", re.S)
_SPK_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_SPK_URL = re.compile(r"https?://\S+")
_SPK_DECOR = re.compile(r"[*_`#>|]")
_SPK_SPACE = re.compile(r"[ \t]+")
_SPK_NL = re.compile(r"\n{2,}")


def speakable_text(text):
    """Reply text -> read-aloud text (same rules as the desktop client)."""
    t = str(text or "")
    t = _SPK_THINK.sub("", t)
    t = _SPK_TOOL.sub("", t)
    t = _SPK_FENCE.sub(" Codeblock übersprungen. ", t)
    t = _SPK_LINK.sub(r"\1", t)
    t = _SPK_URL.sub("", t)
    t = _SPK_DECOR.sub("", t)
    t = _SPK_SPACE.sub(" ", t)
    t = _SPK_NL.sub("\n", t)
    return t.strip()


# Last transcripts from /api/stt, in memory only (no file: spoken words are
# not something to persist by accident). Answers "what did STT hear?" from
# the web UI/API instead of guessing from the model's reply.
STT_RECENT_MAX = 50
_stt_recent = collections.deque(maxlen=STT_RECENT_MAX)
_stt_lock = threading.Lock()


STT_AUDIO_MAX = 5
_stt_audio = collections.deque(maxlen=STT_AUDIO_MAX)   # (ts, src, content-type, bytes)


def stt_remember(text, seconds, src, audio=None, ctype=""):
    with _stt_lock:
        _stt_recent.append({"ts": int(time.time()), "text": str(text or "")[:500],
                            "seconds": seconds, "src": src})
        if audio:
            _stt_audio.append((int(time.time()), src, ctype, bytes(audio[:4 * 1024 * 1024])))


def stt_audio(i=0):
    """The i-th most recent STT upload as (ts, src, content-type, bytes) — to
    LOOK at what a client sends (rate, level, header) when STT hears nothing."""
    with _stt_lock:
        items = list(_stt_audio)[::-1]
        return items[i] if 0 <= i < len(items) else None


def stt_recent():
    with _stt_lock:
        return list(_stt_recent)[::-1]        # newest first
