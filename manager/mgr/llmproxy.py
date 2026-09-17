# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""LLM key proxy (security boundary): the agents call /api/llm/<backend> and the manager adds the key, so no LLM key ever enters a VM; plus the per-instance budget and rate guard and the usage booking of proxied calls.

Part of the mgr package: no import from manager.py. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import json
import os
import threading
import time
import urllib.request

from mgr import notify as _notify
from mgr import store as _store
from mgr import guests as _guests
from mgr import hindsight as _hindsight
from mgr import routes as _routes
from mgr import settings as _settings


# Credential injection gateway (OneCLI pattern): the agent sends its chat
# requests to /api/llm/<backend>/chat/completions instead of directly to the
# router; when forwarding, the manager appends the Authorization header from
# the settings. This way the LLM keys NEVER leave the host: a compromised VM
# can at most call models through the manager (visible, throttleable), but
# cannot exfiltrate a key and reuse it outside the system.
LLM_PROXY_UPSTREAMS = {
    "openrouter": ("https://openrouter.ai/api/v1/chat/completions", "OPENROUTER_API_KEY"),
    "orcarouter": ("https://api.orcarouter.ai/v1/chat/completions", "ORCAROUTER_API_KEY"),
}

# ---- Guardrails: budget + rate limit for LLM calls --------------------------
# Enforcement at the key injection proxy: all of the VMs' router calls pass
# through there. Budget per instance and day (tokens, from llm_usage) and a
# frequency cap per minute. Override per instance via config: BUDGET_TOKENS
# (0 = off), LLM_RATE_MIN. On exceedance: 429 + at most one notify per hour.
GUARD_BUDGET_TOKENS = int(os.environ.get("GUARD_BUDGET_TOKENS", "5000000"))
GUARD_LLM_RATE_MIN = int(os.environ.get("GUARD_LLM_RATE_MIN", "60"))
_guard_lock = threading.Lock()
_guard_calls = {}          # instance -> [timestamps]
_guard_notified = {}       # instance -> ts of the last budget notify


def _guard_check(inst):
    """(allowed, reason). inst = instance dict or None (admin/host: always ok)."""
    if inst is None:
        return True, ""
    name = inst["name"]
    cfg = inst.get("config") or {}
    now = time.time()
    # 1) Frequency per minute
    try:
        rate = int(cfg.get("LLM_RATE_MIN", GUARD_LLM_RATE_MIN))
    except ValueError:
        rate = GUARD_LLM_RATE_MIN
    with _guard_lock:
        lst = _guard_calls.setdefault(name, [])
        lst[:] = [t for t in lst if now - t < 60]
        if rate > 0 and len(lst) >= rate:
            return False, f"rate limit: {rate} LLM calls/min reached"
        lst.append(now)
    # 2) Daily budget (tokens since local midnight)
    try:
        budget = int(cfg.get("BUDGET_TOKENS", GUARD_BUDGET_TOKENS))
    except ValueError:
        budget = GUARD_BUDGET_TOKENS
    if budget > 0:
        midnight = int(time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)))
        u = _store.usage_for(name, midnight)
        used = (u.get("in") or 0) + (u.get("out") or 0)
        if used >= budget:
            with _guard_lock:
                last = _guard_notified.get(name, 0)
                fire = now - last > 3600
                if fire:
                    _guard_notified[name] = now
            if fire:
                try:
                    _notify.notify_add("guardrail", f"Budget reached: {name}",
                               f"{used:,} tokens today (limit {budget:,}). LLM calls "
                               f"pause until midnight. Override: BUDGET_TOKENS in the "
                               f"instance config.", link="tasks")
                except Exception:
                    pass
            return False, f"budget: {used:,}/{budget:,} tokens used today"
    return True, ""



def _proxy_usage(inst, backend, raw, ms=None, turn="", step=None):
    """Book the tokens the UPSTREAM reports for this guest: the budget guard
    must not rest on what the agent chooses to tell us via /api/usage. The
    span (turn, step from the request headers, duration) rides along."""
    if inst is None:
        return
    raw = raw.strip()
    if raw.startswith(b"data:"):
        raw = raw[5:]
    try:
        j = json.loads(raw)
        u = j.get("usage") or {}
        if isinstance(u, dict) and (u.get("prompt_tokens") or u.get("completion_tokens")):
            _store.usage_add(inst["name"], j.get("model") or backend, u.get("prompt_tokens"),
                      u.get("completion_tokens"), u.get("cost"), turn=turn, ms=ms, step=step)
    except (ValueError, AttributeError, TypeError):
        pass


