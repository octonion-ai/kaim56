#!/usr/bin/env python3
"""Web-Transport-Bridge: kleine Chat-UI + /api/chat, agent-aware (claude|fabric).

Laeuft in der microVM auf 0.0.0.0:WEB_PORT. Wird NUR ueber den Manager-Proxy
(agents.kat56.de/i/<name>/) erreicht, daher keine eigene Auth. Stdlib only.
"""
import json
import os
import subprocess
import tempfile
import time
import uuid
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AGENT = os.environ.get("AGENT", "claude")
WORKDIR = os.environ.get("CLAUDE_WORKDIR", "/root/workspace")
PORT = int(os.environ.get("WEB_PORT", "8080"))
TIMEOUT = int(os.environ.get("WEB_TIMEOUT", "600"))
ALLOW_ACTIONS = os.environ.get("ALLOW_ACTIONS", "true").strip().lower() not in (
    "0", "false", "no", "off")
FABRIC_DEFAULT_PATTERN = os.environ.get("FABRIC_DEFAULT_PATTERN", "ai")
_session = None


_model = None      # per /model gesetzt; None = Vorgabe der Installation
_last_turn = [""]  # id of the most recent claude turn, for the X-Kaim-Turn header


def _log(*m):
    print(*m, flush=True)


def _post(path, payload):
    """Fire-and-forget to the manager (usage / trace / audit). The manager
    identifies this instance by source IP; a failure must never disturb a turn."""
    try:
        req = urllib.request.Request(manager_base() + path, method="POST",
                                     data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass

# Die Anmeldung ist eine Kopie des Host-Credentials (guest-init holt sie beim
# Boot). Claude Code auf dem Host erneuert seine Tokens laufend und der
# Refresh-Token rotiert dabei — die Kopie im Gast laeuft nach Stunden ab und
# kann sich nicht mehr selbst erneuern ("OAuth session expired and could not be
# refreshed"). Darum vor jedem Turn nachziehen, wenn der Host ein neueres hat,
# und bei genau dieser Fehlermeldung einmal erzwungen nachziehen + wiederholen.
CRED_PATH = os.environ.get("CLAUDE_CRED_PATH", "/home/node/.claude/.credentials.json")
AUTH_FAIL_MARKS = ("Failed to authenticate", "OAuth session expired", "Not logged in")


def manager_base():
    """Der Manager ist das Host-Gateway (.1 des /30) auf :8700."""
    ip = ""
    try:
        ip = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5).stdout.split()[0]
    except Exception:
        pass
    if not ip:
        return "http://172.30.1.1:8700"
    return f"http://{ip.rsplit('.', 1)[0]}.1:8700"


def _sync_credentials(force=False):
    """Host-Credential holen; schreiben, wenn es juenger ist als die Kopie (oder
    force). True = geschrieben. Faellt still zurueck: ohne Manager bleibt die Kopie."""
    try:
        with urllib.request.urlopen(f"{manager_base()}/api/claude-credentials", timeout=5) as r:
            host = json.loads(r.read().decode()).get("claudeAiOauth") or {}
    except Exception:
        return False
    if not host.get("accessToken"):
        return False
    try:
        with open(CRED_PATH) as fh:
            cur = json.load(fh)
    except (OSError, ValueError):
        cur = {}
    mine = (cur.get("claudeAiOauth") or {}).get("expiresAt") or 0
    if not force and mine >= (host.get("expiresAt") or 0):
        return False
    cur["claudeAiOauth"] = host
    d = os.path.dirname(CRED_PATH)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".cred.", dir=d)
    with os.fdopen(fd, "w") as fh:
        json.dump(cur, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CRED_PATH)
    return True


AUTO_RESET_MIN = int(os.environ.get("AUTO_RESET_MIN", "0") or 0)   # idle minutes, then a new conversation (0 = never)
_last_turn = [0.0]


def _auto_reset():
    import time
    global _session
    now = time.time()
    last, _last_turn[0] = _last_turn[0], now
    if AUTO_RESET_MIN > 0 and last and _session and now - last >= AUTO_RESET_MIN * 60:
        _session = None
        return True
    return False


