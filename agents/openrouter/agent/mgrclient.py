# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""The agent's client for its manager: where the manager is, GET/POST helpers, the LLM key (or the key proxy: the key never enters the VM), the effective LLM URL, and the reports the agent sends back (usage).

Part of the openrouter agent package (runs inside the VM): no import from the package root. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import json
import os
import socket
import urllib.error

from . import config as _config
from . import observe as _observe


def _manager_base():
    """Manager URL as seen from the guest: host gateway (.1 of the /30) on port 8700."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    finally:
        s.close()
    return f"http://{ip.rsplit('.', 1)[0]}.1:8700"


def _mgr(base, path, payload=None, timeout=60):
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(base + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")


def ensure_or_key():
    """Obtain the LLM key and hold it in memory. With llama.cpp the key is
    optional — if it is missing, the agent runs without auth (empty bearer), which
    is the normal case for a server without --api-key and not an error."""
    if _llm_proxy_active():
        # Proxy mode: the manager injects the key while forwarding — the
        # VM needs (and gets) none. A broker fetch here would be exactly
        # the leak the proxy is meant to prevent.
        return ""
    if _config.OR_KEY:
        return _config.OR_KEY
    try:
        d = json.loads(_mgr_get(_manager_base(), f"/api/secret/{_config.LLM_KEY_SECRET}"))
        _config.OR_KEY = d.get("value", "") or ""      # config's module global, written by attribute
        if not _config.OR_KEY and _config.LLM_BACKEND != "llama":
            print(f"{_config.LLM_KEY_SECRET}: {d.get('error', 'not released by the broker')}",
                  flush=True)
    except Exception as e:
        if _config.LLM_BACKEND != "llama":
            print(f"{_config.LLM_KEY_SECRET} could not be obtained from the manager: {e!r}", flush=True)
    return _config.OR_KEY


def _llm_proxy_active():
    """Key-injection proxy on? Only for the router backends — llama.cpp is
    local and has no cloud key worth protecting, so that stays direct."""
    return bool(os.environ.get("KEY_PROXY")) and _config.LLM_BACKEND in ("openrouter",
                                                                 "orcarouter")


def _llm_url():
    """Target URL for chat requests, fresh on each call: in proxy mode the
    manager path (which injects the key), otherwise the direct backend URL.
    Lazy rather than at import, because /model switches the backend at runtime."""
    if _llm_proxy_active():
        return f"{_manager_base()}/api/llm/{_config.LLM_BACKEND}/chat/completions"
    return _config.OR_URL



def _mgr_get(base, path, timeout=30):
    req = urllib.request.Request(base + path, method="GET")
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")


def _llm_headers():
    """Request headers for chat requests. In proxy mode WITHOUT Authorization —
    the manager sets it while forwarding; a bearer from the VM would be
    at best a dummy and only suggest a key were present here."""
    h = {"Content-Type": "application/json",
         "HTTP-Referer": "https://agents.example.com", "X-Title": "kaim56-agent",
         "X-Kaim-Turn": _observe._turn_id[0], "X-Kaim-Step": str(_observe._turn_step[0])}   # the span, for the proxy's books
    if not _llm_proxy_active():
        h["Authorization"] = f"Bearer {ensure_or_key()}"
    return h



# --- report usage -----------------------------------------------------------
def report_usage(u, ms=None, ok=True, err=""):
    """Report tokens/cost of a call to the manager (fire-and-forget) — plus the
    span: turn, step (LLM call index in the turn), duration, and for a call
    that failed after all retries ok:false with the error. The manager
    recognizes the instance by its source IP; we send only numbers and the
    error text. If the manager fails, that must not disturb the chat."""
    if not isinstance(u, dict):
        u = {}
    try:
        payload = json.dumps({
            "model": _config.OR_MODEL,
            "prompt_tokens": u.get("prompt_tokens") or 0,
            "completion_tokens": u.get("completion_tokens") or 0,
            "cost": u.get("cost") or 0.0,
            "turn": _observe._turn_id[0], "step": _observe._turn_step[0], "ms": ms,
            "ok": bool(ok), "err": str(err or "")[:400],
            "direct": bool(_config.LLAMA_ENDPOINT),     # a local model is called directly, not through the key proxy
        }).encode()
        req = urllib.request.Request(f"{_manager_base()}/api/usage", data=payload,
                                     method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass
