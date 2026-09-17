# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""Talking to a VM's web bridge: which instances have one, waiting for it after a start, one chat turn, and the token stream.

Part of the mgr package: no import from manager.py. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import codecs
import json
import socket
import time
import urllib.request

from mgr import instances as _instances
from mgr import vm as _vm


# ---- Chat (UI under /chat, see chatui.py) ----------------------------------
# Chattable is every instance with TRANSPORT=web: the bridge in the microVM
# serves /api/chat (and optionally /api/chat/stream) on :8080.

def web_instances():
    """Instances you can chat with (+ running state for the UI)."""
    return [{"name": i["name"], "running": _instances.is_running(i),
             "description": i.get("description", "")}
            for i in _instances.load_instances()
            if (i.get("config") or {}).get("TRANSPORT") == "web"]


def wait_web(inst, timeout=120):
    """Starts the instance if needed and waits until the bridge accepts."""
    if not _instances.is_running(inst):
        _vm.start(inst)
    ip = _instances.net_of(inst)["guest"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            socket.create_connection((ip, _instances.WEB_GUEST_PORT), 2).close()
            return True
        except OSError:
            time.sleep(1)
    return False


def guest_chat(inst, message, image=None, timeout=620):
    """Non-streaming call to the bridge in the microVM."""
    payload = {"message": message}
    if image:
        payload["image"] = image
    req = urllib.request.Request(
        f"http://{_instances.net_of(inst)['guest']}:{_instances.WEB_GUEST_PORT}/api/chat",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    body = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
    try:
        return json.loads(body).get("reply", body)
    except ValueError:
        return body


def guest_stream(inst, message, image, on_token, timeout=620):
    """Streams tokens from /api/chat/stream. Bridges without streaming answer on
    the same path with JSON — that then arrives as a single piece."""
    payload = {"message": message}
    if image:
        payload["image"] = image
    req = urllib.request.Request(
        f"http://{_instances.net_of(inst)['guest']}:{_instances.WEB_GUEST_PORT}/api/chat/stream",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
    except Exception:
        on_token(guest_chat(inst, message, image, timeout))
        return
    if "json" in (r.headers.get("Content-Type") or ""):
        body = r.read().decode("utf-8", "replace")
        try:
            on_token(json.loads(body).get("reply", body))
        except ValueError:
            on_token(body)
        return
    dec = codecs.getincrementaldecoder("utf-8")("replace")
    while True:
        raw = r.read(256)
        if not raw:
            break
        tok = dec.decode(raw)
        if tok:
            on_token(tok)
    tail = dec.decode(b"", True)
    if tail:
        on_token(tail)