class LLMProxyMixin:
    """The handler side of the key proxy: POST /api/llm/<backend>/chat/completions
    from a VM, upstream with the host-held key, usage booked per instance.
    Mixed into mgr.httpd.H."""

    def _llm_proxy(self, _pp):
        """POST /api/llm/<backend>/chat/completions — credential injection
        gateway. The body goes unchanged to the router; the manager injects the
        Authorization header from the settings so the key never reaches the VM.
        Streams (SSE) are passed through line by line, upstream errors
        transparently (status + body). Deliberately NO logs of key or body —
        those are exactly what should not leave the host or linger anywhere."""
        parts = _pp.strip("/").split("/")      # api/llm/<backend>/chat/completions
        backend = parts[2] if len(parts) > 2 else ""
        if backend not in LLM_PROXY_UPSTREAMS or parts[3:] != ["chat", "completions"]:
            out = b'{"error":"unknown llm proxy path"}'
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers(); self.wfile.write(out); return
        ok_g, why = _guard_check(_guests.instance_by_ip(self.client_address[0]))
        if not ok_g:
            out = json.dumps({"error": {"message": f"guardrail: {why}", "code": 429}}).encode()
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers(); self.wfile.write(out); return
        url, keyname = LLM_PROXY_UPSTREAMS[backend]
        st = _settings.load_settings()
        # Self-hosted OrcaRouter-Lite: the shared base URL applies to the proxy
        # too — otherwise the detour would suddenly run against the cloud while
        # direct mode talks to the own server.
        if backend == "orcarouter" and (st.get("ORCAROUTER_URL") or "").strip():
            u = st["ORCAROUTER_URL"].strip().rstrip("/")
            if not u.endswith("/chat/completions"):
                u += "/chat/completions" if u.endswith("/v1") else "/v1/chat/completions"
            url = u
        key = (st.get(keyname) or "").strip()
        if not key:
            out = json.dumps({"error": f"{keyname} not configured on host"}).encode()
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers(); self.wfile.write(out); return
        payload = self._raw(_routes.BODY_MAX_LLM)
        ginst = _guests.instance_by_ip(self.client_address[0])
        if ginst is None and self.client_address[0].startswith("172.17."):
            ginst = {"name": "hindsight" if _hindsight.enabled() else "services"}   # booked, not a VM
        span = {"turn": self.headers.get("X-Kaim-Turn", "")[:16],
                "step": self.headers.get("X-Kaim-Step", "") or None}
        _t0 = time.monotonic()
        try:
            want_stream = bool(json.loads(payload or b"{}").get("stream"))
        except (ValueError, AttributeError):
            want_stream = False
        req = urllib.request.Request(url, data=payload, method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "HTTP-Referer": f"https://{_settings.PUBLIC_HOST}",
            "X-Title": "kat56-agent"})
        try:
            r = urllib.request.urlopen(req, timeout=600)
        except urllib.error.HTTPError as e:
            # Pass upstream errors through 1:1: the agent has its own retry
            # logic for 429/5xx and shows 4xx bodies as an error message.
            data = e.read()
            if ginst is not None:            # a failed LLM span, with the reason
                _store.usage_add(ginst["name"], backend, 0, 0, 0, ok=False,
                          err=f"HTTP {e.code}: {data[:300].decode('utf-8', 'replace')}",
                          ms=int((time.monotonic() - _t0) * 1000), **span)
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data); return
        except Exception as e:
            data = json.dumps({"error": f"llm upstream unreachable: {e!r}"}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data); return
        with r:
            self.send_response(r.status)
            self.send_header("Content-Type", r.headers.get("Content-Type", "application/json"))
            if want_stream:
                # Write SSE on line by line and flush — full buffering would kill
                # the token streaming in the agent. readline() blocks only until
                # the next event line, never until the end of the stream. Without
                # Content-Length the response ends with the connection close
                # (HTTP/1.0), urllib in the guest reads until EOF.
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                try:
                    while True:
                        chunk = r.readline()
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        if b'"usage"' in chunk:
                            _proxy_usage(ginst, backend, chunk, ms=int((time.monotonic() - _t0) * 1000), **span)
                except (BrokenPipeError, ConnectionResetError):
                    pass               # client gone -> upstream closes via with
            else:
                data = r.read()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                _proxy_usage(ginst, backend, data, ms=int((time.monotonic() - _t0) * 1000), **span)
