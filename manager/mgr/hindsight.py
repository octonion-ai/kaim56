# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""Hindsight (vectorize.io) as an optional second memory.

The manager's own memory stays as it is: the key-value store, the semantic
notes in history.db and the Markdown folder. Hindsight adds a memory that
extracts facts from what it is given and answers questions over them
(retain / recall / reflect). It runs as a container on the host and is used
only when HINDSIGHT_URL is set in the settings — every function here is a
no-op without it, and a failing server never disturbs a turn.

One Hindsight bank per instance (bank id = instance name). What goes in:
every chat turn (user text + reply) and every memory_store note. What comes
out: recall hits merged into the per-turn [Memory] block, and the agent tool
memory_reflect for a reasoned answer over the whole bank.

The container gets its LLM through the manager's key proxy, so no API key
lives in the container; its calls are booked under the instance "hindsight".
"""
import json
import re
import threading
import urllib.error
import urllib.request

_settings = None            # callable → settings dict, injected by manager.py
_log = print


def configure(settings_getter, log=print):
    global _settings, _log
    _settings, _log = settings_getter, log


def url():
    try:
        return (_settings().get("HINDSIGHT_URL") or "").strip().rstrip("/") if _settings else ""
    except Exception:
        return ""


def enabled():
    return bool(url())


def bank(instance):
    return (re.sub(r"[^a-zA-Z0-9_-]", "-", str(instance or "")).strip("-")[:64]) or "default"


def _call(method, path, body=None, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url() + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    return json.loads(raw) if raw.strip() else {}


def retain(instance, content, tags=(), wait=False):
    """Hand a piece of text to the bank; Hindsight extracts the facts itself.
    True when accepted. Async on the server side unless wait=True."""
    content = (content or "").strip()
    if not enabled() or not content:
        return False
    body = {"items": [{"content": content[:20000], "tags": [str(t) for t in tags if t]}],
            "async": not wait}
    try:
        _call("POST", f"/v1/default/banks/{bank(instance)}/memories", body, timeout=60 if wait else 20)
        return True
    except Exception as e:
        _log(f"[hindsight] retain failed for {instance}: {e!r}"[:300], flush=True)
        return False


def retain_async(instance, content, tags=()):
    """Fire-and-forget: a turn must never wait for the memory server."""
    if not enabled() or not (content or "").strip():
        return
    threading.Thread(target=retain, args=(instance, content, tuple(tags)), daemon=True).start()


def recall(instance, query, k=5, budget="low"):
    """The bank's best matches for a query, as [{"text", "score", "source"}]."""
    query = (query or "").strip()
    if not enabled() or not query:
        return []
    try:
        d = _call("POST", f"/v1/default/banks/{bank(instance)}/memories/recall",
                  {"query": query, "budget": budget, "max_tokens": 600})
    except Exception as e:
        _log(f"[hindsight] recall failed for {instance}: {e!r}"[:300], flush=True)
        return []
    out = []
    for r in (d.get("results") or [])[:max(1, int(k))]:
        if not isinstance(r, dict):
            continue
        t = r.get("text") or r.get("content") or r.get("fact") or ""
        if str(t).strip():
            out.append({"text": str(t).strip(), "score": r.get("score"), "source": "hindsight"})
    return out


def reflect(instance, query, budget="low"):
    """A reasoned answer over the bank (Hindsight's own model call)."""
    query = (query or "").strip()
    if not enabled():
        return "hindsight is off — set HINDSIGHT_URL in Settings"
    if not query:
        return "question missing"
    try:
        d = _call("POST", f"/v1/default/banks/{bank(instance)}/reflect",
                  {"query": query, "budget": budget, "max_tokens": 800}, timeout=120)
    except urllib.error.HTTPError as e:
        return f"hindsight HTTP {e.code}: {e.read()[:200].decode('utf-8', 'replace')}"
    except Exception as e:
        return f"hindsight error: {e!r}"
    return (d.get("text") or "").strip() or "(no answer)"


def health():
    """(ok, detail) — the session panel and the Settings tab show it."""
    if not enabled():
        return False, "off"
    try:
        _call("GET", "/health", timeout=5)
        d = _call("GET", "/v1/default/banks", timeout=5)
        n = len(d.get("banks", d) if isinstance(d, dict) else d)
        return True, f"reachable · {n} banks"
    except Exception as e:
        return False, f"unreachable ({e.__class__.__name__})"
