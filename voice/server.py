#!/usr/bin/env python3
"""Voice service for kAIm56: recognition (Parakeet) and speech output (Piper).

Runs on the host, not in the microVMs: together the models need a good 700 MB
and would otherwise sit in memory per instance. Bound to 127.0.0.1 — the
service is reachable only through the manager, just like the katfs node. The
manager knows from the source IP who is calling; there is no separate access
control here, because it should never be directly reachable.

  POST /stt   Audio (beliebiges Format)      -> {"text": …, "seconds": …}
  POST /tts   {"text": …}                    -> audio/wav
  GET  /health                               -> {"ready": bool, …}
"""
import json
import os
import subprocess
import tempfile
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import onnx_asr

PORT = int(os.environ.get("PORT", "8770"))
# Bind to 0.0.0.0 in the container: Docker's port forwarding does NOT reach the
# container loopback. The restriction sits on the host side of the mapping
# (-p 127.0.0.1:8770:8770) — so from outside the service is just as unreachable
# as the katfs node, only one level up.
HOST = os.environ.get("HOST", "0.0.0.0")
PIPER = os.environ.get("PIPER_BIN", "/opt/piper/piper")
VOICE = os.environ.get("PIPER_VOICE", "/opt/piper/de-thorsten-high.onnx")
ASR_NAME = os.environ.get("ASR_MODEL", "nemo-parakeet-tdt-0.6b-v3")
MAX_BODY = 32 * 1024 * 1024      # 32 MB is enough for several minutes of speech
MAX_TEXT = 4000                  # longer replies are shortened before speaking

_asr = None
_asr_lock = threading.Lock()


def asr():
    """Load the model on first call (~2 s from cache) and keep it."""
    global _asr
    with _asr_lock:
        if _asr is None:
            t0 = time.time()
            _asr = onnx_asr.load_model(ASR_NAME, quantization="int8")
            print(f"[voice] ASR loaded in {time.time()-t0:.1f}s", flush=True)
    return _asr


def to_wav16k(raw):
    """Arbitrary input (Opus/OGG from Signal, AAC/M4A from Android, WAV) ->
    16 kHz mono PCM. Without this step recognition fails on anything that is
    not WAV."""
    src = tempfile.NamedTemporaryFile(suffix=".in", delete=False)
    src.write(raw)
    src.close()
    dst = src.name + ".wav"
    try:
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-i", src.name, "-ac", "1", "-ar", "16000", "-f", "wav", dst],
                       check=True, capture_output=True)
    finally:
        os.unlink(src.name)
    return dst


def list_voices():
    """Available Piper voices: all *.onnx in the Piper directory."""
    d = os.path.dirname(VOICE)
    try:
        return sorted(f[:-5] for f in os.listdir(d) if f.endswith(".onnx"))
    except OSError:
        return [os.path.basename(VOICE)[:-5]]


def _voice_path(name):
    """Resolve a voice name path-safely; unknown/empty -> default."""
    if not name:
        return VOICE
    base = os.path.basename(str(name))
    if not base.endswith(".onnx"):
        base += ".onnx"
    p = os.path.join(os.path.dirname(VOICE), base)
    return p if os.path.exists(p) else VOICE


def speak(text, voice="", speed=1.0):
    """Piper runs as a process per request — at a 0.07 real-time factor the
    startup costs more than the synthesis, but it keeps the service stateless.
    speed >1 = faster (Piper: length_scale = 1/speed), clamped 0.5–2.0."""
    try:
        speed = min(2.0, max(0.5, float(speed or 1.0)))
    except (TypeError, ValueError):
        speed = 1.0
    out = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    out.close()
    cmd = [PIPER, "--model", _voice_path(voice), "--output_file", out.name]
    if abs(speed - 1.0) > 0.01:
        cmd += ["--length_scale", f"{1.0 / speed:.3f}"]
    p = subprocess.run(cmd, input=text.encode(), capture_output=True)
    if p.returncode != 0:
        os.unlink(out.name)
        raise RuntimeError(p.stderr.decode()[:300])
    return out.name


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            return self._json(200, {"ready": _asr is not None,
                                    "voice": os.path.basename(VOICE)[:-5],
                                    "voices": list_voices(), "asr": ASR_NAME})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        ln = int(self.headers.get("Content-Length", 0) or 0)
        if ln > MAX_BODY:
            return self._json(413, {"error": "body too large"})
        raw = self.rfile.read(ln) if ln else b""

        if self.path.startswith("/stt"):
            if not raw:
                return self._json(400, {"error": "no audio"})
            wav = None
            try:
                t0 = time.time()
                wav = to_wav16k(raw)
                with wave.open(wav) as w:
                    secs = w.getnframes() / w.getframerate()
                text = asr().recognize(wav)
                return self._json(200, {"text": text, "seconds": round(secs, 2),
                                        "took": round(time.time() - t0, 2)})
            except subprocess.CalledProcessError as e:
                return self._json(415, {"error": "audio not decodable",
                                        "detail": e.stderr.decode()[:200]})
            except Exception as e:
                return self._json(500, {"error": repr(e)[:300]})
            finally:
                if wav and os.path.exists(wav):
                    os.unlink(wav)

        if self.path.startswith("/tts"):
            try:
                b = json.loads(raw or b"{}")
                text = (b.get("text") or "").strip()[:MAX_TEXT]
            except json.JSONDecodeError:
                return self._json(400, {"error": "bad json"})
            if not text:
                return self._json(400, {"error": "no text"})
            out = None
            try:
                t0 = time.time()
                out = speak(text, b.get("voice", ""), b.get("speed", 1.0))
                data = open(out, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("X-Took", f"{time.time()-t0:.2f}")
                self.end_headers()
                self.wfile.write(data)
                return
            except Exception as e:
                return self._json(500, {"error": repr(e)[:300]})
            finally:
                if out and os.path.exists(out):
                    os.unlink(out)

        self._json(404, {"error": "not found"})


if __name__ == "__main__":
    threading.Thread(target=asr, daemon=True).start()   # im Hintergrund vorladen
    print(f"[voice] hoert auf {HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
