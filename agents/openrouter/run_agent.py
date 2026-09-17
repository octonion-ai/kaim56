#!/usr/bin/env python3
# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 Ulrich Neidel
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""Transport-Layer fuer den OpenRouter-Agent: TRANSPORT=signal | web."""
import json
import uuid
import os
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import agent

TRANSPORT = os.environ.get("TRANSPORT", "signal")
SIGNAL_API = os.environ.get("SIGNAL_API", "https://signalapi.kat56.de").rstrip("/")
SIGNAL_VERIFY = os.environ.get("SIGNAL_VERIFY", "true").strip().lower() not in ("0", "false", "no", "off")
SIGNAL_NUMBER = os.environ.get("SIGNAL_NUMBER", "")
ALLOWED_SENDERS = [s.strip() for s in os.environ.get("ALLOWED_SENDERS", "").split(",") if s.strip()]
POLL_TIMEOUT = int(os.environ.get("POLL_TIMEOUT", "10"))
MAX_REPLY = int(os.environ.get("MAX_REPLY_CHARS", "3500"))
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))
_ctx = None
if not SIGNAL_VERIFY:
    import ssl
    _ctx = ssl.create_default_context()
    _ctx.check_hostname = False
    _ctx.verify_mode = ssl.CERT_NONE


def log(*a):
    print(time.strftime("%F %T"), *a, flush=True)


# --- signal -----------------------------------------------------------------
def _open(req):
    return urllib.request.urlopen(req, timeout=max(POLL_TIMEOUT + 15, 30), context=_ctx)


def sig_send(text, to):
    for i in range(0, len(text) or 1, MAX_REPLY):
        body = json.dumps({"message": text[i:i + MAX_REPLY] or " ", "number": SIGNAL_NUMBER,
                           "recipients": [to]}).encode()
        try:
            _open(urllib.request.Request(f"{SIGNAL_API}/v2/send", data=body,
                  headers={"Content-Type": "application/json"}, method="POST")).read()
        except Exception as e:
            log("send failed", repr(e))


def sig_receive():
    try:
        return json.loads(_open(urllib.request.Request(
            f"{SIGNAL_API}/v1/receive/{SIGNAL_NUMBER}?timeout={POLL_TIMEOUT}")).read().decode())
    except Exception as e:
        log("receive error", repr(e))
        return []


def signal_loop():
    sig_receive()
    sig_send(f"🤖 openrouter-agent online (model {agent.config.OR_MODEL}). /reset für neuen Kontext.",
             ALLOWED_SENDERS[0])
    while True:
        for env in sig_receive() or []:
            e = env.get("envelope", env)
            dm = e.get("dataMessage") or {}
            text = dm.get("message")
            src = e.get("sourceNumber") or e.get("source")
            if not text or src == SIGNAL_NUMBER or src not in ALLOWED_SENDERS:
                continue
            if (dm.get("groupInfo") or {}).get("groupId"):
                continue
            log("MSG from", src, ":", text[:80])
            sig_send("🤔 …", src)
            try:
                reply = agent.run(text)
            except Exception as ex:
                reply = f"⚠️ {ex!r}"
            sig_send(reply, src)
            log("replied", len(reply))
            # Turn in die gemeinsame Chat-Historie (App+Web) spiegeln.
            try:
                agent.mgrclient._mgr(agent.mgrclient._manager_base(), "/api/chat-log",
                           {"sender": src, "user": text, "reply": reply}, timeout=10)
            except Exception:
                pass
        time.sleep(1)


# --- web --------------------------------------------------------------------
PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>agent</title>
<style>*{box-sizing:border-box}body{font-family:system-ui,sans-serif;margin:0;height:100dvh;display:flex;flex-direction:column;background:#fafafa;color:#222}
header{padding:.7rem 1rem;font-weight:600;border-bottom:1px solid #ddd;background:#fff}
#log{flex:1;overflow-y:auto;padding:1rem;display:flex;flex-direction:column;gap:.6rem}
.msg{max-width:85%;padding:.55rem .8rem;border-radius:12px;white-space:pre-wrap;line-height:1.4;word-wrap:break-word}
.me{align-self:flex-end;background:#0a7cff;color:#fff}.bot{align-self:flex-start;background:#eee}
form{display:flex;gap:.5rem;padding:.7rem;border-top:1px solid #ddd;background:#fff}
textarea{flex:1;padding:.6rem;font-size:1rem;border:1px solid #ccc;border-radius:10px;resize:none;min-height:44px;max-height:140px}
button{padding:.6rem 1rem;border:none;border-radius:10px;background:#0a7cff;color:#fff;cursor:pointer}
@media(prefers-color-scheme:dark){body{background:#111;color:#e2e2e2}header,form{background:#1a1a1a;border-color:#333}.bot{background:#242424}textarea{background:#1b1b1b;color:#e2e2e2;border-color:#444}}</style></head><body>
<header>🤖 openrouter-agent</header><div id=log></div>
<form id=f><textarea id=t placeholder="Nachricht… (Enter sendet)" autofocus></textarea><button>➤</button></form>
<script>const log=document.getElementById('log'),t=document.getElementById('t');
function add(x,c){const d=document.createElement('div');d.className='msg '+c;d.textContent=x;log.appendChild(d);log.scrollTop=log.scrollHeight;return d}
async function send(){const m=t.value.trim();if(!m)return;t.value='';add(m,'me');const b=add('…','bot');
try{const r=await fetch('api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:m})});b.textContent=(await r.json()).reply||'(leer)'}catch(e){b.textContent='⚠️ '+e}log.scrollTop=log.scrollHeight}
document.getElementById('f').onsubmit=e=>{e.preventDefault();send()};
t.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}});</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(PAGE.encode())

    def do_POST(self):
        try:
            d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except json.JSONDecodeError:
            d = {}
        message = d.get("message") or ""
        image = d.get("image")   # optionales Base64-JPEG (Vision)
        try:
            deadline = float(d.get("deadline") or 0)   # Zeitbudget des Aufrufers (Manager-Task)
        except (TypeError, ValueError):
            deadline = 0.0
        kind = "task" if d.get("kind") == "task" else None   # Trace-Art: Task-Lauf oder Chat
        turn = uuid.uuid4().hex[:8]     # der Turn bekommt seinen Namen VOR den Headern
        # Steering: Nachricht in einen LAUFENDEN Turn einspeisen. queued=false
        # heisst: gerade kein Turn aktiv -> Aufrufer sendet normal.
        if self.path.rstrip("/").endswith("/steer"):
            ok = agent.steer_push(message) if message else False
            b = json.dumps({"queued": bool(ok)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b)
            return
        # Streaming: /api/chat/stream -> Tokens als roher Text (chunk-weise).
        if self.path.rstrip("/").endswith("/stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Kaim-Turn", turn)      # the client can fetch the trace by it
            self.end_headers()

            def emit(tok):
                try:
                    self.wfile.write(tok.encode("utf-8"))
                    self.wfile.flush()
                except Exception:
                    pass
            try:
                if message or image:
                    agent.run_stream(message, emit, image, deadline=deadline, kind=kind or "stream", turn=turn)
            except Exception as ex:
                emit(f"⚠️ {ex!r}")
            return
        reply = agent.run(message, deadline=deadline, kind=kind or "chat", turn=turn) if message else ""
        b = json.dumps({"reply": reply, "turn": turn}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Kaim-Turn", turn)
        self.end_headers()
        self.wfile.write(b)


def main():
    if not agent.mgrclient.ensure_or_key() and not agent.config.LLAMA_ENDPOINT:   # a local model needs no key
        log("FATAL: OPENROUTER_API_KEY fehlt — weder in der Umgebung noch vom "
            "Secret-Broker des Managers (Allowlist in secret-policy.json?)")
    agent.init()
    if TRANSPORT == "web":
        log(f"web-transport auf :{WEB_PORT}")
        ThreadingHTTPServer(("0.0.0.0", WEB_PORT), H).serve_forever()
    else:
        if not SIGNAL_NUMBER or not ALLOWED_SENDERS:
            log("FATAL: SIGNAL_NUMBER/ALLOWED_SENDERS fehlen")
            return
        log("signal-transport")
        signal_loop()


if __name__ == "__main__":
    main()