def run_claude(msg):
    global _session, _model
    _auto_reset()
    m = msg.strip()
    low = m.lower()
    # Plattform-Slash-Befehle: Claude Code hat EIGENE Slash-Befehle (/branch =
    # Git-Worktrees!), die hier nur verwirren wuerden. Was die Bruecke kann,
    # macht sie selbst; was es auf Claude-Instanzen nicht gibt, sagt sie ehrlich
    # — statt Claude Codes "isn't available in this environment" durchzureichen.
    if low == "/reset":
        _session = None
        return "🔄 Neue Unterhaltung."
    if low.startswith("/model"):
        rest = m[6:].strip()
        if not rest:
            return f"🧠 Modell: {_model or 'Vorgabe der Installation'} · /model sonnet|opus|haiku|<id> · /model default"
        _model = None if rest.lower() in ("default", "reset", "aus", "off") else rest
        return f"🧠 Modell ab jetzt: {_model or 'Vorgabe der Installation'}"
    if low.startswith("/fresh"):
        rest = m[6:].strip()
        if not rest:
            return "Usage: /fresh <Auftrag> — Einmal-Anfrage im Wegwerf-Kontext."
        return _claude_once(rest, resume=None, keep_session=False)
    for known in ("/aside", "/branch", "/back", "/goal", "/steps", "/reasoning"):
        if low == known or low.startswith(known + " "):
            return (f"ℹ️ {known} gibt es nur auf den OpenRouter-Agenten, nicht auf "
                    "Claude-Code-Instanzen. Hier verfuegbar: /reset, /fresh, /model — "
                    "alles andere geht als Claude-Code-Befehl an Claude selbst.")
    return _claude_once(m, resume=_session, keep_session=True)


def _claude_once(msg, resume, keep_session):
    _sync_credentials()
    out = _claude_run(msg, resume, keep_session)
    if any(k in out for k in AUTH_FAIL_MARKS) and _sync_credentials(force=True):
        out = _claude_run(msg, resume, keep_session)      # einmal mit frischer Anmeldung
    return out


def _claude_run(msg, resume, keep_session):
    global _session
    cmd = ["claude", "-p", msg, "--output-format", "json",
           # Plattform-Tools (Memory, Suche, Skills, Notify) als echter
           # MCP-Server statt curl-Rezepten — Claude sieht sie im Tool-Katalog.
           "--mcp-config", "/app/kaim56-mcp.json"]
    if _model:
        cmd += ["--model", _model]
    if resume:
        cmd += ["--resume", resume]
    if ALLOW_ACTIONS:
        cmd += ["--dangerously-skip-permissions"]
    turn = uuid.uuid4().hex[:8]; _last_turn[0] = turn
    kind = "chat" if keep_session else "fresh"
    model = _model or "claude-code"
    _post("/api/trace", {"turn": turn, "event": "start", "kind": kind})
    t0 = time.monotonic()
    p = subprocess.run(cmd, cwd=WORKDIR, capture_output=True, text=True, timeout=TIMEOUT)
    ms = int((time.monotonic() - t0) * 1000)
    d = {}
    try:
        d = json.loads(p.stdout)
    except json.JSONDecodeError:
        d = {}
    if keep_session and d.get("session_id"):
        _session = d["session_id"]
    result = (d.get("result") if d else None) or (p.stdout or p.stderr or "(keine Ausgabe)")[:4000]
    u = d.get("usage") or {}
    itok, otok = int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
    cost = float(d.get("total_cost_usd") or 0.0)
    steps = int(d.get("num_turns") or 1)
    ok = bool(d) and not d.get("is_error")
    # Usage -> Resources tab (subscription runs have cost 0; tokens still show).
    _post("/api/usage", {"model": model, "prompt_tokens": itok, "completion_tokens": otok,
                         "cost": cost, "turn": turn, "step": 1, "ms": ms, "ok": ok,
                         "err": "" if ok else str(result)[:200], "direct": True})
    _post("/api/trace", {"turn": turn, "event": "end", "kind": kind, "steps": steps,
                         "ms": ms, "outcome": "ok" if ok else "error"})
    _log(f"turn {turn} {kind} {model} {ms}ms tokens {itok}/{otok} cost ${cost:.4f} "
         f"steps {steps} -> {len(str(result))} chars" + ("" if ok else " ERROR"))
    return result


