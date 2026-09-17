# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""Settings (settings.json, maintained in the config UI) and site config (site.json, host-specific non-secrets): schema, load/save, what never reaches the browser.

Part of the mgr package: no import from manager.py. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import json
import os
import re
import urllib.request

from mgr import paths as _paths


# Shared secrets/defaults, maintained in the config UI, which pre-fill empty
# template parameters of the same name.
SETTINGS_SCHEMA = [
    {"key": "OPENROUTER_API_KEY", "label": "OpenRouter API key"},
    {"key": "BRAVE_API_KEY", "label": "Brave Search API key (web search for the agents; free tier at brave.com/search/api)"},
    {"key": "ANTHROPIC_API_KEY", "label": "Anthropic API key"},
    {"key": "OPENAI_API_KEY", "label": "OpenAI API key"},
    {"key": "ORCAROUTER_API_KEY", "label": "OrcaRouter API key (sk-orca-…)"},
    {"key": "ORCAROUTER_URL", "label": "OrcaRouter base URL (blank = https://api.orcarouter.ai/v1; set only when self-hosting OrcaRouter-Lite)"},
    {"key": "SIGNAL_NUMBER", "label": "Signal bot number"},
    {"key": "ALLOWED_SENDERS", "label": "Allowed Signal number(s) — who may command the bots and receive its messages; several separated by commas, international format: +4917…, +4915…"},
    {"key": "SIGNAL_API", "label": "Signal REST API URL"},
    {"key": "LLAMA_ENDPOINT", "label": "llama.cpp endpoint (OpenAI-compatible base URL, e.g. http://10.0.0.50:8080/v1)"},
    {"key": "LLAMA_API_KEY", "label": "llama.cpp API key (optional, only if --api-key is set)"},
    {"key": "HINDSIGHT_URL", "label": "Hindsight memory server — optional second memory (facts from every turn, recall + reflect); blank = off, e.g. http://127.0.0.1:8888"},
    {"key": "LLM_KEY_PROXY", "label": "LLM key injection proxy (1 = keys stay on the host, VMs proxy through the manager)", "options": [
        {"value": "", "label": "— off (agent fetches key via broker) —"},
        {"value": "1", "label": "on — keys never leave the host"}]},
    {"key": "TTS_VOICE", "label": "TTS voice (Piper)", "options": [
        {"value": "", "label": "— container default —"},
        {"value": "de-thorsten-high", "label": "German · Thorsten (high)"},
        {"value": "de-thorsten-medium", "label": "German · Thorsten (medium, faster)"},
        {"value": "de-eva_k-x_low", "label": "German · Eva K (x_low, fastest)"},
        {"value": "en-amy-medium", "label": "English · Amy (medium)"}]},
    {"key": "TTS_SPEED", "label": "TTS speed (0.5 slow … 2.0 fast, empty = 1.0)"},
]
# These values NEVER end up in instances/<name>.json and never on the microVM's
# config disk. The agent fetches them at runtime via the secret broker
# (/api/secret/<name>, guest identified by source IP, allowlist per policy).
SECRET_PARAMS = {"OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LLAMA_API_KEY", "ORCAROUTER_API_KEY"}

VOICE_PORT = int(os.environ.get("VOICE_PORT", "8770"))   # voice service, loopback

# Configured secrets never leave the manager in plain text — the UI gets this
# marker and sends it back unchanged on save, where it is discarded. A genuinely
# empty value still deletes the entry.
SETTINGS_KEEP = "__unchanged__"
# MCP_CONFIG carried the substituted secrets in plain text (e.g. the HA bearer
# token). Instead MCP_SERVERS is stored — only the catalog names; the agent
# fetches the values at runtime via /api/mcp-config.
NEVER_PERSIST = SECRET_PARAMS | {"MCP_CONFIG"}


# ---- Site config (site.json): non-secret, host-specific values ----
# Domains/IPs/interface of this installation in ONE place, kept out via
# .gitignore. If the file is missing, public defaults apply (example.com /
# 1.1.1.1 / eth0) — this keeps the repo free of internal infrastructure.
SITE_FILE = os.path.join(_paths.BASE, "site.json")
def load_site():
    try:
        with open(SITE_FILE) as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return {}
SITE = load_site()
PUBLIC_HOST = SITE.get("PUBLIC_HOST") or "example.com"


SIGNAL_HOST = SITE.get("SIGNAL_HOST") or "signal-api.example.com"
# Editor on the host (code-server/openvscode). Only a LINK in the footer, no
# embedding: the manager runs behind HTTPS, the editor usually on plain HTTP in
# the LAN — an iframe would be blocked as mixed content. Empty = no link.
CODE_URL = SITE.get("CODE_URL") or ""
# Where THIS directory (BASE) is mounted inside the editor container — with it
# the Plugins tab links straight to a tool's file in VS Code instead of
# showing the source itself. Empty = names only.
CODE_ROOT = SITE.get("CODE_ROOT") or ""


def load_settings():
    try:
        with open(_paths.SETTINGS_FILE) as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return {}


_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASS|PASSWORD)$")


def is_secret_setting(key):
    """A setting whose value must never reach the browser: the known key
    params and anything named like a credential (a stray HF_TOKEN in the
    file was embedded raw into the admin page once)."""
    return key in SECRET_PARAMS or bool(_SECRET_NAME.search(str(key).upper()))


def settings_for_ui():
    d = dict(load_settings())
    for k in list(d):
        if is_secret_setting(k) and d[k]:
            d[k] = SETTINGS_KEEP
    return d


def save_settings(d):
    cur = load_settings()
    cur.update({k: v for k, v in d.items()
                if isinstance(k, str) and v != SETTINGS_KEEP})
    with open(_paths.SETTINGS_FILE, "w") as fh:
        json.dump(cur, fh, indent=2)
    try:
        os.chmod(_paths.SETTINGS_FILE, 0o600)
    except OSError:
        pass
    return "saved"



_VOICE_LABELS = {"de-thorsten-high": "German · Thorsten (high)", "de-thorsten-medium": "German · Thorsten (medium, faster)",
                 "de-eva_k-x_low": "German · Eva K (x_low, fastest)", "en-amy-medium": "English · Amy (medium)"}


def settings_schema():
    """The settings form's schema; the TTS voice list is whatever the voice
    container actually has installed (a voice added to the image showed up
    in the health line but not in the dropdown), the static list is the
    fallback while the container is down."""
    out = []
    for s in SETTINGS_SCHEMA:
        if s["key"] == "TTS_VOICE":
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{VOICE_PORT}/health", timeout=2) as r:
                    d = json.loads(r.read())
                voices = [v for v in d.get("voices", []) if isinstance(v, str)]
                if voices:
                    s = {**s, "options": [{"value": "", "label": f"— container default ({d.get('voice', '?')}) —"}]
                         + [{"value": v, "label": _VOICE_LABELS.get(v, v)} for v in voices]}
            except Exception:
                pass
        out.append(s)
    return out
