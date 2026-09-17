# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""Model catalog: the curated shortlist (models.json) and the OpenRouter model list with its cache.

Part of the mgr package: no import from manager.py. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import json
import os
import time
import urllib.request

from mgr import paths as _paths


_ormodels = {"ts": 0.0, "data": []}


# "Relevant" = curated flagship models (exact IDs). Only those currently
# present in the OpenRouter catalog are shown. Extend as needed.
CURATED = {
    "openai/gpt-4o", "openai/gpt-4o-mini", "openai/gpt-4.1", "openai/gpt-4.1-mini",
    "openai/o3", "openai/o4-mini", "openai/gpt-5", "openai/gpt-5-mini",
    "anthropic/claude-3.7-sonnet", "anthropic/claude-3.5-sonnet", "anthropic/claude-3.5-haiku",
    "anthropic/claude-sonnet-4", "anthropic/claude-sonnet-4.5", "anthropic/claude-opus-4.1",
    "google/gemini-2.0-flash-001", "google/gemini-2.5-pro", "google/gemini-2.5-flash",
    "deepseek/deepseek-chat", "deepseek/deepseek-r1", "deepseek/deepseek-chat-v3.1",
    "deepseek/deepseek-v4-flash-0731",
    "meta-llama/llama-3.3-70b-instruct", "meta-llama/llama-4-maverick",
    "mistralai/mistral-large", "mistralai/mistral-small",
    "qwen/qwen-2.5-72b-instruct", "qwen/qwen3-coder", "x-ai/grok-3", "x-ai/grok-4",
}


MODELS_FILE = os.path.join(_paths.BASE, "models.json")
def load_curated():
    """The curated selection for the create form. Kept as a file so a new model
    comes in via the Models tab instead of via an edit to CURATED + restart.
    If the file is missing, CURATED is the initial seed."""
    try:
        with open(MODELS_FILE) as fh:
            data = json.load(fh)
        ids = data.get("curated") if isinstance(data, dict) else data
        if isinstance(ids, list):
            return {str(i) for i in ids if i}
    except (FileNotFoundError, ValueError, AttributeError):
        pass
    return set(CURATED)


def save_curated(ids):
    clean = sorted({str(i).strip() for i in ids if str(i).strip()})
    with open(MODELS_FILE, "w") as fh:
        json.dump({"curated": clean}, fh, indent=2)
    return f"{len(clean)} models in the shortlist"


def openrouter_models(force=False, tools_only=False, relevant_only=False):
    """OpenRouter models, price ascending. Cached for 10 min; force bypasses the
    cache. tools_only -> only function/tool calling; relevant_only -> only curated."""
    if force or time.time() - _ormodels["ts"] >= 600 or not _ormodels["data"]:
        try:
            req = urllib.request.Request("https://openrouter.ai/api/v1/models",
                                         headers={"User-Agent": "kaim56"})
            d = json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
            rows = []
            for m in d.get("data", []):
                p = m.get("pricing", {}) or {}
                try:
                    pr, co = float(p.get("prompt", 0)), float(p.get("completion", 0))
                except (TypeError, ValueError):
                    continue
                if pr < 0 or co < 0:
                    continue  # hide auto-router / dynamic pricing
                sp = m.get("supported_parameters") or []
                rows.append((pr + co, pr, co, m.get("id", ""), "tools" in sp,
                             m.get("name", ""), m.get("context_length") or 0))
            rows.sort(key=lambda r: r[0])
            out = []
            for tot, pr, co, mid, tools, name, ctx in rows:
                if mid:
                    tag = "free" if tot == 0 else f"${pr*1e6:.2f}/${co*1e6:.2f} /1M"
                    out.append({"id": mid, "label": f"{mid}  ({tag})", "tools": tools,
                                "name": name, "ctx": ctx, "price": tag})
            if out:
                _ormodels["ts"], _ormodels["data"] = time.time(), out
        except Exception as e:
            print(f"[quiet] openrouter model list refresh failed: {e!r}", flush=True)
    data = _ormodels["data"]
    if tools_only:
        data = [m for m in data if m.get("tools")]
    if relevant_only:
        cur = load_curated()
        data = [m for m in data if m["id"] in cur]
    return data