def run_fabric(msg):
    pattern, body = FABRIC_DEFAULT_PATTERN, msg
    if msg.startswith("/p "):
        parts = msg[3:].split(None, 1)
        pattern = parts[0]
        body = parts[1] if len(parts) > 1 else ""
    p = subprocess.run(["fabric", "-p", pattern], input=body, capture_output=True,
                       text=True, timeout=TIMEOUT)
    return (p.stdout or p.stderr or "(leere Antwort)").strip()


def run(msg):
    try:
        return run_fabric(msg) if AGENT == "fabric" else run_claude(msg)
    except subprocess.TimeoutExpired:
        return f"⏱️ Zeitlimit ({TIMEOUT}s) erreicht."
    except Exception as e:
        return f"⚠️ Fehler: {e!r}"


PAGE = """<!doctype html><html lang=de><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>__AGENT__ chat</title>
<style>
*{box-sizing:border-box}
body{font-family:system-ui,sans-serif;margin:0;height:100dvh;display:flex;flex-direction:column;background:#fafafa;color:#222}
header{padding:.7rem 1rem;font-weight:600;border-bottom:1px solid #ddd;background:#fff}
#log{flex:1;overflow-y:auto;padding:1rem;display:flex;flex-direction:column;gap:.6rem}
.msg{max-width:85%;padding:.55rem .8rem;border-radius:12px;white-space:pre-wrap;line-height:1.4;word-wrap:break-word}
.me{align-self:flex-end;background:#0a7cff;color:#fff}
.bot{align-self:flex-start;background:#eee}
form{display:flex;gap:.5rem;padding:.7rem;border-top:1px solid #ddd;background:#fff}
textarea{flex:1;padding:.6rem;font-size:1rem;border:1px solid #ccc;border-radius:10px;resize:none;min-height:44px;max-height:140px}
button{padding:.6rem 1rem;font-size:1rem;border:none;border-radius:10px;background:#0a7cff;color:#fff;cursor:pointer}
@media(prefers-color-scheme:dark){body{background:#111;color:#e2e2e2}header,form{background:#1a1a1a;border-color:#333}
.bot{background:#242424}textarea{background:#1b1b1b;color:#e2e2e2;border-color:#444}}
</style></head><body>
<header>🤖 __AGENT__</header>
<div id=log></div>
<form id=f><textarea id=t placeholder="Nachricht… (Enter sendet)" autofocus></textarea><button>➤</button></form>
<script>
const log=document.getElementById('log'),t=document.getElementById('t');
function add(txt,cls){const d=document.createElement('div');d.className='msg '+cls;d.textContent=txt;log.appendChild(d);log.scrollTop=log.scrollHeight;return d}
async function send(){const m=t.value.trim();if(!m)return;t.value='';add(m,'me');const b=add('…','bot');
  try{const r=await fetch('api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:m})});
    const j=await r.json();b.textContent=j.reply||'(leer)';}catch(e){b.textContent='⚠️ '+e}log.scrollTop=log.scrollHeight}
document.getElementById('f').onsubmit=e=>{e.preventDefault();send()};
t.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}});
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = PAGE.replace("__AGENT__", AGENT).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            ln = max(0, int(self.headers.get("Content-Length", 0)))
        except (TypeError, ValueError):
            ln = 0
        try:
            d = json.loads(self.rfile.read(ln) or b"{}")
        except json.JSONDecodeError:
            d = {}
        reply = run(d.get("message", "")) if d.get("message") else ""
        b = json.dumps({"reply": reply}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if _last_turn[0]:
            self.send_header("X-Kaim-Turn", _last_turn[0])   # the app fetches the trace by it
        self.end_headers()
        self.wfile.write(b)


if __name__ == "__main__":
    print(f"web_bridge agent={AGENT} workdir={WORKDIR} on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
