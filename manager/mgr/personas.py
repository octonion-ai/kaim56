# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""Personas: the system prompts an instance can take on, with the tools and model a persona brings.

Part of the mgr package: no import from manager.py. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import json
import os
import re

from mgr import paths as _paths
from mgr import policy as _policy


# ---- Personas / system prompts --------------------------------------------
PERSONAS_FILE = os.path.join(_paths.BASE, "personas.json")
_DEFAULT_PERSONAS = [
    {"name": "assistant",
     "prompt": "You are a helpful agent with tools (shell, files, web, MCP). "
               "Use tools when needed, otherwise answer directly. Keep it brief."},
    {"name": "researcher",
     "prompt": "You are a thorough researcher. Use web_search and http_fetch, check multiple "
               "sources and cite URLs as evidence. Summarize in a structured way. For large "
               "tasks, use spawn_subagent to research sub-questions in parallel."},
    {"name": "coder",
     "prompt": "You are an experienced software developer. Use bash/read_file/write_file in the "
               "workspace, work in small steps, test your result and briefly explain what you do."},
    {"name": "translator",
     "prompt": "You are a precise translator. Translate naturally and idiomatically, without "
               "comments, unless the user explicitly asks for them."},
]


def load_personas():
    try:
        with open(PERSONAS_FILE) as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return data
    except (FileNotFoundError, ValueError):
        save_personas(_DEFAULT_PERSONAS)
        return list(_DEFAULT_PERSONAS)
    return list(_DEFAULT_PERSONAS)


def save_personas(items):
    if not isinstance(items, list):
        return -1
    try:
        with open(PERSONAS_FILE, "w") as fh:
            json.dump(items, fh, indent=2, ensure_ascii=False)
        return len(items)
    except OSError:
        return -1


def upsert_persona(name, prompt, tools=None, model=None):
    name = re.sub(r"[^a-z0-9_-]", "", (name or "").lower())
    if not name:
        return "invalid name (only a-z 0-9 _ -)"
    prev = next((p for p in load_personas() if p.get("name") == name), {})
    items = [p for p in load_personas() if p.get("name") != name]
    ent = {"name": name, "prompt": prompt or ""}
    # A persona may recommend a tool subset and a model, pre-filled when an
    # instance is created from it. None = "not provided" -> keep the existing
    # values (a web save posts only name+prompt); an explicit value replaces,
    # an explicit empty clears.
    if tools is None:
        if prev.get("tools"):
            ent["tools"] = prev["tools"]
    else:
        tl = [t.strip() for t in tools if str(t).strip()] if isinstance(tools, (list, tuple)) else \
             [t.strip() for t in str(tools).split(",") if t.strip()]
        keep = [t for t in tl if t in _policy.AGENT_TOOL_NAMES]
        if keep:
            ent["tools"] = keep
    if model is None:
        if prev.get("model"):
            ent["model"] = prev["model"]
    elif str(model).strip():
        ent["model"] = str(model).strip()[:120]
    items.append(ent)
    save_personas(items)
    return f"persona '{name}' saved"


def delete_persona(name):
    save_personas([p for p in load_personas() if p.get("name") != name])
    return f"persona '{name}' deleted"
