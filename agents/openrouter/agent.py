#!/usr/bin/env python3
# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""OpenRouter agent with tool-calling — model-agnostic, runs inside the microVM.

Tools: bash, read_file, write_file, list_dir, http_fetch  + optional MCP servers
(stdio), fetched from the manager at runtime. Transports: signal | web (via TRANSPORT). Stdlib only.
"""
import itertools
import json
import os
import re
import socket
import subprocess
import threading
import time
import datetime
import urllib.parse
import urllib.request
import urllib.error
import uuid

# --- config -----------------------------------------------------------------
# The key is deliberately NO LONGER kept in the instance config (and thus not on
# the microVM's config disk). Env remains a fallback for legacy setups; otherwise
# it is fetched once from the manager on first need — which recognizes the guest
# by its source IP and checks the allowlist from secret-policy.json.
OR_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OR_MODEL = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o")
OR_URL = os.environ.get("OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions")

# Self-hosted LLM via llama.cpp (OpenAI-compatible). If LLAMA_ENDPOINT is set,
# the agent talks to the local server instead of OpenRouter — same code, just a
# different base URL, model name and (optional) key. The endpoint comes from the
# shared settings via the instance config, the key as a secret via the broker
# (LLAMA_API_KEY, may be absent -> no auth).
LLAMA_ENDPOINT = os.environ.get("LLAMA_ENDPOINT", "").strip()
# OrcaRouter: an OpenAI-compatible gateway like OpenRouter, just a different base
# URL and an sk-orca key. If ORCAROUTER_MODEL is set (or a custom URL when
# self-hosting OrcaRouter-Lite), the agent talks to OrcaRouter instead of
# OpenRouter. The key comes as a secret via the broker (ORCAROUTER_API_KEY).
ORCA_URL = os.environ.get("ORCAROUTER_URL", "").strip()
ORCA_MODEL = os.environ.get("ORCAROUTER_MODEL", "").strip()


def _openai_chat_url(base):
    """Bring a base URL to the full /chat/completions path — no matter whether
    ".../v1", ".../v1/chat/completions" or a bare "host:port" comes in."""
    u = base.rstrip("/")
    if u.endswith("/chat/completions"):
        return u
    if u.endswith("/v1"):
        return u + "/chat/completions"
    return u + "/v1/chat/completions"


LLM_BACKEND = "openrouter"
LLM_NAME = "OpenRouter"
LLM_KEY_SECRET = "OPENROUTER_API_KEY"
if LLAMA_ENDPOINT:
    LLM_BACKEND = "llama"
    LLM_NAME = "llama.cpp"
    LLM_KEY_SECRET = "LLAMA_API_KEY"
    OR_URL = _openai_chat_url(LLAMA_ENDPOINT)
    OR_MODEL = os.environ.get("LLAMA_MODEL") or os.environ.get("OPENROUTER_MODEL") or "local-model"
elif ORCA_MODEL or ORCA_URL:
    LLM_BACKEND = "orcarouter"
    LLM_NAME = "OrcaRouter"
    LLM_KEY_SECRET = "ORCAROUTER_API_KEY"
    OR_URL = _openai_chat_url(ORCA_URL or "https://api.orcarouter.ai/v1")
    OR_MODEL = ORCA_MODEL or os.environ.get("OPENROUTER_MODEL") or "openai/gpt-4o"
# Key-injection proxy (OneCLI pattern): with KEY_PROXY=1 (config disk) the chat
# requests go to the manager, which injects the backend key while forwarding
# — the key never reaches the VM. The target URL is built LAZILY on purpose in
# _llm_url(): _manager_base() is not yet defined here, and /model can switch the
# backend at runtime. llama.cpp stays direct (locally reachable, key optional —
# there is nothing to hide there).
WORKDIR = os.environ.get("CLAUDE_WORKDIR", "/home/node/workspace")
BASH_TIMEOUT = int(os.environ.get("BASH_TIMEOUT", "120"))
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "12"))
MAX_TOOL_OUT = int(os.environ.get("MAX_TOOL_OUT", "8000"))
# Heartbeat during tool execution: slow local models + long-running tools
# (apt, downloads) produce minutes of byte silence -> a proxy/client idle
# timeout (Traefik default 180s) would otherwise cut the stream mid-sentence.
HEARTBEAT_SEC = int(os.environ.get("HEARTBEAT_SEC", "30"))
SYSTEM = os.environ.get("AGENT_SYSTEM",
    "You are a helpful agent with tools (shell, files, web, MCP). "
    "Work in the directory %s. Use tools when needed, otherwise answer directly. "
    "Keep it brief." % WORKDIR)

# Missions are open to EVERY agent (not just the orchestrator): whoever gets a
# multi-stage assignment owns the plan and delegates the steps to the instance
# that has the needed tools/MCP.
SYSTEM += (
    "\n\nMissions: If the user gives you a MULTI-STAGE assignment (several "
    "tasks/days), IMMEDIATELY create a mission with clear steps via "
    "mission_start. The mission is YOURS (you own the plan), the steps may run "
    "ANYWHERE: per push pick the capable instance with list_agents — the one "
    "that has the needed tools/MCP (e.g. hass for HomeAssistant) — kick the step "
    "off with create_task(target=<that instance>) and record task id AND target "
    "on the step with mission_update (status doing). Only use target 'ephemeral' "
    "when no existing agent fits. Once a task is done, you are triggered "
    "automatically: check the result, set the step to done/failed, kick off the "
    "next step. All steps done -> mission_finish with a conclusion. Blocked -> "
    "notify the user. Simple one-off assignments stay ordinary tasks WITHOUT a "
    "mission.")

# Runtime self-knowledge: the agent should know WHAT it is running on, so that it
# answers "which model do you use?" correctly and does not mistakenly pull in the
# template (list_agents shows OTHER agents for routing).
SYSTEM += (f"\n\nRuntime: You run via {LLM_NAME} with the model "
           f"'{OR_MODEL}'. If anyone asks about your model/backend, name exactly "
           f"that — do NOT use list_agents for it (that lists other agents to "
           f"delegate to, not you). Your TOOLS (shell, http_fetch, web_search, "
           f"files) execute inside YOUR OWN microVM on the user's host and reach "
           f"the internet through the host's connection — NOT on the model "
           f"provider's servers. Never claim a fetch failed because of where the "
           f"model runs; when a fetch fails, quote the actual error, and when an "
           f"earlier attempt failed, just try again instead of concluding you "
           f"are blocked.")

# Appended to EVERY system prompt, personas included: the memory tools are
# built in, so the instruction for them belongs here — not in each persona
# individually, where it would be lost on the next edit.
SYSTEM += (
    "\n\nMemory: Within a conversation you remember what was said so far quite "
    "normally — use that as a matter of course and do NOT explain to the user, "
    "unprompted, how your memory works or that it resets. Across conversations "
    "and restarts, only what you deliberately store persists: whatever future "
    "conversations need — the user's preferences, decisions made, ongoing "
    "projects, learned quirks of the environment — you store immediately and "
    "silently with memory_store. The key is short (for updating); the value is a "
    "COMPLETE, self-contained statement (a full sentence), because it is later "
    "retrieved by meaning — 'Ulrich's favorite mountain to hike is the "
    "Watzmann', not just 'Watzmann'. Update existing entries under the same key. "
    "Keep no running log: do not store fleeting details. Matching earlier notes "
    "are surfaced to you automatically; memory_recall provides more when needed. "
    "When the user shares a document of LASTING relevance (a CV, a contract, a "
    "project brief — marked '[Attached document: …]'), store its essence with "
    "memory_store in the same turn, unasked: for a CV e.g. the profile you "
    "derived (roles, focus areas, region). A /reset must not cost that work. "
    "\n\nWhere knowledge goes — pick by kind, not by mood: FACTS about the "
    "user, their projects or this environment -> memory_store. RULES on how to "
    "do something ('always X', a correction of your approach) -> playbook_add. "
    "Expertise for a task at hand -> load_skill (borrowed, not stored). What "
    "was already DONE -> recall_tasks looks it up; do not store task outcomes "
    "in memory, the history has them.")

SYSTEM += (
    "\n\nPlaybooks (fixed rules): If the user tells you HOW something is to be "
    "done, states a lasting preference ('always …', 'for X use Y') or corrects "
    "your approach, capture it IMMEDIATELY and silently with playbook_add as a "
    "short, concrete rule — that way your knowledge grows with their wishes. The "
    "rules surfaced under [Playbooks] you always follow. With playbooks you show "
    "them, with playbook_forget you remove one.")

# Behavioral guardrails, adapted in spirit from Anthropic's published system
# prompts (the model-agnostic parts) — applies to every model behind this
# agent, personas included.
SYSTEM += (
    "\n\nWorking style: Invent nothing. If you are not sure whether something is "
    "true or still current, say so openly and check it with web_search/"
    "http_fetch instead of guessing; do not invent sources, quotes or links. "
    "Before claiming you cannot do something or have no access, check whether "
    "there is a tool for it, and use it — acting yourself comes before asking "
    "for it. On unclear requests make a sensible assumption and get going; only "
    "ask back when it genuinely cannot proceed without the detail. A task you "
    "have started you carry to the end instead of stopping halfway.\n"
    "Tone: matter-of-fact, without flattery and without excessive apologies; "
    "disagree kindly and with reasons when you are of a different opinion, "
    "instead of caving. Drop empty filler words like 'honestly', 'really' or "
    "'actually' — just say it directly. Answer concisely and in prose; lists, "
    "bolding and headings only when the content truly calls for them or you are "
    "asked for them; keep caveats short, the main part is the answer. You do not "
    "speculate about the intentions or state of mind of others.")


# Model reasoning/thinking (OpenRouter reasoning parameter). None = off.
# --- /model: switch model (and optionally backend) at runtime ---------------
# Like pi.dev: switch up mid-session ("/model orcarouter:
# anthropic/claude-sonnet-4.6") and back again — without a restart, the context
# stays. Only effective until restart; the instance config remains authoritative.
_MODEL_BACKENDS = {
    "openrouter": ("OpenRouter", "https://openrouter.ai/api/v1/chat/completions",
                   "OPENROUTER_API_KEY"),
    "orcarouter": ("OrcaRouter", "https://api.orcarouter.ai/v1/chat/completions",
                   "ORCAROUTER_API_KEY"),
}


def _set_model(cmd):
    global OR_MODEL, OR_URL, LLM_NAME, LLM_KEY_SECRET, LLM_BACKEND, OR_KEY
    rest = cmd[len("/model"):].strip()
    if not rest or rest in ("show", "status"):
        return f"🧠 Model: {OR_MODEL} via {LLM_NAME} ({_llm_url()})"
    if ":" in rest and rest.split(":", 1)[0] in _MODEL_BACKENDS:
        prov, mdl = rest.split(":", 1)
        name, url, secret = _MODEL_BACKENDS[prov]
        LLM_BACKEND, LLM_NAME, OR_URL, LLM_KEY_SECRET = prov, name, url, secret
        OR_KEY = ""                      # fetch the new backend's key from the broker
        OR_MODEL = mdl.strip()
    else:
        OR_MODEL = rest
    return f"🧠 Model now: {OR_MODEL} via {LLM_NAME} (until restart)"


def _set_steps(cmd):
    """/steps [n|unlimited] — change the max tool steps per turn at runtime
    (until restart; permanently: AGENT_MAX_STEPS in the instance config).
    '/steps 30' = up to 30 rounds, '/steps unlimited' = unlimited (then only the
    guardrails limit: token budget + rate limit at the key proxy)."""
    global MAX_STEPS
    rest = cmd[len("/steps"):].strip().lower()
    if not rest:
        cur = "unlimited" if MAX_STEPS <= 0 else MAX_STEPS
        return (f"🔢 max tool steps per turn: {cur}"
                "  ·  /steps <1..x> or /steps unlimited")
    if rest in ("unlimited", "unbegrenzt", "inf", "infinite", "\u221e", "0", "none", "off"):
        MAX_STEPS = 0
        return ("🔢 max tool steps now: unlimited (until restart) "
                "\u2014 only the guardrails still limit")
    try:
        MAX_STEPS = max(1, int(rest))
    except ValueError:
        return "Usage: /steps <1..x> or /steps unlimited"
    return f"🔢 max tool steps now: {MAX_STEPS} (until restart)"


def _step_iter():
    """Iterator for the tool rounds: bounded (range) or unbounded
    (itertools.count) when MAX_STEPS<=0. Reads MAX_STEPS fresh on each call."""
    return itertools.count() if MAX_STEPS <= 0 else range(MAX_STEPS)


# Default from env (OPENROUTER_REASONING), switchable at runtime via /reasoning.
_reasoning = (os.environ.get("OPENROUTER_REASONING", "").strip().lower() or None)
if _reasoning not in (None, "low", "medium", "high"):
    _reasoning = None


# Marker for the thinking/reasoning block in the token stream. Visible Unicode
# brackets: they practically never occur in normal text and are NOT stripped by
# the security gateway (no zero-width/tag characters). Web and app collapse the
# region between the markers as "thinking".
THINK_START = "\u27E6think\u27E7"
THINK_END = "\u27E6/think\u27E7"


def _set_reasoning(cmd):
    """/reasoning [off|low|medium|high] — toggle without an argument (off <-> medium)."""
    global _reasoning
    arg = cmd[len("/reasoning"):].strip().lower()
    if arg in ("off", "aus", "0", "none", "false"):
        _reasoning = None
    elif arg in ("low", "medium", "high"):
        _reasoning = arg
    elif arg == "":
        _reasoning = None if _reasoning else "medium"
    else:
        return "Usage: /reasoning [off|low|medium|high]"
    return f"🧠 Reasoning {'off' if _reasoning is None else 'on (' + _reasoning + ')'}."


def log(*a):
    import time
    print(time.strftime("%F %T"), *a, flush=True)


# --- built-in tools ---------------------------------------------------------
def t_bash(command):
    p = subprocess.run(command, shell=True, cwd=WORKDIR, capture_output=True,
                       text=True, timeout=BASH_TIMEOUT)
    return (p.stdout + p.stderr).strip() or f"(exit {p.returncode}, no output)"


def _safe(path):
    p = os.path.abspath(os.path.join(WORKDIR, path)) if not os.path.isabs(path) else path
    return p


def t_read_file(path):
    with open(_safe(path)) as f:
        return f.read(MAX_TOOL_OUT)


def t_write_file(path, content):
    p = _safe(path)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p, "w") as f:
        f.write(content)
    return f"written: {p} ({len(content)} characters)"


def t_list_dir(path="."):
    return "\n".join(sorted(os.listdir(_safe(path)))) or "(empty)"


def _html_to_text(html):
    """Readable text out of an HTML page: scripts/styles gone, block tags as
    line breaks, entities resolved, whitespace collapsed. Links keep their
    target in brackets so the model can follow them with another fetch."""
    import html as _h
    t = html.replace("\r", "")
    t = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", t)
    t = re.sub(r'(?is)<a[^>]*href=["\'](https?://[^"\']+)["\'][^>]*>(.*?)</a>',
               lambda m: re.sub(r"<[^>]+>", "", m.group(2)) + " [" + m.group(1) + "]", t)
    t = re.sub(r"(?i)<(br|/p|/div|/li|/tr|/h[1-6]|/section|/article)[^>]*>", "\n", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = _h.unescape(t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n[ \t]*", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def t_http_fetch(url, method="GET", raw=False):
    """Fetch a URL. HTML is converted to readable text (a modern page is 90%
    markup and scripts — hard-truncated raw HTML used to cut content off before
    it ever appeared, and the model concluded pages were "too complex").
    raw=true returns the unconverted body for the cases that need markup."""
    req = urllib.request.Request(url, method=method, headers={
        # A browser UA: big job/news portals 403 obvious bot agents outright.
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) "
                      "Gecko/20100101 Firefox/120.0",
        "Accept-Language": "de,en;q=0.7"})
    r = urllib.request.urlopen(req, timeout=30)
    body = r.read(5_000_000).decode("utf-8", "replace")
    ctype = (r.headers.get("Content-Type") or "").lower()
    if raw or not ("html" in ctype or body.lstrip()[:200].lower().startswith(("<!doctype", "<html"))):
        return body
    return _html_to_text(body)


def t_read_pdf(path, pages=""):
    """Extract PDF text via pdftotext. `path` = workspace file OR
    http(s) URL. `pages` optional as a range, e.g. '1-5'."""
    import re
    import tempfile
    tmp = None
    try:
        if str(path).startswith(("http://", "https://")):
            data = urllib.request.urlopen(
                urllib.request.Request(path, headers={"User-Agent": "or-agent"}),
                timeout=30).read(50 * 1024 * 1024)
            tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
            tmp.write(data)
            tmp.close()
            src = tmp.name
        else:
            src = _safe(path)
        cmd = ["pdftotext", "-q"]
        m = re.match(r"(\d+)-(\d+)$", (pages or "").strip())
        if m:
            cmd += ["-f", m.group(1), "-l", m.group(2)]
        cmd += [src, "-"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        txt = (r.stdout or "").strip()
        if not txt:
            return ("PDF error: " + (r.stderr.strip()[:300] or "")
                    if r.returncode != 0
                    else "(no text in the PDF — possibly a scanned image without a text layer)")
        return txt[:MAX_TOOL_OUT]
    except Exception as e:
        return f"Error: {e!r}"
    finally:
        if tmp:
            try:
                os.remove(tmp.name)
            except OSError:
                pass


def _ddg_search(query, count):
    """DuckDuckGo HTML. Returns a result list, or None when DDG serves its
    bot challenge instead of results (HTTP 202 + "anomaly" page — since
    2026-09 the norm for datacenter IPs, found via a user's empty search)."""
    q = urllib.parse.quote(query)
    req = urllib.request.Request(
        "https://html.duckduckgo.com/html/?q=" + q,
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"})
    r = urllib.request.urlopen(req, timeout=30)
    html = r.read(1_000_000).decode("utf-8", "replace")
    if r.status != 200 or "anomaly" in html[:4000] or "challenge" in html[:4000]:
        return None                       # blocked -> let the caller try elsewhere
    hrefs = re.findall(r'class="result__a"[^>]*href="([^"]+)"', html)
    titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>', html, re.S)
    snips = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.S)

    def real(h):
        m = re.search(r"uddg=([^&]+)", h)
        return urllib.parse.unquote(m.group(1)) if m else h

    return [(_ws_clean(titles[i]) if i < len(titles) else "",
             real(hrefs[i]),
             _ws_clean(snips[i]) if i < len(snips) else "")
            for i in range(min(count, len(hrefs)))]


def _bing_search(query, count):
    """Bing HTML. Result URLs are /ck/a redirects carrying the target
    base64-encoded in u=a1<payload> — decoded here."""
    q = urllib.parse.quote(query)
    req = urllib.request.Request(
        "https://www.bing.com/search?q=" + q + "&count=" + str(max(count, 10)),
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
                 "Accept-Language": "de,en;q=0.7"})
    html = urllib.request.urlopen(req, timeout=30).read(2_000_000).decode("utf-8", "replace")
    out = []
    # Per result block, not one regex across the page: the snippet <p> sits at
    # varying depths and a greedy pattern either misses it or bleeds across
    # blocks.
    for block in html.split('<li class="b_algo"')[1:]:
        block = block.split("</li>", 1)[0]
        a = re.search(r'<h2[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not a:
            continue
        p = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        out.append((_ws_clean(a.group(2)), _bing_real_url(a.group(1)),
                    _ws_clean(p.group(1)) if p else ""))
        if len(out) >= count:
            break
    return out


def _bing_real_url(href):
    """Bing /ck/a redirect -> target URL (u=a1<urlsafe-base64>)."""
    import base64
    h = urllib.parse.unquote(href.replace("&amp;", "&"))
    m = re.search(r"[&?]u=a1([A-Za-z0-9_\-]+)", h)
    if not m:
        return h
    raw = m.group(1)
    try:
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode(
            "utf-8", "replace")
    except Exception:
        return h


def _ws_clean(s):
    return re.sub(r"<[^>]+>", "", s).replace("&amp;", "&").replace("&#x27;", "'").strip()


def t_web_search(query, count=5):
    """Web search. First choice: the manager's /api/websearch — it holds the
    Brave API key (never enters the VM) and falls back to DDG/Bing itself.
    The direct backends below only run when the manager route is missing
    (older manager). "no results" means the query found nothing; a dead
    backend must SAY so — otherwise the model concludes the thing searched
    for does not exist."""
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 5
    try:
        q = urllib.parse.urlencode({"q": query, "count": count})
        d = json.loads(_mgr_get(_manager_base(), "/api/websearch?" + q))
        if d.get("result"):
            return d["result"]
        if d.get("error"):
            raise ValueError(d["error"])
    except Exception:
        pass                      # manager route missing/broken -> go direct
    errors = []
    for name, backend in (("duckduckgo", _ddg_search), ("bing", _bing_search)):
        try:
            rows = backend(query, count)
        except Exception as e:
            errors.append(f"{name}: {e!r}")
            continue
        if rows is None:
            errors.append(f"{name}: blocked (bot challenge)")
            continue
        if rows:
            return "\n".join(f"{i+1}. {t}\n   {u}\n   {sn}"
                              for i, (t, u, sn) in enumerate(rows))
        return "no results"
    return ("⚠️ web search unavailable — every backend failed ("
            + "; ".join(errors) + "). This is an infrastructure problem, "
            "NOT an empty result: tell the user instead of concluding "
            "nothing exists.")

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
    global OR_KEY
    if _llm_proxy_active():
        # Proxy mode: the manager injects the key while forwarding — the
        # VM needs (and gets) none. A broker fetch here would be exactly
        # the leak the proxy is meant to prevent.
        return ""
    if OR_KEY:
        return OR_KEY
    try:
        d = json.loads(_mgr_get(_manager_base(), f"/api/secret/{LLM_KEY_SECRET}"))
        OR_KEY = d.get("value", "") or ""
        if not OR_KEY and LLM_BACKEND != "llama":
            print(f"{LLM_KEY_SECRET}: {d.get('error', 'not released by the broker')}",
                  flush=True)
    except Exception as e:
        if LLM_BACKEND != "llama":
            print(f"{LLM_KEY_SECRET} could not be obtained from the manager: {e!r}", flush=True)
    return OR_KEY


def _llm_proxy_active():
    """Key-injection proxy on? Only for the router backends — llama.cpp is
    local and has no cloud key worth protecting, so that stays direct."""
    return bool(os.environ.get("KEY_PROXY")) and LLM_BACKEND in ("openrouter",
                                                                 "orcarouter")


def _llm_url():
    """Target URL for chat requests, fresh on each call: in proxy mode the
    manager path (which injects the key), otherwise the direct backend URL.
    Lazy rather than at import, because /model switches the backend at runtime."""
    if _llm_proxy_active():
        return f"{_manager_base()}/api/llm/{LLM_BACKEND}/chat/completions"
    return OR_URL


def _llm_headers():
    """Request headers for chat requests. In proxy mode WITHOUT Authorization —
    the manager sets it while forwarding; a bearer from the VM would be
    at best a dummy and only suggest a key were present here."""
    h = {"Content-Type": "application/json",
         "HTTP-Referer": "https://agents.example.com", "X-Title": "kaim56-agent",
         "X-Kaim-Turn": _turn_id[0], "X-Kaim-Step": str(_turn_step[0])}   # the span, for the proxy's books
    if not _llm_proxy_active():
        h["Authorization"] = f"Bearer {ensure_or_key()}"
    return h


def t_spawn_subagent(task, model=None):
    """Delegate a self-contained subtask to a FRESH ephemeral VM and return its
    answer. Runs over the manager's task path (create_task target=ephemeral,
    wait=true) — the manager creates, drives and deletes the VM; the guest
    never touches the admin routes (which it may not call anyway). `model`
    picks the subagent's OpenRouter model, default: the template's."""
    payload = {"message": str(task or "").strip(), "target": "ephemeral",
               "wait": True, "model": (model or "").strip()}
    if not payload["message"]:
        return "⚠️ task missing"
    try:
        body = _mgr(_manager_base(), "/api/task", payload, timeout=630)
        d = json.loads(body)
    except Exception as e:
        return f"Subagent failed: {e!r}"
    if d.get("error"):
        return f"⚠️ {d['error']}"
    if "result" in d:
        return str(d["result"]) or "(subagent returned no result)"
    return "(subagent returned no result)"


def t_create_task(task, target="ephemeral", schedule="", wait=False, model=""):
    """Queue a task for execution — on a CAPABLE instance or
    isolated in an ephemeral VM. The manager runs it; the result
    appears in the shared chat history (app/web). `model` applies to
    ephemeral targets only (the VM is created with it)."""
    payload = {"message": task, "target": (target or "ephemeral").strip(),
               "schedule": (schedule or "").strip(), "wait": bool(wait),
               "model": (model or "").strip()}
    try:
        body = _mgr(_manager_base(), "/api/task", payload,
                    timeout=630 if wait else 30)
        d = json.loads(body)
        if d.get("error"):
            return f"⚠️ {d['error']}"
        if "result" in d:                      # wait=True -> result directly
            return str(d["result"])
        return (f"Task queued (id {d.get('id')}, target {d.get('target')}, "
                f"{d.get('status')}). The result will appear in the chat.")
    except Exception as e:
        return f"Error: {e!r}"


def t_mission_start(goal, steps):
    """Create a multi-stage assignment as a mission: goal + planned steps.
    The progress lives in the manager and survives restart/reset."""
    if isinstance(steps, str):
        steps = [x.strip() for x in steps.split("\n") if x.strip()]
    try:
        d = json.loads(_mgr(_manager_base(), "/api/mission-start",
                            {"goal": goal, "steps": steps}, timeout=10))
        return f"Mission {d['id']} created." if d.get("id") else f"Not created: {d.get('note','')}"
    except Exception as e:
        return f"Error: {e!r}"


def t_missions():
    """List active/paused missions with steps and status."""
    try:
        ms = json.loads(_mgr_get(_manager_base(), "/api/missions", timeout=8)).get("missions", [])
        if not ms:
            return "no missions"
        out = []
        for m in ms:
            if m.get("status") in ("done", "failed"):
                continue
            steps = " | ".join(f"{st['n']}[{st['status']}] {st['text'][:60]}"
                               + (f" (task {st['task_id']})" if st.get("task_id") else "")
                               for st in m.get("steps", []))
            out.append(f"{m['id']} [{m['status']}] {m['goal'][:80]} :: {steps}")
        return "\n".join(out) or "no open missions"
    except Exception as e:
        return f"Error: {e!r}"


def t_mission_update(id, step=None, status="", result="", task_id="", add_step="",
                     note="", target=""):
    """Advance a mission step: status open|doing|done|failed, result brief,
    record the task_id of the kicked-off task and the target instance it went
    to; add_step appends a new step; note only writes to the log."""
    try:
        body = {"id": id, "status": status, "result": result,
                "task_id": task_id, "add_step": add_step, "note": note,
                "target": target}
        if step is not None:
            body["step"] = int(step)
        d = json.loads(_mgr(_manager_base(), "/api/mission-update", body, timeout=10))
        return d.get("msg", "?")
    except Exception as e:
        return f"Error: {e!r}"


def t_mission_finish(id, summary, failed=False):
    """Finish a mission (or end it as failed with failed=true).
    The conclusion goes into long-term memory, the user gets a notification."""
    try:
        d = json.loads(_mgr(_manager_base(), "/api/mission-finish",
                            {"id": id, "summary": summary, "failed": bool(failed)}, timeout=10))
        return d.get("msg", "?")
    except Exception as e:
        return f"Error: {e!r}"


ORACLE_MODEL = os.environ.get("ORACLE_MODEL", "").strip()   # empty = current model
ORACLE_PROMPT = (
    "You are a skeptical advisor (Oracle): a second opinion BEFORE an action. "
    "You NEVER act yourself. Question the assumptions: Does the action fit the "
    "actual assignment? Is the target unambiguously identified (ID + content, "
    "not just time/name)? What would the damage be if the assumption is wrong? "
    "Answer concisely: first 'OBJECTION:' with the strongest counter-argument "
    "(or 'NO OBJECTION'), then at most 3 lines of reasoning/recommendation.")


def t_oracle(plan, kontext=""):
    """Second opinion before an action (pi.dev idea 'oracle'): challenge the
    assumptions, without acting yourself. An extra LLM call without tools; via
    ORACLE_MODEL optionally a stronger model."""
    msgs = [{"role": "system", "content": ORACLE_PROMPT},
            {"role": "user", "content": f"PLANNED ACTION:\n{plan}\n\nCONTEXT:\n{kontext or '(none)'}"}]
    r = or_chat(msgs, [], model=ORACLE_MODEL or None)
    return (r.get("content") or "").strip() or "(Oracle gave no answer — when in doubt do NOT act)"


def t_ha_control(spoken, action):
    """Turn a Home Assistant device or whole room on/off by the name you HEARD —
    the manager matches it against real entities and areas server-side (exact,
    then area, then closest-sounding) and auto-learns a spoken alias on a fuzzy
    hit, so the same wording is instant next time. PREFER this for voice light/
    device control over the raw homeassistant intents: pass the spoken target
    verbatim ('Gartenhaus denke rechts', 'Licht im Gartenhaus') and action
    'on'/'off'. It also handles rooms ('Licht im Gartenhaus' -> all lights of
    that area)."""
    try:
        return _mgr(_manager_base(), "/api/ha-control",
                    {"spoken": spoken, "action": action}, timeout=25)
    except urllib.error.HTTPError as e:
        return f"⚠️ HA control failed: HTTP {e.code}"
    except Exception as e:
        return f"⚠️ HA control failed: {e!r}"


def t_ha_learn_alias(spoken, entity):
    """Teach Home Assistant that a spoken/misheard name refers to an entity, so
    the SAME wording matches natively next time. Use this after you recovered
    from a failed HA intent: you heard e.g. 'Gartenhaus denke rechts', found the
    real entity 'light.gartenhaus_decke_rechts' via GetLiveContext, and switched
    it — then call ha_learn_alias('Gartenhaus denke rechts',
    'light.gartenhaus_decke_rechts'). The HA token stays on the host; you pass
    only the words and the entity id."""
    try:
        return _mgr(_manager_base(), "/api/ha-alias",
                    {"spoken": spoken, "entity": entity}, timeout=20)
    except urllib.error.HTTPError as e:
        return f"⚠️ alias not learned: HTTP {e.code}"
    except Exception as e:
        return f"⚠️ alias not learned: {e!r}"


def t_notify(title, message=""):
    """Send a push notification to the user's devices (app as an
    Android system notification, web manager as a bell). For important
    events/results when the user is not in the chat. Unlike
    send_signal (which rings in Signal), this is the app/web channel. Delivery
    goes through the manager."""
    try:
        body = _mgr(_manager_base(), "/api/notify",
                    {"title": title, "message": message}, timeout=15)
        d = json.loads(body)
        return "Notification sent." if d.get("id") else \
            "⚠️ not sent: " + str(d.get("note", ""))
    except urllib.error.HTTPError as e:
        try:
            return "⚠️ not sent: " + str(json.loads(e.read()).get("note", e.code))
        except Exception:
            return f"⚠️ not sent (HTTP {e.code})"
    except Exception as e:
        return f"⚠️ Error: {e!r}"


def t_send_signal(text, to=""):
    """Write to the user via Signal. Delivery runs in the manager: the
    bot number and the API access live there, and the recipient is checked
    against the list of allowed numbers. So from here you cannot
    write to arbitrary numbers — by design."""
    try:
        body = _mgr(_manager_base(), "/api/signal",
                    {"text": text, "to": (to or "").strip()}, timeout=45)
        d = json.loads(body)
        return ("Signal sent: " if d.get("ok") else "⚠️ not sent: ") + str(d.get("note", ""))
    except urllib.error.HTTPError as e:
        try:
            return "⚠️ not sent: " + str(json.loads(e.read()).get("note", e.code))
        except Exception:
            return f"⚠️ not sent: HTTP {e.code}"
    except Exception as e:
        return f"Error: {e!r}"


def t_read_inbox(peek=False):
    """Read new user messages (Signal/app/web) since the last run —
    the orchestrator's inbox. By default each message is delivered only
    ONCE (watermark). peek=True returns without 'consuming'."""
    try:
        body = _mgr_get(_manager_base(), "/api/inbox" + ("?peek=1" if peek else ""))
        msgs = json.loads(body).get("messages", [])
        if not msgs:
            return "Inbox empty (nothing new)"
        out = []
        for m in msgs:
            who = m.get("instance") or m.get("title") or "?"
            out.append(f"[{who}] {str(m.get('text',''))[:200]}")
        return "\n".join(out)
    except Exception as e:
        return f"Error: {e!r}"


def t_list_agents():
    """List available agent instances + capabilities (model, MCP) —
    for routing: choose as the create_task target the agent that has the needed
    tools/MCP (e.g. the one with the homeassistant MCP for lights/heating)."""
    try:
        rows = json.loads(_mgr_get(_manager_base(), "/api/agents")).get("agents", [])
        if not rows:
            return "no agents"
        out = []
        for a in rows:
            mcp = (" mcp:" + ",".join(a["mcps"])) if a.get("mcps") else ""
            st = "running" if a.get("running") else "off"
            out.append(f"{a['name']} [{st}] {a.get('backend') or a.get('template','')} {a.get('model','')}{mcp}")
        return "\n".join(out)
    except Exception as e:
        return f"Error: {e!r}"


def t_recall_tasks(query="", limit=10):
    """Query previously executed tasks (long-term memory / base knowledge).
    Without query the most recent; with query, search by text in task/result/goal.
    Use this BEFORE creating new tasks to avoid duplicates."""
    try:
        q = urllib.parse.quote(query or "")
        body = _mgr_get(_manager_base(), f"/api/history?q={q}&limit={int(limit)}")
        rows = json.loads(body).get("rows", [])
        if not rows:
            return "no matching earlier tasks"
        out = []
        for r in rows:
            ts = time.strftime("%m-%d %H:%M", time.localtime(r.get("ts", 0)))
            ok = "" if r.get("ok") else "⚠️ "
            out.append(f"[{ts}] {ok}{r.get('target')}: {str(r.get('task',''))[:80]}"
                       f" -> {str(r.get('result','') or '')[:140]}")
        return "\n".join(out)
    except Exception as e:
        return f"Error: {e!r}"


def t_list_tasks():
    """List running/scheduled tasks with IDs — needed to remove a specific
    one with delete_task. (recall_tasks, by contrast, returns the history of
    completed runs, not the active ones with their IDs.)"""
    try:
        body = _mgr_get(_manager_base(), "/api/tasks-open")
        tasks = json.loads(body).get("tasks", [])
        if not tasks:
            return "no running tasks"
        out = []
        for t in tasks:
            sch = f" [{t['schedule']}]" if t.get("schedule") else ""
            out.append(f"{t.get('id')} @{t.get('instance')} ({t.get('status')}){sch}: "
                       f"{str(t.get('message',''))[:80]}")
        return "\n".join(out)
    except Exception as e:
        return f"Error: {e!r}"


def t_delete_task(id):
    """Remove a running/scheduled task by ID. The ID comes from
    list_tasks. Final; it does not abort a task that is currently running,
    but prevents future runs."""
    try:
        body = _mgr(_manager_base(), "/api/task-delete", {"id": str(id)})
        d = json.loads(body)
        return (f"Task {id} deleted." if d.get("deleted")
                else f"No task with ID {id} found.")
    except Exception as e:
        return f"Error: {e!r}"


def t_edit_task(id, message="", schedule=""):
    """Change the message and/or schedule of a task (ID from list_tasks).
    schedule e.g. 'every 2h', 'daily 08:00', 'hourly'; an empty schedule turns
    a recurring task into a one-off. Empty fields stay
    unchanged. A task that is currently RUNNING cannot be changed."""
    try:
        payload = {"id": str(id)}
        if message:
            payload["message"] = message
        if schedule is not None:
            payload["schedule"] = schedule
        body = _mgr(_manager_base(), "/api/task-edit", payload)
        return str(json.loads(body).get("result", body))
    except Exception as e:
        return f"Error: {e!r}"


def _mgr_get(base, path, timeout=30):
    req = urllib.request.Request(base + path, method="GET")
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")


def t_list_skills(query=""):
    """List available expert skills. Without a query: names only (the catalog
    has ~70 entries; the full descriptions cost ~2.5k tokens per call). With a
    query: name + description of the matching ones."""
    try:
        arr = json.loads(_mgr_get(_manager_base(), "/api/skills?meta=1"))
    except Exception as e:
        return f"Error: {e!r}"
    if not arr:
        return "No skills available."
    q = (query or "").strip().lower()
    if q:
        hits = [s for s in arr
                if q in s.get("name", "").lower() or q in s.get("description", "").lower()]
        if not hits:
            return f"No skill matches '{query}'. list_skills() shows all names."
        return "\n".join(f"- {s.get('name')}: {s.get('description', '')}" for s in hits)
    names = sorted(s.get("name", "") for s in arr)
    return ("Skills (load with load_skill(name); descriptions via "
            "list_skills(query=…)):\n" + ", ".join(names))


def t_propose_skill(name, description, content):
    """Propose a skill for the catalog: a procedure that worked and will be
    needed again. It waits for the operator's approval in the Skills tab."""
    try:
        return _mgr(_manager_base(), "/api/skill-proposals",
                    {"name": name, "description": description, "content": content,
                     "turn": _turn_id[0], "note": "proposed by the agent"}, timeout=10)
    except Exception as e:
        return f"Error: {e!r}"


def t_search_sessions(query, instance=""):
    """Exact (full-text) search over earlier chats and task results — the
    counterpart of memory_recall's semantic search."""
    try:
        raw = _mgr(_manager_base(), "/api/sessions-search",
                   {"q": query, "instance": instance or "", "limit": 10}, timeout=15)
        hits = json.loads(raw).get("hits", [])
    except Exception as e:
        return f"Error: {e!r}"
    if not hits:
        return f"No earlier session mentions '{query}'."
    out = []
    for h in hits:
        when = time.strftime("%Y-%m-%d", time.localtime(h.get("ts") or 0)) if h.get("ts") else "?"
        out.append(f"- {when} [{h.get('kind')}] {h.get('instance')} · {h.get('title', '')[:60]}: {h.get('snippet', '')}")
    return "\n".join(out)


def t_load_skill(name):
    """Load a skill into the context (returns the knowledge document)."""
    try:
        return _mgr_get(_manager_base(), f"/api/skills/{urllib.parse.quote(str(name), safe='')}")
    except Exception as e:
        return f"Error: {e!r}"


def t_memory_store(key, value):
    """Store a value permanently (centrally in the manager, survives instance deletion)."""
    inst = os.environ.get("FC_INSTANCE", "default")
    try:
        return _mgr(_manager_base(), f"/api/memory/{inst}", {"key": key, "value": value})
    except Exception as e:
        return f"Error: {e!r}"


def t_memory_recall(key=None):
    """Retrieve a stored value (without key: all entries for this instance)."""
    inst = os.environ.get("FC_INSTANCE", "default")
    try:
        # Store takes the key via JSON body — ANY string works there. Recall
        # puts it into the URL path, so it must be quoted, or a key with a
        # space/umlaut can be stored but never retrieved (bit a live agent:
        # "jobsuche Firmen" saved fine, recall exploded).
        tail = f"/{urllib.parse.quote(str(key), safe='')}" if key else ""
        return _mgr_get(_manager_base(), f"/api/memory/{inst}" + tail)
    except Exception as e:
        return f"Error: {e!r}"


def t_playbook_add(rule):
    """Record a permanent rule/procedure (playbook). It will ALWAYS be
    surfaced and followed from now on."""
    try:
        d = json.loads(_mgr(_manager_base(), "/api/playbook-add", {"text": rule}))
        if d.get("added"):
            return "Rule saved."
        return "Rule already exists." if d.get("note") == "exists" else "Not saved."
    except Exception as e:
        return f"Error: {e!r}"


def t_playbooks():
    """Show all fixed rules (playbooks) with IDs."""
    try:
        pbs = json.loads(_mgr_get(_manager_base(), "/api/playbooks")).get("playbooks", [])
        if not pbs:
            return "no playbooks"
        return "\n".join(f"{p['id']}: {p['text']}" for p in pbs)
    except Exception as e:
        return f"Error: {e!r}"


def t_playbook_forget(id):
    """Remove a rule by ID (ID from playbooks)."""
    try:
        d = json.loads(_mgr(_manager_base(), "/api/playbook-remove", {"id": str(id)}))
        return f"Rule {id} removed." if d.get("removed") else f"No rule {id}."
    except Exception as e:
        return f"Error: {e!r}"


def t_list_secrets():
    """Show which secrets this agent may fetch according to the allowlist (names only)."""
    try:
        d = json.loads(_mgr_get(_manager_base(), "/api/secrets"))
        ks = d.get("allowed", [])
        return "Allowed secrets: " + (", ".join(ks) if ks else "(none)")
    except Exception as e:
        return f"Error: {e!r}"


def t_get_secret(name):
    """Fetch an allowed secret from the manager (only when needed; do not log/share)."""
    try:
        d = json.loads(_mgr_get(_manager_base(), f"/api/secret/{name}"))
        return d.get("value", "") if "value" in d else f"⚠️ {d.get('error', 'not allowed')}"
    except urllib.error.HTTPError as e:
        return "⚠️ not allowed" if e.code == 403 else f"Error: HTTP {e.code}"
    except Exception as e:
        return f"Error: {e!r}"


def t_remote_ls(path="."):
    """List the shared remote directory (P2P browser share)."""
    # No longer directly to the katfs node (which is loopback-only since the
    # isolation fix), but through the broker in the manager. It recognizes the
    # instance by its source IP and addresses ONLY its assigned share —
    # the agent can no longer reach someone else's.
    try:
        return _mgr_get(_manager_base(), f"/api/katfs/ls?path={urllib.parse.quote(path)}")
    except Exception as e:
        return f"Error (is the share active?): {e!r}"


def t_remote_read(path):
    """Read a file from the shared remote directory."""
    try:
        return _mgr_get(_manager_base(),
                        f"/api/katfs/read?path={urllib.parse.quote(path)}", timeout=60)
    except Exception as e:
        return f"Error: {e!r}"


def _katfs_post(url, data=b""):
    """POST to the katfs node. On HTTP errors take the body along — that is where
    the actual reason is ({"error": ...}); without it only a bare
    'Internal Server Error' remains, which is useless to both model and human."""
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        return urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        return f"Error HTTP {e.code}: {body or e.reason}"
    except Exception as e:
        return f"Error: {e!r}"


def t_remote_write(path, content):
    """Write a file to the shared remote directory."""
    return _katfs_post(
        _manager_base() + f"/api/katfs/write?path={urllib.parse.quote(path)}",
        (content or "").encode())


def t_remote_delete(path, recursive=False):
    """Delete a file/folder from the shared remote directory."""
    q = f"/api/katfs/delete?path={urllib.parse.quote(path)}"
    if recursive:
        q += "&recursive=1"
    return _katfs_post(_manager_base() + q)


BUILTIN = {
    "bash": (t_bash, "Run a shell command in the workspace",
             {"command": {"type": "string", "description": "command"}}, ["command"]),
    "read_file": (t_read_file, "Read a file",
                  {"path": {"type": "string"}}, ["path"]),
    "write_file": (t_write_file, "Write a file",
                   {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    "list_dir": (t_list_dir, "List a directory",
                 {"path": {"type": "string"}}, []),
    "http_fetch": (t_http_fetch,
                   "Fetch a URL. HTML comes back as readable TEXT with link targets "
                   "in brackets — follow them with another fetch. raw=true for the "
                   "unconverted body.",
                   {"url": {"type": "string"},
                    "method": {"type": "string", "description": "GET (default) or POST"},
                    "raw": {"type": "boolean", "description": "true = raw HTML/body"}},
                   ["url"]),
    "read_pdf": (t_read_pdf, "Extract text from a PDF — path is a workspace file OR an http(s) URL; pages optional as a range (e.g. '1-5').",
                 {"path": {"type": "string", "description": "file in the workspace or http(s) URL"},
                  "pages": {"type": "string", "description": "optional page range, e.g. '1-5'"}}, ["path"]),
    "web_search": (t_web_search,
                   "Web search (Brave Search API via the manager; DuckDuckGo/Bing "
                   "as fallback). Returns title + URL + snippet.",
                   {"query": {"type": "string", "description": "search terms"},
                    "count": {"type": "integer", "description": "results (1-10, default 5)"}},
                   ["query"]),
    "spawn_subagent": (t_spawn_subagent,
                       "Delegate a self-contained subtask to a fresh ephemeral VM and wait for its answer "
                       "(the manager creates and deletes the VM). Optionally pick the subagent's model — "
                       "e.g. a cheap/fast one for grunt work or a strong one for hard reasoning.",
                       {"task": {"type": "string", "description": "task for the subagent (self-contained: it has no memory of this chat)"},
                        "model": {"type": "string", "description": "optional OpenRouter model id for the subagent, e.g. google/gemini-2.5-flash"}}, ["task"]),
    "create_task": (t_create_task,
                    "Queue a task — IMPORTANT: choose target by capability. "
                    "If the task needs a specific MCP/token (e.g. Home Assistant), "
                    "use the matching instance as target (e.g. 'hass'). For general/"
                    "isolated work use 'ephemeral' (fresh VM, deleted afterwards). schedule "
                    "optional ('every 2h','daily 08:00','hourly'). wait=true waits for the "
                    "result, otherwise it runs in the background and appears in the chat.",
                    {"task": {"type": "string", "description": "what should be done"},
                     "target": {"type": "string", "description": "instance name (capable) or 'ephemeral'"},
                     "schedule": {"type": "string", "description": "optional: every Nm|Nh|Nd, daily HH:MM, hourly"},
                     "wait": {"type": "boolean", "description": "wait for the result (default false)"},
                     "model": {"type": "string", "description": "optional OpenRouter model for an ephemeral target"}}, ["task"]),
    "mission_start": (t_mission_start,
                      "Create a multi-stage assignment as a mission (goal + steps). For anything "
                      "that needs several tasks/days — the progress survives restarts.",
                      {"goal": {"type": "string", "description": "goal of the mission"},
                       "steps": {"type": "array", "items": {"type": "string"},
                                 "description": "planned steps in order"}},
                      ["goal", "steps"]),
    "missions": (t_missions, "List open missions with steps/status.", {}, []),
    "mission_update": (t_mission_update,
                       "Advance a mission step: set status (doing/done/failed), "
                       "record result + task_id AND the target instance of the kicked-off "
                       "task, add_step appends a step.",
                       {"id": {"type": "string", "description": "mission ID"},
                        "step": {"type": "integer", "description": "step number"},
                        "status": {"type": "string", "description": "open|doing|done|failed"},
                        "result": {"type": "string", "description": "short result"},
                        "task_id": {"type": "string", "description": "ID of the create_task task"},
                        "add_step": {"type": "string", "description": "append a new step"},
                        "note": {"type": "string", "description": "log note only"},
                        "target": {"type": "string",
                                   "description": "instance the step was delegated to "
                                                  "(create_task target)"}}, ["id"]),
    "mission_finish": (t_mission_finish,
                       "Finish a mission; failed=true on failure. Provide a short conclusion.",
                       {"id": {"type": "string"}, "summary": {"type": "string"},
                        "failed": {"type": "boolean"}}, ["id", "summary"]),
    "oracle": (t_oracle,
               "Second opinion BEFORE a risky/irreversible action: challenges your "
               "assumptions, never acts itself. plan = what you intend and why; kontext = "
               "relevant facts (IDs, wordings, user assignment). On 'OBJECTION' do not "
               "act, but resolve it or ask back.",
               {"plan": {"type": "string", "description": "planned action + reasoning"},
                "kontext": {"type": "string", "description": "facts: IDs, wordings, assignment"}},
               ["plan"]),
    "ha_control": (t_ha_control,
                   "Turn a Home Assistant device OR whole room on/off by the SPOKEN name "
                   "(manager matches real entities/areas server-side and auto-learns the "
                   "alias on a fuzzy hit). Prefer this over raw HA intents for voice control: "
                   "pass the heard target verbatim and action on/off. Handles rooms too "
                   "('Licht im Gartenhaus').",
                   {"spoken": {"type": "string", "description": "the spoken target, e.g. 'Gartenhaus denke rechts' or 'Licht im Gartenhaus'"},
                    "action": {"type": "string", "description": "'on' or 'off'"}},
                   ["spoken", "action"]),
    "ha_learn_alias": (t_ha_learn_alias,
                       "Teach Home Assistant a spoken-name alias for an entity so the same "
                       "misheard wording matches natively next time (STT hears 'Decke' as "
                       "'denke'). Call it after recovering from a failed HA intent, with the "
                       "words you originally heard and the real entity id.",
                       {"spoken": {"type": "string", "description": "the spoken/misheard name, e.g. 'Gartenhaus denke rechts'"},
                        "entity": {"type": "string", "description": "real entity id, e.g. 'light.gartenhaus_decke_rechts'"}},
                       ["spoken", "entity"]),
    "notify": (t_notify,
               "Push notification to the user's devices (app system notification + "
               "web-manager bell). For important events/results when they are not in the "
               "chat. Unlike send_signal this is the app/web channel, does not ring "
               "in Signal.",
               {"title": {"type": "string", "description": "short title"},
                "message": {"type": "string", "description": "text of the notification"}},
               ["title"]),
    "send_signal": (t_send_signal,
                    "Send the user a Signal message — for results, findings "
                    "or questions when they are not currently in the chat. Do NOT use for the "
                    "normal reply in an ongoing conversation (that arrives anyway) "
                    "and not repeatedly unprompted: a message rings on a "
                    "phone. Recipients only from the allowed list; leaving 'to' empty "
                    "means: to the default recipient.",
                    {"text": {"type": "string", "description": "message text"},
                     "to": {"type": "string", "description": "optional: number in the format +49…"}},
                    ["text"]),
    "read_inbox": (t_read_inbox,
                  "Read new user messages (Signal/app/web) since the last run — "
                  "the orchestrator's inbox. Each message comes only once (watermark); "
                  "peek=true to preview without consuming.",
                  {"peek": {"type": "boolean", "description": "only look, do not consume"}}, []),
    "list_agents": (t_list_agents,
                    "List available agent instances + capabilities (model/MCP). "
                    "For routing: choose the create_task target by capability.",
                    {}, []),
    "recall_tasks": (t_recall_tasks,
                     "Query previously executed tasks + results (long-term memory). "
                     "Without query the most recent, with query search specifically. Use BEFORE create_task "
                     "to check whether something is already done/scheduled (no duplicates).",
                     {"query": {"type": "string", "description": "search term (empty = most recent)"},
                      "limit": {"type": "integer", "description": "max hits (default 10)"}}, []),
    "list_tasks": (t_list_tasks,
                   "List RUNNING/scheduled tasks with IDs — for targeted deletion. "
                   "(recall_tasks, by contrast, is the history of completed runs.)", {}, []),
    "delete_task": (t_delete_task,
                    "Delete a running/scheduled task by ID. Get the ID first with "
                    "list_tasks. Final.",
                    {"id": {"type": "string", "description": "task ID from list_tasks"}}, ["id"]),
    "edit_task": (t_edit_task,
                  "Change the message and/or schedule of a task (ID from list_tasks). "
                  "schedule e.g. 'every 2h', 'daily 08:00', 'hourly'; empty = one-off.",
                  {"id": {"type": "string", "description": "task ID from list_tasks"},
                   "message": {"type": "string", "description": "new text (empty = unchanged)"},
                   "schedule": {"type": "string", "description": "new schedule (empty = one-off/unchanged)"}},
                  ["id"]),
    "search_sessions": (t_search_sessions,
                        "Full-text search over earlier chats and task results (exact words, "
                        "newest and best matches first). Use memory_recall for meaning, "
                        "this for names, numbers, URLs you remember seeing.",
                        {"query": {"type": "string", "description": "words to look for"},
                         "instance": {"type": "string", "description": "optional: another instance (orchestrator only)"}},
                        ["query"]),
    "propose_skill": (t_propose_skill,
                      "Propose a reusable procedure as a skill for the catalog (after a "
                      "non-trivial task that worked, or after the user corrected your approach). "
                      "The operator approves it in the Skills tab.",
                      {"name": {"type": "string", "description": "kebab-case name"},
                       "description": {"type": "string", "description": "one line: what it is for"},
                       "content": {"type": "string", "description": "Markdown: purpose, when to use, exact steps and tools, pitfalls; no secrets"}},
                      ["name", "description", "content"]),
    "list_skills": (t_list_skills,
                    "List available expert skills. Without arguments: names only. "
                    "query='…' searches names AND descriptions. Before specialized "
                    "tasks, check whether a matching skill exists.",
                    {"query": {"type": "string",
                               "description": "optional: filter, e.g. 'docker' or 'security'"}},
                    []),

    "load_skill": (t_load_skill, "Load an expert skill (knowledge document) into the context and follow it.",
                   {"name": {"type": "string", "description": "skill name from list_skills"}}, ["name"]),
    "memory_store": (t_memory_store, "Store a value permanently (survives restart/instance deletion).",
                     {"key": {"type": "string"}, "value": {"type": "string"}}, ["key", "value"]),
    "memory_recall": (t_memory_recall, "Retrieve a stored value; without key all entries.",
                      {"key": {"type": "string"}}, []),
    "playbook_add": (t_playbook_add,
                     "Record a permanent rule/procedure — applies ALWAYS from now on. "
                     "Use this when the user tells you HOW something is to be done, states a "
                     "lasting preference or corrects you.",
                     {"rule": {"type": "string", "description": "the rule as a short, concrete sentence"}}, ["rule"]),
    "playbooks": (t_playbooks, "Show all fixed rules (playbooks) with IDs.", {}, []),
    "playbook_forget": (t_playbook_forget, "Remove a rule by ID (ID from playbooks).",
                        {"id": {"type": "string", "description": "playbook ID"}}, ["id"]),
    "remote_ls": (t_remote_ls,
                  "List the folder the user has shared (lives on THEIR machine, "
                  "connected via P2P). Paths are relative to the root of the share.",
                  {"path": {"type": "string", "description": "relative, default '.'"}}, []),
    "remote_read": (t_remote_read,
                    "Read a file from the user's shared folder (path relative to the share).",
                    {"path": {"type": "string"}}, ["path"]),
    "remote_write": (t_remote_write,
                     "Write a file to the user's shared folder — CREATES and "
                     "OVERWRITES, missing subfolders are created automatically. Write access "
                     "is explicitly allowed: when the user wants to put, save or "
                     "change something there, CALL THIS TOOL instead of claiming you cannot "
                     "write. Only if it returns an error is it not possible.",
                     {"path": {"type": "string", "description": "relative to the share, e.g. 'note.txt'"},
                      "content": {"type": "string", "description": "complete new file content"}},
                     ["path", "content"]),
    "remote_delete": (t_remote_delete,
                      "Delete a file or folder in the user's shared folder. "
                      "Irreversible — there is no trash. Only delete when the user "
                      "requests it, and ask first when in doubt. A non-empty folder "
                      "fails on purpose; set recursive=true for that.",
                      {"path": {"type": "string", "description": "relative to the share"},
                       "recursive": {"type": "boolean",
                                     "description": "delete the folder including its contents (default false)"}},
                      ["path"]),
    "list_secrets": (t_list_secrets, "Show the secret names released for this agent (no values).",
                     {}, []),
    "get_secret": (t_get_secret, "Fetch a released secret (e.g. API key/token) only when needed. Never output values in replies/logs.",
                   {"name": {"type": "string"}}, ["name"]),
}


# Optional per-instance tool allowlist (AGENT_TOOLS, comma-separated). Empty =
# all. Filters both the schema reported to the model AND the
# execution — otherwise a model could call a disabled tool anyway.
# MCP tools are unaffected by this (those are controlled by MCP_SERVERS/policy).
_TOOL_ALLOW = {t.strip() for t in os.environ.get("AGENT_TOOLS", "").split(",") if t.strip()}


# Task administration only where the manager has set TASK_ADMIN (orchestrator).
# The MISSION tools are deliberately NOT in here: every agent may plan its own
# mission and delegate the steps to capable instances (create_task target). Each
# agent only ever sees and writes its own missions — the manager keys them by
# the calling instance.
_TASK_ADMIN_TOOLS = {"list_tasks", "delete_task", "edit_task"}


def tool_enabled(name):
    if name == "offload_read":
        return True   # system helper: must always be available, otherwise a reference dangles
    if name == "spawn_subagent" and os.environ.get("NO_SPAWN"):
        return False
    if name in _TASK_ADMIN_TOOLS and not os.environ.get("TASK_ADMIN"):
        return False
    return (not _TOOL_ALLOW) or name in _TOOL_ALLOW


def builtin_schema():
    out = []
    for name, (_fn, desc, props, req) in BUILTIN.items():
        if not tool_enabled(name):
            continue
        out.append({"type": "function", "function": {
            "name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": req}}})
    return out


# --- MCP (stdio) ------------------------------------------------------------
class MCP:
    def __init__(self, name, argv, env=None):
        self.name = name
        proc_env = {**os.environ, **(env or {})}
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, bufsize=1, env=proc_env)
        self._id = 0
        self._rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "or-agent", "version": "1"}})
        self._notify("notifications/initialized")

    def _send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def _notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _rpc(self, method, params):
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        for _ in range(10000):
            line = self.proc.stdout.readline()
            if not line:
                return {}
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == self._id:
                return msg.get("result", {})
        return {}

    def tools(self):
        return self._rpc("tools/list", {}).get("tools", [])

    def call(self, tool, args):
        r = self._rpc("tools/call", {"name": tool, "arguments": args})
        parts = [c.get("text", "") for c in r.get("content", []) if c.get("type") == "text"]
        return "\n".join(parts) or json.dumps(r)[:MAX_TOOL_OUT]


class HubMCP:
    """MCP via the manager instead of as its own process in the VM.

    The server process runs in the MCP hub on the host; here only JSON-RPC
    goes out via /api/mcp. This way the guest needs neither the tokens (the
    manager inserts them) nor LAN access (the hub opens the connection to the
    target system). Same interface as MCP: tools() and call()."""

    def __init__(self, name):
        self.name = name
        self._id = 0
        self._rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "or-agent", "version": "1"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def _send(self, payload):
        body = json.dumps({"server": self.name, "payload": payload})
        req = urllib.request.Request(_manager_base() + "/api/mcp", data=body.encode(),
                                     headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=120).read() or b"{}")

    def _rpc(self, method, params):
        self._id += 1
        out = self._send({"jsonrpc": "2.0", "id": self._id,
                          "method": method, "params": params})
        if out.get("error"):
            raise RuntimeError(str(out["error"])[:300])
        return out.get("result", {})

    def tools(self):
        return self._rpc("tools/list", {}).get("tools", [])

    def call(self, tool, args):
        r = self._rpc("tools/call", {"name": tool, "arguments": args})
        parts = [c.get("text", "") for c in r.get("content", []) if c.get("type") == "text"]
        return "\n".join(parts) or json.dumps(r)[:MAX_TOOL_OUT]


_mcp = {}      # server-name -> MCP
_mcp_tools = {}  # exposed-tool-name -> (server-name, mcp-tool-name)


def init_mcp():
    # MCP_CONFIG no longer lives in the instance config — it carried the tokens in
    # plaintext. The manager assembles it at runtime from MCP_SERVERS and
    # inserts only the secrets that this instance's policy allows.
    cfg = os.environ.get("MCP_CONFIG", "")
    if not cfg:
        try:
            body = _mgr_get(_manager_base(), "/api/mcp-config")
            d = json.loads(body)
            if d.get("unresolved"):
                log("MCP: secrets not released, server may start without access:",
                    ", ".join(d["unresolved"]))
            if d.get("mcpServers"):
                cfg = json.dumps(d)
        except Exception as e:
            log("MCP configuration could not be obtained from the manager:", repr(e))
    if not cfg:
        p = os.path.join(WORKDIR, ".mcp.json")
        if os.path.exists(p):
            cfg = open(p).read()
    if not cfg:
        return []
    try:
        servers = json.loads(cfg).get("mcpServers", json.loads(cfg))
    except Exception as e:
        log("MCP config malformed:", e)
        return []
    schema = []
    for name, spec in servers.items():
        argv = [spec["command"], *spec.get("args", [])] if isinstance(spec, dict) else None
        if not argv:
            continue
        env = spec.get("env") if isinstance(spec, dict) else None
        try:
            # Hub first: the process runs on the host, the guest needs neither
            # argv nor env nor secrets. The own-process path stays a fallback
            # for managers without /api/mcp (older versions).
            try:
                srv = HubMCP(name)
            except Exception as hub_err:
                log(f"MCP '{name}': hub unreachable ({hub_err!r:.120}), starting locally")
                srv = MCP(name, argv, env={str(k): str(v) for k, v in (env or {}).items()})
            _mcp[name] = srv
            for t in srv.tools():
                fq = f"{name}__{t['name']}"[:64]
                _mcp_tools[fq] = (name, t["name"])
                schema.append({"type": "function", "function": {
                    "name": fq, "description": (t.get("description") or fq)[:400],
                    "parameters": t.get("inputSchema") or {"type": "object", "properties": {}}}})
            log(f"MCP '{name}': {len(srv.tools())} tools")
        except Exception as e:
            log(f"MCP '{name}' start failed:", repr(e))
    return schema


def _audit_target(name, args):
    """The most meaningful field per tool for the audit log — never a secret value.
    For get_secret only the name, for write NOT the content."""
    a = args or {}
    if name in ("http_fetch",):
        return a.get("url", "")
    if name == "web_search":
        return a.get("query", "")
    if name in ("read_file", "write_file", "list_dir", "read_pdf",
                "remote_ls", "remote_read", "remote_write", "remote_delete"):
        return a.get("path", "")
    if name == "bash":
        return (a.get("command", "") or "")[:200]
    if name in ("get_secret", "load_skill", "memory_store", "memory_recall", "recall_tasks", "read_inbox"):
        return a.get("name", "") or a.get("key", "") or a.get("query", "")
    if name == "spawn_subagent":
        return (a.get("task", "") or "")[:120]
    if name == "create_task":
        return (a.get("target","") + ": " + (a.get("task","") or ""))[:160]
    return ""


# One id per user turn: lets a trace reviewer group the tool calls of a turn
# and line them up with the chat log. Set in run()/run_stream().
_turn_id = [""]


def audit(name, args, ok=True, err="", result="", ms=None):
    """Log a tool call at the manager (per instance, on the host — survives VM
    restarts). Best-effort: if the broker fails, the agent continues normally.
    Carries tool, target (URL/path/query), ok, a short ERROR TEXT and a short
    RESULT excerpt — without those a reviewer cannot tell a healthy call from
    one that failed politely (the audit used to say ok:true while a tool
    returned "⚠️ blocked"). NEVER secret values or full file contents."""
    try:
        rec = {"tool": name, "target": _audit_target(name, args), "ok": bool(ok),
               "err": str(err)[:300], "result": str(result)[:300],
               "turn": _turn_id[0]}
        if ms is not None:
            rec["ms"] = int(ms)
        _mgr(_manager_base(), "/api/audit", rec, timeout=5)
    except Exception:
        pass


# ---- traces: one turn = one span tree (turn -> LLM calls -> tool calls) -----
# The manager stitches them together from three feeds it already had: the
# audit (tool calls), the usage (LLM calls) and — new — a turn marker. Each
# record carries the turn id and its duration; nothing else changes.
_turn_step = [0]        # LLM calls so far in this turn (the span index)
_turn_t0 = [0.0]        # monotonic start of the turn
_turn_kind = ["chat"]


def trace_turn(event, **kw):
    """POST /api/trace {turn, event:start|end, kind, steps, ms, outcome}.
    Best-effort like audit(): the manager being away must not touch a turn."""
    try:
        _mgr(_manager_base(), "/api/trace",
             {"turn": _turn_id[0], "event": event, "kind": _turn_kind[0], **kw}, timeout=5)
    except Exception:
        pass


def _trace_begin(kind):
    _turn_kind[0] = kind
    _turn_step[0] = 0
    _turn_t0[0] = time.monotonic()
    trace_turn("start")


def _trace_end(outcome):
    trace_turn("end", steps=_turn_step[0], ms=int((time.monotonic() - _turn_t0[0]) * 1000),
               outcome=outcome)


# ---- skills from experience -------------------------------------------------
# Hermes' loop, with the manager's approval gate: after a long successful
# turn one extra model call distills the way it went into a SKILL proposal
# and files it; nothing enters the catalog without the operator's click.
SKILL_LEARN = os.environ.get("SKILL_LEARN", "1") not in ("0", "false", "False", "")
SKILL_LEARN_MIN_STEPS = int(os.environ.get("SKILL_LEARN_MIN_STEPS", "5"))
_LEARN_SYSTEM = (
    "You just completed a multi-step task (the conversation follows). Decide whether "
    "the approach is a REUSABLE procedure worth saving as a skill for future tasks of "
    "the same kind. Answer exactly NONE when it was a one-off, trivial, mostly failed, "
    "personal, or already covered by a skill you loaded. Otherwise answer with one JSON "
    "object and nothing else: {\"name\": kebab-case, \"description\": one line, "
    "\"content\": Markdown with sections Purpose, When to use, Steps (the exact tools "
    "and arguments that worked, in order), Pitfalls}. No secrets, no personal data, no "
    "full tool outputs, under 4000 characters.")


def _turn_slice(hist, user_text):
    """The messages of the turn that just ended: from the last user message
    with that text to the end, tool outputs trimmed, images dropped."""
    start = 0
    for i in range(len(hist) - 1, -1, -1):
        m = hist[i]
        if m.get("role") == "user" and (m.get("content") == user_text or isinstance(m.get("content"), list)):
            start = i
            break
    out = []
    for m in hist[start:]:
        c = m.get("content")
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
        entry = {"role": m.get("role"), "content": (str(c) if c is not None else "")[:1500]}
        if m.get("tool_calls"):
            entry["tool_calls"] = m["tool_calls"]
        if m.get("tool_call_id"):
            entry["tool_call_id"] = m["tool_call_id"]
        out.append(entry)
    return out


def _learn_skill(turn_msgs, user_text):
    """One model call, no tools; posts the proposal or does nothing."""
    msgs = [{"role": "system", "content": _LEARN_SYSTEM}] + turn_msgs + \
           [{"role": "user", "content": "Distill now: NONE or the JSON object."}]
    try:
        reply = or_chat(msgs, []).get("content") or ""
    except Exception:
        return None
    t = reply.strip().strip("`")
    if t.lower().startswith("json"):
        t = t[4:].strip()
    if not t or t.upper().startswith("NONE"):
        return None
    try:
        d = json.loads(t[t.index("{"):t.rindex("}") + 1])
    except (ValueError, TypeError):
        return None
    if not all(isinstance(d.get(k), str) for k in ("name", "description", "content")):
        return None
    try:
        return _mgr(_manager_base(), "/api/skill-proposals",
                    {"name": d["name"], "description": d["description"], "content": d["content"],
                     "turn": _turn_id[0], "note": f"distilled after: {str(user_text)[:120]}"}, timeout=10)
    except Exception:
        return None


def _maybe_learn(hist, user_text, outcome):
    """Fire the distillation in the background when the turn qualifies:
    enabled, ended well, at least SKILL_LEARN_MIN_STEPS model calls."""
    if not SKILL_LEARN or outcome != "ok" or _turn_step[0] < SKILL_LEARN_MIN_STEPS:
        return False
    if str(user_text).startswith("/"):
        return False
    slice_ = _turn_slice(hist, user_text)
    threading.Thread(target=_learn_skill, args=(slice_, user_text), daemon=True).start()
    return True


def _outcome_of(text):
    t = str(text or "")
    if "(max tool steps reached)" in t:
        return "max_steps"
    if "time budget exhausted" in t:
        return "deadline"
    if t.lstrip().startswith("⚠️"):
        return "error"
    return "ok"


# Result strings that mean "the tool ran but the CALL failed" — tools report
# errors as text, not exceptions, so the audit has to look at the words.
_ERR_PREFIXES = ("⚠️", "Error:", "Tool error", "error:")


def _looks_failed(out):
    t = str(out).lstrip()
    return t.startswith(_ERR_PREFIXES) or "web search unavailable" in t[:120]


def _resolve_tool_name(name):
    """Models drop the MCP prefix now and then — 'mrmusic_power' for
    'mrmusic__mrmusic_power' (gemini-2.5-flash, 2026-09-07, 'unknown tool'
    twice while the user waited for the radio). A bare name that matches
    exactly ONE registered MCP tool is accepted; ambiguity stays unknown."""
    if name in BUILTIN or name in _mcp_tools:
        return name
    hits = [fq for fq, (_srv, tool) in _mcp_tools.items()
            if tool == name or fq.endswith("__" + name)]
    if len(hits) == 1:
        log(f"tool name '{name}' resolved to '{hits[0]}'")
        return hits[0]
    return name


def _tools_report():
    """'/tools' — the registry as the model sees it, without a model call.
    Built-in tools that are enabled here, then every MCP tool with its full
    name. A smoke test after a rootfs rebuild reads exactly this."""
    builtin = sorted(n for n in BUILTIN if tool_enabled(n))
    mcp = sorted(_mcp_tools)
    out = [f"built-in ({len(builtin)}): " + ", ".join(builtin)]
    if mcp:
        out.append(f"mcp ({len(mcp)}): " + ", ".join(mcp))
    else:
        out.append("mcp (0): none registered")
    return "\n".join(out)


def exec_tool(name, args):
    name = _resolve_tool_name(name)
    t0 = time.monotonic()

    def _audit(**kw):        # every exit books the call with its duration
        audit(name, args, ms=int((time.monotonic() - t0) * 1000), **kw)
    # Hook/intervention: denylist + optional HITL approval BEFORE execution.
    allow, reason = _hook_before_tool(name, args)
    if not allow:
        _audit(ok=False)
        return f"Tool '{name}' not executed: {reason}"
    try:
        if name in BUILTIN:
            if not tool_enabled(name):
                _audit(ok=False, err="tool not enabled")
                return f"Tool '{name}' is not enabled for this instance."
            out = str(BUILTIN[name][0](**args))
        elif name in _mcp_tools:
            srv, tool = _mcp_tools[name]
            out = str(_mcp[srv].call(tool, args))
        else:
            _audit(ok=False, err="unknown tool")
            return f"unknown tool: {name}"
    except Exception as e:
        _audit(ok=False, err=repr(e))
        return f"Tool error ({name}): {e!r}"
    failed = _looks_failed(out)
    _audit(ok=not failed, err=out[:300] if failed else "", result="" if failed else out[:200])
    return _finalize_output(name, out)


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
            "model": OR_MODEL,
            "prompt_tokens": u.get("prompt_tokens") or 0,
            "completion_tokens": u.get("completion_tokens") or 0,
            "cost": u.get("cost") or 0.0,
            "turn": _turn_id[0], "step": _turn_step[0], "ms": ms,
            "ok": bool(ok), "err": str(err or "")[:400],
            "direct": bool(LLAMA_ENDPOINT),     # a local model is called directly, not through the key proxy
        }).encode()
        req = urllib.request.Request(f"{_manager_base()}/api/usage", data=payload,
                                     method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass


# ===== Harness patterns (inspired by strands-agents/harness-sdk, Apache-2.0) ====
# Four building blocks, all stdlib, without a new dependency:
#  1) Retry with backoff around the model call
#  2) SUMMARIZE context instead of discarding it (summarizing conversation manager)
#  3) OFFLOAD large tool outputs instead of hard-truncating (context offloader)
#  4) GOAL loop with judge (goal loop) + tool HOOK (interventions/HITL)

LLM_RETRIES = int(os.environ.get("LLM_RETRIES", "3"))
# Socket timeout of one LLM call (also between stream chunks): a local model
# on a CPU may chew on a 6k-token prompt or an image for minutes before the
# first byte, so the llama backend gets ten minutes (the manager's stream
# timeout is 620 s); cloud backends keep the short values.
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "600" if LLAMA_ENDPOINT else "120"))
LLM_STREAM_TIMEOUT = int(os.environ.get("LLM_STREAM_TIMEOUT", str(max(LLM_TIMEOUT, 180))))


def _conn_dropped(e):
    """The server closed the connection without an answer — for llama.cpp
    typically a crash (seen: every image input killed the server, which then
    reloaded the model for a while)."""
    t = str(e).lower()
    return isinstance(e, (ConnectionResetError, BrokenPipeError)) or \
        "closed connection" in t or "connection reset" in t or "remotedisconnected" in t


def _llama_dropped_msg(messages):
    """No retry against a crashed local server: it is reloading. If the
    request carried an image, that was the trigger — the images leave the
    history so the next turn does not kill the server again."""
    n = _strip_history_images(messages)
    if n:
        return ("⚠️ the local model server dropped the connection while processing an image "
                "(it crashes on image input) — the image was removed from the history, ask again without one")
    return "⚠️ the local model server dropped the connection — it is probably restarting; try again in a minute"


_LOADING_MSG = "⚠️ the local model server is loading its model — try again in a minute"


def _is_timeout(e):
    return isinstance(e, (TimeoutError, socket.timeout)) or "timed out" in str(e).lower()


def _retry_after(e, attempt):
    """A retry after a timeout is only worth it against a cloud backend: a
    local model is still busy with the request that timed out, a second one
    would just queue behind it."""
    if attempt >= LLM_RETRIES:
        return False
    return not (LLAMA_ENDPOINT and _is_timeout(e))
_RETRY_CODES = {408, 409, 429, 500, 502, 503, 504}


def _retry_sleep(attempt):
    # 0.5s, 1s, 2s, 4s … capped at 8s.
    time.sleep(min(8.0, 0.5 * (2 ** attempt)))


# --- 2) context summarization ----------------------------------------------
SUMMARY_TAG = "[Summary]"
CTX_SUMMARY = os.environ.get("CTX_SUMMARY", "1") != "0"
CTX_PRESERVE_RECENT = int(os.environ.get("CTX_PRESERVE_RECENT", "10"))
SUMMARIZE_PROMPT = (
    "You summarize a conversation history. Produce a concise, structured "
    "summary in bullet points. Do NOT answer conversationally and do NOT "
    "address the user. Include: topics and questions covered; important tool calls "
    "and their results; facts, data and code that were shared; open points; key "
    "insights. Write in the third person. Do not assume that tools "
    "failed unless explicitly stated.")


def _msg_text(m):
    c = m.get("content")
    if isinstance(c, list):   # vision content -> only the text parts
        c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return c or ""


def _summarize(msgs, prior=""):
    """Condense a message list (conversation, without system blocks) into a short
    bullet-point summary. If the call fails -> '' (the caller then does
    the old discard behavior)."""
    lines = []
    for m in msgs:
        role = m.get("role")
        txt = _msg_text(m)
        if role == "tool":
            lines.append(f"[Tool result] {txt[:1500]}")
        elif role == "assistant":
            tcs = m.get("tool_calls")
            if tcs:
                names = ", ".join(t.get("function", {}).get("name", "?") for t in tcs)
                lines.append(f"[Assistant called tools: {names}] {txt[:800]}")
            else:
                lines.append(f"[Assistant] {txt[:1500]}")
        elif role == "user":
            lines.append(f"[User] {txt[:1500]}")
    joined = "\n".join(lines)
    if prior:
        joined = f"Prior summary:\n{prior}\n\nNew messages:\n{joined}"
    msg = or_chat([{"role": "system", "content": SUMMARIZE_PROMPT},
                   {"role": "user", "content": joined}], [])
    out = (msg.get("content") or "").strip()
    return "" if out.startswith("⚠") else out   # an error message does not count


# --- 3) Context-Offloader ---------------------------------------------------
OFFLOAD_DIR = os.path.join(WORKDIR, ".offload")
OFFLOAD_MIN = int(os.environ.get("OFFLOAD_MIN", str(MAX_TOOL_OUT)))
OFFLOAD_PREVIEW = int(os.environ.get("OFFLOAD_PREVIEW", "2000"))
_offload_seq = 0


# Type-aware previews (idea from Caveman's per-type compressors, done in ~60
# lines of stdlib instead of adopting the BSL-licensed engine): the preview an
# agent sees for an offloaded output should carry STRUCTURE, not just the first
# N characters. A head-slice of a 40k JSON is usually an unclosed brace of the
# first record; an outline of keys, types and counts tells the model what it is
# holding and where to read on. Nothing is lost either way — the full text
# stays in the offload file.

def _preview_json(out, budget):
    """Outline of a JSON payload: shape, keys, counts, first items."""
    data = json.loads(out)      # caller catches
    lines = []

    def walk(node, path, depth):
        if len(lines) > 60 or depth > 3:
            return
        if isinstance(node, dict):
            lines.append(f"{path or '$'}: object, {len(node)} keys: "
                         + ", ".join(list(node.keys())[:12])
                         + (" …" if len(node) > 12 else ""))
            for k in list(node.keys())[:6]:
                v = node[k]
                if isinstance(v, (dict, list)):
                    walk(v, f"{path}.{k}" if path else k, depth + 1)
        elif isinstance(node, list):
            lines.append(f"{path or '$'}: array, {len(node)} items")
            if node and isinstance(node[0], (dict, list)):
                walk(node[0], (path or "$") + "[0]", depth + 1)
            elif node:
                sample = json.dumps(node[:3], ensure_ascii=False)
                lines.append(f"{path or '$'}[0..2]: {sample[:200]}")
        else:
            lines.append(f"{path or '$'}: {json.dumps(node, ensure_ascii=False)[:120]}")

    walk(data, "", 0)
    head = json.dumps(data, ensure_ascii=False)[:budget // 3]
    return ("[JSON structure]\n" + "\n".join(lines))[:budget - len(head) - 20] \
        + "\n\n[begins] " + head


def _preview_log(out, budget):
    """Head + tail + everything that smells like a problem, duplicates folded."""
    lines = out.splitlines()
    folded, last, count = [], None, 0
    for ln in lines:
        if ln == last:
            count += 1
            continue
        if count > 1:
            folded.append(f"  [previous line repeats ×{count}]")
        folded.append(ln)
        last, count = ln, 1
    if count > 1:
        folded.append(f"  [previous line repeats ×{count}]")
    interesting = [ln for ln in folded
                   if re.search(r"error|warn|fail|exception|traceback|fatal|denied",
                                ln, re.I)]
    head = folded[:15]
    tail = folded[-10:] if len(folded) > 25 else []
    mid = [ln for ln in interesting if ln not in head and ln not in tail][:20]
    parts = head + (["  […]"] if mid or tail else []) + mid \
        + (["  […]"] if tail and mid else []) + tail
    return (f"[log, {len(lines)} lines, duplicates folded]\n"
            + "\n".join(parts))[:budget]


def _smart_preview(out, budget):
    """Pick a preview by payload type; plain head-slice as the fallback."""
    stripped = out.lstrip()
    if stripped[:1] in "[{":
        try:
            return _preview_json(out, budget)
        except Exception:
            pass
    lines = out.count("\n")
    if lines >= 30 and len(out) / max(lines, 1) < 400:
        try:
            return _preview_log(out, budget)
        except Exception:
            pass
    return out[:budget]


def _finalize_output(name, out):
    """If a tool output is larger than OFFLOAD_MIN, it is offloaded to a file IN
    FULL and only a preview + reference is kept in the context (offload_read
    fetches the rest). This way nothing is lost without flooding the context.
    Smaller -> unchanged."""
    out = out if isinstance(out, str) else str(out)
    if len(out) <= OFFLOAD_MIN:
        return out
    global _offload_seq
    _offload_seq += 1
    oid = f"{name}-{_offload_seq}-{uuid.uuid4().hex[:6]}"
    try:
        os.makedirs(OFFLOAD_DIR, exist_ok=True)
        with open(os.path.join(OFFLOAD_DIR, oid + ".txt"), "w") as fh:
            fh.write(out)
    except Exception:
        return out[:MAX_TOOL_OUT]   # offloading failed -> fall back: hard-truncate
    preview = _smart_preview(out, OFFLOAD_PREVIEW)
    return (preview + f"\n\n[… full output offloaded ({len(out)} characters). "
            f"Read verbatim with offload_read(id=\"{oid}\", offset=0).]")


def t_offload_read(id="", offset=0, length=None):
    """Read an offloaded tool output (see the offload reference) in chunks."""
    length = int(length) if length else MAX_TOOL_OUT
    offset = max(0, int(offset or 0))
    safe = os.path.basename(str(id))              # no path traversal
    fp = os.path.join(OFFLOAD_DIR, safe + ".txt")
    try:
        with open(fp) as fh:
            fh.seek(offset)
            data = fh.read(length)
    except FileNotFoundError:
        return f"offload '{id}' not found."
    except Exception as e:
        return f"offload error: {e!r}"
    more = f"\n\n[… continue with offset={offset + len(data)} …]" if len(data) >= length else ""
    return data + more


# Attach offload_read to the tool catalog (only here, because t_offload_read
# is defined after the BUILTIN literal).
BUILTIN["offload_read"] = (
    t_offload_read,
    "Re-read a previously offloaded, truncated tool output in chunks "
    "(the offload reference names id and offset).",
    {"id": {"type": "string", "description": "offload id from the reference"},
     "offset": {"type": "integer", "description": "start position (characters)"},
     "length": {"type": "integer", "description": "max characters (default 8000)"}},
    ["id"])


# --- 4a) goal loop ---------------------------------------------------------
GOAL_MAX_ATTEMPTS = int(os.environ.get("GOAL_MAX_ATTEMPTS", "3"))
_goal = (os.environ.get("AGENT_GOAL", "").strip() or None)
JUDGE_PROMPT = (
    "You are a strict reviewer. Check whether the ANSWER meets the GOAL for the "
    "QUESTION. Answer EXCLUSIVELY with JSON, no other text: "
    '{"meets": true|false, "feedback": "concise reasoning, what is still missing"}.')


def _set_goal(cmd):
    global _goal
    rest = cmd[len("/goal"):].strip()
    if rest in ("", "show", "status"):
        return f"\U0001f3af Goal: {_goal}" if _goal else \
            "No goal set. /goal <criterion> sets one, /goal off removes it."
    if rest in ("off", "clear", "none", "aus"):
        _goal = None
        return "\U0001f3af Goal removed."
    _goal = rest
    return f"\U0001f3af Goal set (max {GOAL_MAX_ATTEMPTS} attempts): {_goal}"


def _judge(goal, question, answer):
    """(meets, feedback). Judge broken/unparseable -> let it pass (True)."""
    try:
        m = or_chat([{"role": "system", "content": JUDGE_PROMPT},
                     {"role": "user", "content": f"GOAL:\n{goal}\n\nQUESTION:\n{question}\n\nANSWER:\n{answer}"}], [])
        raw = (m.get("content") or "").strip()
        d = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        return bool(d.get("meets")), str(d.get("feedback", ""))[:500]
    except Exception:
        return True, ""


def _run_goal(hist, question):
    """Produce an answer and check it against _goal; on non-fulfillment improve it
    with the judge's critique, up to max GOAL_MAX_ATTEMPTS."""
    answer = _tool_loop(hist)
    for _ in range(GOAL_MAX_ATTEMPTS - 1):
        meets, fb = _judge(_goal, question, answer)
        if meets:
            break
        hist.append({"role": "system", "content":
                     f"Your last answer does not yet meet the goal: {_goal}. "
                     f"Critique: {fb}. Improve the answer accordingly."})
        answer = _tool_loop(hist)
    return answer


# --- 4b) tool hook: hard denylist + optional HITL approval ------------------
HITL = os.environ.get("HITL", "") not in ("", "0", "false", "False")
HITL_TOOLS = set(t for t in os.environ.get(
    "HITL_TOOLS", "bash,remote_delete,remote_write,delete_task,edit_task").split(",") if t)
HITL_TIMEOUT = int(os.environ.get("HITL_TIMEOUT", "120"))
# Always active, independent of HITL: obviously destructive bash patterns.
_DENY_PATTERNS = ("rm -rf /", ":(){:|:&};:", "mkfs", "dd if=", "> /dev/sd", "chmod -R 000")


def _request_approval(name, args):
    """Request an approval from the manager (which asks the user via Signal) and
    poll for it. If the manager cannot (old version/no Signal) -> do not
    block (True). Timeout/rejection -> False."""
    try:
        d = json.loads(_mgr(_manager_base(), "/api/hitl",
                            {"tool": name, "target": _audit_target(name, args)}, timeout=8))
        hid = d.get("id")
        if not hid:
            return True
    except Exception:
        return True
    deadline = time.time() + HITL_TIMEOUT
    while time.time() < deadline:
        time.sleep(2)
        try:
            st = json.loads(_mgr_get(_manager_base(), f"/api/hitl/{hid}", timeout=6)).get("status")
        except Exception:
            continue
        if st == "approved":
            return True
        if st == "denied":
            return False
    return False


def _hook_before_tool(name, args):
    """(allow, reason). Denylist first, then optional HITL approval."""
    if name == "bash":
        cmd = str(args.get("command", ""))
        for pat in _DENY_PATTERNS:
            if pat in cmd:
                return False, f"blocked by security rule ({pat})"
    if HITL and name in HITL_TOOLS:
        if not _request_approval(name, args):
            return False, "not approved by the user (or timed out)"
    return True, ""


# --- OpenRouter chat --------------------------------------------------------
FOLD_SYSTEM = os.environ.get("LLM_FOLD_SYSTEM", "1" if LLAMA_ENDPOINT else "0") not in ("0", "false", "False", "")


def _wire_messages(messages):
    """The history carries system notes mid-conversation ([Memory], [Playbooks],
    the date line, a deadline note …). Chat templates of local models (Qwen3
    in llama.cpp: "System message must be at the beginning") reject that, so
    for the llama backend every later system message is folded into the first
    one, in order; the other roles keep their places. LLM_FOLD_SYSTEM=1
    forces it for any backend, 0 disables it."""
    if not FOLD_SYSTEM:
        return messages
    first, extra, rest = None, [], []
    for m in messages:
        if m.get("role") == "system":
            c = str(m.get("content", "") or "")
            if first is None:
                first = dict(m); first["content"] = c
            elif c.strip():
                extra.append(c)
        else:
            rest.append(m)
    if first is None:
        return messages
    if extra:
        first["content"] = "\n\n".join([first["content"]] + extra)
    return [first] + rest


def or_chat(messages, tools, model=None):
    messages = _wire_messages(messages)
    _b = {"model": model or OR_MODEL, "messages": messages, "usage": {"include": True}}
    if tools:                       # do NOT send an empty tools list (400)
        _b["tools"] = tools
        _b["tool_choice"] = "auto"
    if _reasoning:
        _b["reasoning"] = {"effort": _reasoning}
    body = json.dumps(_b).encode()
    last = ""
    _turn_step[0] += 1
    t0 = time.monotonic()
    for attempt in range(LLM_RETRIES + 1):
        req = urllib.request.Request(_llm_url(), data=body, method="POST",
                                     headers=_llm_headers())
        try:
            r = urllib.request.urlopen(req, timeout=LLM_TIMEOUT)
            d = json.loads(r.read().decode())
            report_usage(d.get("usage"), ms=int((time.monotonic() - t0) * 1000))
            return d["choices"][0]["message"]
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", "replace")[:400]
            if e.code == 400 and "image" in err_body.lower() \
                    and _strip_history_images(messages):
                _b["messages"] = messages
                body = json.dumps(_b).encode()
                continue           # images gone -> the turn gets another chance
            last = f"⚠️ {LLM_NAME} HTTP {e.code}: {err_body[:300]}"
            if LLAMA_ENDPOINT and e.code == 503:
                last = _LOADING_MSG
            elif e.code in _RETRY_CODES and attempt < LLM_RETRIES:
                _retry_sleep(attempt); continue
            report_usage({}, ms=int((time.monotonic() - t0) * 1000), ok=False, err=last)
            return {"content": last}
        except Exception as e:
            last = f"⚠️ {LLM_NAME} error: {e!r}"
            if LLAMA_ENDPOINT and _conn_dropped(e):
                last = _llama_dropped_msg(messages)
            elif _retry_after(e, attempt):
                _retry_sleep(attempt); continue
            report_usage({}, ms=int((time.monotonic() - t0) * 1000), ok=False, err=last)
            return {"content": last}
    return {"content": last}


TOOLS = []
_history = [{"role": "system", "content": SYSTEM}]

# Semantic long-term memory: instead of dumping ALL facts into the prompt on the
# first turn (that grows with the memory and costs every turn), the agent
# fetches only the content-nearest notes per question. Short-term is
# _history (this conversation), long-term lives semantically in the manager.
RECALL_TAG = "[Memory]"
RECALL_K = 4
# Threshold for multilingual-e5: relevant hits sit ~0.82+, thematically
# unrelated ones ~0.76. 0.78 separates cleanly. Tunable if too strict/loose.
RECALL_MIN = 0.78


def _recall(user_message):
    """Replace the memory block in _history with the long-term notes matching
    THIS question. Exactly ONE such block remains, fresh each
    turn; /reset clears it too. If the search fails, this turn simply has
    no long-term context — the notes stay stored."""
    _history[:] = [m for m in _history
                   if not (m.get("role") == "system"
                           and str(m.get("content", "")).startswith(RECALL_TAG))]
    try:
        body = _mgr(_manager_base(), "/api/memory-search",
                    {"query": user_message, "k": RECALL_K}, timeout=8)
        hits = [h for h in json.loads(body).get("hits", [])
                if h.get("score", 0) >= RECALL_MIN]
    except Exception:
        hits = []
    if hits:
        block = (RECALL_TAG + " Relevant notes from earlier sessions "
                 "(use them when they fit the question):\n"
                 + "\n".join(f"- {h['text']}" for h in hits))
        _history.append({"role": "system", "content": block})


PLAYBOOK_TAG = "[Playbooks]"


def _inject_playbooks():
    """Surface the fixed rules fresh each turn — unlike _recall, playbooks
    apply ALWAYS. Exactly ONE block, /reset clears it too."""
    _history[:] = [m for m in _history
                   if not (m.get("role") == "system"
                           and str(m.get("content", "")).startswith(PLAYBOOK_TAG))]
    try:
        pbs = json.loads(_mgr_get(_manager_base(), "/api/playbooks", timeout=6)).get("playbooks", [])
    except Exception:
        pbs = []
    if pbs:
        block = (PLAYBOOK_TAG + " Your fixed rules — ALWAYS follow:\n"
                 + "\n".join(f"- {p.get('text','')}" for p in pbs))
        # Appended, not inserted at the top: next to the question the rules
        # are followed; at the top gemini-flash kept saying "12:31 Uhr"
        # against a rule that forbids the "Uhr" (2026-09-08).
        _history.append({"role": "system", "content": block})


# --- prompt templates: /name -> prompt maintained in the manager ------------
# Recurring assignments as a command (pi.dev idea "prompt templates").
# Expansion happens HERE in the agent — so it works in web, app and
# Signal alike. "/daily please keep it short" -> template text + " please keep it short".
_BUILTIN_SLASH = ("/reset", "/fresh", "/reasoning", "/goal", "/model", "/steps",
                  "/aside", "/branch", "/back", "/tools")
_prompts_cache = {"ts": 0.0, "map": {}}


def _prompt_templates():
    if time.time() - _prompts_cache["ts"] > 30:
        try:
            lst = json.loads(_mgr_get(_manager_base(), "/api/prompts", timeout=6)).get("prompts", [])
            _prompts_cache["map"] = {p["name"]: p.get("text", "") for p in lst if p.get("name")}
        except Exception:
            pass                       # keep the old cache
        _prompts_cache["ts"] = time.time()
    return _prompts_cache["map"]


def _expand_prompt(message):
    m = message.strip()
    if not m.startswith("/") or m.startswith(_BUILTIN_SLASH):
        return message
    name, _, rest = m[1:].partition(" ")
    tpl = _prompt_templates().get(name)
    if not tpl:
        return message
    return tpl + ((" " + rest.strip()) if rest.strip() else "")


MISSION_TAG = "[Missions]"


NOW_TAG = "[Now]"
MEMINDEX_TAG = "[MemoryIndex]"
MEMORY_DIR = os.environ.get("MEMORY_DIR", "")


def _inject_memory_index():
    """The head of /memory/MEMORY.md every turn: what notes exist, where the
    timeline is — so the agent greps the folder instead of guessing. One
    block, refreshed each turn; absent when there is no memory folder."""
    _history[:] = [m for m in _history
                   if not (m.get("role") == "system"
                           and str(m.get("content", "")).startswith(MEMINDEX_TAG))]
    if not MEMORY_DIR:
        return
    try:
        with open(os.path.join(MEMORY_DIR, "MEMORY.md"), encoding="utf-8") as fh:
            head = "\n".join(fh.read().split("\n")[:60]).strip()
    except OSError:
        return
    if head:
        _history.append({"role": "system", "content":
                         MEMINDEX_TAG + f" Your long-term memory is the folder {MEMORY_DIR} "
                         "(notes/*.md, timeline/*.md; grep it, read the note you need, "
                         "write or edit notes as Markdown with [[slug]] links). Index:\n" + head})


def _now_line():
    """Current date and time in the instance's timezone (TZ from config.env,
    set by the manager from the host). The model had no clock at all: asked
    about 'this week' it fetched the date through a Home-Assistant tool and
    gave up when that call failed (2026-09-07)."""
    tz = os.environ.get("TZ") or "UTC"
    try:
        from zoneinfo import ZoneInfo
        now = datetime.datetime.now(ZoneInfo(tz))
    except Exception:
        now = datetime.datetime.now().astimezone()
        tz = str(now.tzinfo)
    off = now.strftime("%z")
    off = off[:3] + ":" + off[3:] if len(off) == 5 else off
    return (f"{NOW_TAG} {now.strftime('%A, %Y-%m-%d %H:%M')} {now.tzname()} ({tz}, UTC{off}). "
            "This is the current time for the message below — earlier times stated "
            "in this conversation are outdated. Use it for 'today', 'this week', "
            "dates and times; no tool call needed. Tool results may carry UTC "
            "timestamps (ISO …Z): convert them to this zone before you state a time. "
            f"Datetimes you pass TO tools: ISO 8601 with this offset, e.g. "
            f"{now.strftime('%Y-%m-%d')}T18:30:00{off}.")


def _inject_now():
    """Exactly ONE [Now] system line per turn, refreshed every turn — placed
    LAST, right before the new user message. At the top of the context
    gemini-flash kept answering with a time from earlier turns (18:15 asked,
    '16:37' said); next to the question it is read."""
    _history[:] = [m for m in _history
                   if not (m.get("role") == "system"
                           and str(m.get("content", "")).startswith(NOW_TAG))]
    _history.append({"role": "system", "content": _now_line()})


def _inject_missions():
    """Surface active missions compactly each turn — this way the work state
    survives /reset and restart. For every agent: the manager returns only the
    missions this instance owns. Exactly ONE block, /reset clears it too."""
    _history[:] = [m for m in _history
                   if not (m.get("role") == "system"
                           and str(m.get("content", "")).startswith(MISSION_TAG))]
    try:
        ms = json.loads(_mgr_get(_manager_base(), "/api/missions", timeout=6)).get("missions", [])
    except Exception:
        ms = []
    lines = []
    for m in ms:
        if m.get("status") != "active":
            continue
        cur = next((st for st in m.get("steps", []) if st.get("status") == "doing"),
                   None) or next((st for st in m.get("steps", []) if st.get("status") == "open"), None)
        done = sum(1 for st in m.get("steps", []) if st.get("status") == "done")
        lines.append(f"- {m['id']}: {m['goal'][:100]} ({done}/{len(m.get('steps', []))} steps) — "
                     + (f"currently step {cur['n']}: {cur['text'][:80]} [{cur['status']}]"
                        + (f" @{cur['target']}" if cur.get("target") else "")
                        if cur else "all steps done -> mission_finish!"))
    if lines:
        _history.append({"role": "system", "content":
                            MISSION_TAG + " Your ongoing missions (progress lives in the "
                            "manager, use mission_update/mission_finish):\n" + "\n".join(lines)})


# Upper bound for the conversation _history. Without it the context of a
# long-running process (orchestrator: heartbeat + app chats share ONE _history)
# grows unbounded, and every call sends everything again. Trimming happens only
# BETWEEN turns (here, before the new user message) — never mid tool cycle,
# otherwise a tool result dangles without its tool_calls (API error).
CTX_MAX_MSGS = int(os.environ.get("CTX_MAX_MSGS", "20"))


def _trim_history():
    """On overflow, SUMMARIZE the older messages instead of discarding
    them (summarizing conversation manager). _history[0] (system) is
    pinned; the last CTX_PRESERVE_RECENT conversation messages stay
    verbatim; everything before is condensed into a [Summary] system block
    (an existing summary is folded in). Transient blocks
    (playbooks/memory) are discarded here — _inject/_recall set them
    up again right away. Only call BETWEEN turns, never in the tool cycle."""
    if len(_history) <= CTX_MAX_MSGS:
        return
    if _branch_depth() > 0:
        return          # open side branch: do not trim, the marker must stay
    head = _history[0]
    prior, convo = "", []
    for m in _history[1:]:
        if m.get("role") == "system":
            c = str(m.get("content", ""))
            if c.startswith(SUMMARY_TAG):
                prior = c[len(SUMMARY_TAG):].strip()
            continue    # playbook/recall/summary: do not treat as conversation
        convo.append(m)

    def _boundary_keep(msgs, n):
        """The last n messages, but starting at a user boundary, so that
        no tool result is orphaned from its assistant/tool_calls."""
        k = msgs[-n:] if n < len(msgs) else msgs[:]
        while k and k[0].get("role") != "user":
            k.pop(0)
        return k

    def _prefix(sm):
        return [{"role": "system", "content": SUMMARY_TAG + " " + sm}] if sm else []

    if not CTX_SUMMARY or len(convo) <= CTX_PRESERVE_RECENT:
        # summarizing off/too little -> old behavior, but keep the summary.
        _history[:] = [head] + _prefix(prior) + _boundary_keep(convo, CTX_MAX_MSGS - 1)
        return
    recent = _boundary_keep(convo, CTX_PRESERVE_RECENT)
    to_sum = convo[:len(convo) - len(recent)]
    new_summary = _summarize(to_sum, prior) if to_sum else prior
    if not new_summary:
        # summarizer unavailable -> do not risk losing more context: discard.
        _history[:] = [head] + _prefix(prior) + recent
        return
    _history[:] = [head] + _prefix(new_summary) + recent


# --- steering: interrupt the running agent -----------------------------------
# While a turn is running (tool loop), the user can push in additional messages
# (run_agent: POST /api/steer). They are fed in between two tool
# steps as a user message — the agent changes course instead of
# stubbornly running to the end.
_steer_lock = threading.Lock()
_steer_q = []
_busy = [False]


def steer_push(msg):
    """(accepted?) True if a turn is running and the message is fed in;
    False -> the caller should send it as a normal message."""
    with _steer_lock:
        if not _busy[0]:
            return False
        _steer_q.append(str(msg)[:2000])
        return True


def _drain_steer(hist, on_token=None):
    with _steer_lock:
        msgs, _steer_q[:] = _steer_q[:], []
    for m in msgs:
        hist.append({"role": "user", "content":
                     "[Steering — just pushed in by the user, takes priority] " + m})
        if on_token:
            on_token(f"\n\u21aa {m}\n")
    return bool(msgs)


# --- branches (tree chat): side question in inherited context, clean return --
# /aside opens an aside: a marker remembers the point. /back closes
# the innermost branch: everything after the marker is condensed into ONE sidenote
# (or discarded without a trace with "drop") — the main topic stays unpolluted
# but informed. Nesting is possible (a stack via markers in the history).
BRANCH_MARK = "[Branch]"
NOTE_TAG = "[Sidenote]"


def _branch_depth():
    return sum(1 for m in _history
               if m.get("role") == "system"
               and str(m.get("content", "")).startswith(BRANCH_MARK))


def _branch_open(cmd):
    # /aside is the name, /branch the silent alias (muscle memory, old
    # playbooks — and Claude Code owns "/branch" for git worktrees, which made
    # the old name collide in people's heads).
    word = "/aside" if cmd.startswith("/aside") else "/branch"
    thema = cmd[len(word):].strip()
    _history.append({"role": "system", "content":
                     BRANCH_MARK + (f" Aside: {thema}" if thema else " Aside") +
                     " — the user asks a question aside from the main topic."})
    return f"⑂ Aside opened (depth {_branch_depth()})." + \
        (f" Topic: {thema}" if thema else "")


def _branch_close(cmd):
    drop = cmd[len("/back"):].strip().lower() in ("drop", "verwerfen")
    idx = None
    for i in range(len(_history) - 1, 0, -1):
        m = _history[i]
        if m.get("role") == "system" and str(m.get("content", "")).startswith(BRANCH_MARK):
            idx = i
            break
    if idx is None:
        return "No open side branch."
    segment = _history[idx + 1:]
    note = ""
    if not drop and segment:
        try:
            lines = []
            for m in segment:
                c = _msg_text(m)
                if m.get("role") in ("user", "assistant") and c:
                    lines.append(("User: " if m["role"] == "user" else "Agent: ") + c[:300])
            r = or_chat([{"role": "system", "content":
                          "Summarize this side branch of a conversation in ONE line (max 140 "
                          "characters): the core question and the outcome. Just the line."},
                         {"role": "user", "content": "\n".join(lines)[:6000]}], [])
            note = (r.get("content") or "").strip().splitlines()[0][:160]
        except Exception:
            note = ""
    del _history[idx:]
    if note:
        _history.append({"role": "system", "content": f"{NOTE_TAG} Side branch resolved: {note}"})
    left = _branch_depth()
    return ("↩ Back in the " + ("main topic" if left == 0 else f"branch depth {left}") +
            ("." if drop or not note else f" — sidenote: {note}"))


# Wall-clock budget of the current turn (epoch seconds, 0 = none). The manager
# sets it per task from its own timeout: a task that ran 12 steps of slow web
# fetches once needed more than the manager's 10 minutes, the manager gave up,
# the agent finished into the void and the result was lost. With a deadline
# the loop stops fetching in time and answers with what it has.
_deadline = [0.0]
DEADLINE_MARGIN = 45           # seconds before the deadline reserved for the final answer
_DEADLINE_NOTE = ("[TimeBudget] The time budget for this turn is exhausted. Answer NOW with "
                  "what you have: a partial result is fine, say what is still missing. "
                  "No more tools.")


def _out_of_time():
    return bool(_deadline[0]) and time.time() > _deadline[0] - DEADLINE_MARGIN


def _tool_loop(hist):
    """Tool loop on an arbitrary message list. `hist` is either
    the persistent _history (conversation) or a throwaway list (heartbeat)."""
    for _ in _step_iter():
        _drain_steer(hist)
        if _out_of_time():
            hist.append({"role": "system", "content": _DEADLINE_NOTE})
            msg = or_chat(hist, [])                       # tools off: the final answer
            hist.append(msg)
            return (msg.get("content") or "(empty answer)") + "\n\n⏱️ (time budget exhausted — partial result)"
        msg = or_chat(hist, TOOLS)
        hist.append(msg)
        tcs = msg.get("tool_calls")
        if not tcs:
            # Did a steering message arrive during the answer? Then continue.
            if _drain_steer(hist):
                continue
            return msg.get("content") or "(empty answer)"
        for tc in tcs:
            fn = tc["function"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            out = "(time budget exhausted — not executed)" if _out_of_time() else exec_tool(fn["name"], args)
            log("tool", fn["name"], "->", "(redacted)" if fn["name"] == "get_secret" else out[:80].replace("\n", " "))
            hist.append({"role": "tool", "tool_call_id": tc["id"], "content": out})
    return "(max tool steps reached)"


_STEPS_PREFIX = re.compile(r"^/(?:steps|maxsteps)\s+(\d+|unlimited)\s+(\S.*)$", re.I | re.S)


def _turn_steps(message):
    """'/steps 40 <text>' (alias /maxSteps): the step cap for THIS turn only,
    (n, text); None when the message is not of that form. A task once carried
    '/maxSteps 100 …' as plain text — no such command, the model just read it."""
    m = _STEPS_PREFIX.match(message.strip())
    if not m:
        return None
    n = 0 if m.group(1).lower() == "unlimited" else max(1, int(m.group(1)))
    return n, m.group(2).strip()


def run(user_message, deadline=0.0, kind="chat", turn=None):
    """`turn`: the bridge names the turn up front so it can hand the id to the
    client in a response header — the client then fetches the trace."""
    global MAX_STEPS
    _deadline[0] = float(deadline or 0)
    ts = _turn_steps(user_message)
    if ts:
        saved, MAX_STEPS = MAX_STEPS, ts[0]
        try:
            return run(ts[1], kind=kind, turn=turn)
        finally:
            MAX_STEPS = saved
    _turn_id[0] = turn or uuid.uuid4().hex[:8]
    user_message = _expand_prompt(user_message)
    if user_message.strip() == "/reset":
        del _history[1:]
        return "🔄 Context reset."
    if user_message.startswith("/reasoning"):
        return _set_reasoning(user_message)
    if user_message.startswith("/goal"):
        return _set_goal(user_message)
    if user_message.strip() == "/tools":
        return _tools_report()
    if user_message.startswith("/model"):
        return _set_model(user_message)
    if user_message.startswith("/steps"):
        return _set_steps(user_message)
    if user_message.startswith(("/aside", "/branch")):
        return _branch_open(user_message)
    if user_message.startswith("/back"):
        return _branch_close(user_message)
    # /fresh: run statelessly in a throwaway context — the conversation
    # _history stays untouched (otherwise a heartbeat would wipe out a running
    # app chat, because both share the same _history). For the
    # orchestrator heartbeat: look, delegate, discard.
    if user_message.startswith("/fresh"):
        m = user_message[len("/fresh"):].strip()
        hist = [{"role": "system", "content": SYSTEM},
                {"role": "system", "content": _now_line()},
                {"role": "user", "content": m}]
        _trace_begin("fresh")
        out = "⚠️ (no answer)"
        try:
            out = _tool_loop(hist)
            return out
        finally:
            _trace_end(_outcome_of(out))
            _maybe_learn(hist, m, _outcome_of(out))
    _trim_history()
    _inject_playbooks()
    _inject_missions()
    _inject_memory_index()
    _inject_now()
    _recall(user_message)
    _history.append({"role": "user", "content": user_message})
    _busy[0] = True
    _trace_begin(kind)
    out = "⚠️ (no answer)"
    try:
        out = _run_goal(_history, user_message) if _goal else _tool_loop(_history)
        return out
    finally:
        _busy[0] = False
        _trace_end(_outcome_of(out))
        _maybe_learn(_history, user_message, _outcome_of(out))


def _strip_history_images(messages):
    """Replace image parts in the history with a marker; returns the count.

    Why: a provider can reject an image the history has long carried ("Provided
    image is not valid", e.g. after a model switch with different image rules) —
    and from then on EVERY turn dies before the model runs, silently breaking
    the whole agent (found on a live instance whose memory stayed empty because
    no turn ever reached the tools). The text context is worth more than a dead
    conversation, so on that error the images go and the turn is retried."""
    n = 0
    for m in messages:
        c = m.get("content")
        if not isinstance(c, list):
            continue
        for i, part in enumerate(c):
            if isinstance(part, dict) and part.get("type") == "image_url":
                c[i] = {"type": "text",
                        "text": "[image removed: the provider rejected it]"}
                n += 1
    return n


def or_chat_stream(messages, tools, on_token):
    """Like or_chat, but streaming: calls on_token(text) per delta. Reassembles
    the (assistant) message including any tool_calls from the stream."""
    def _build_llm_body(use_tools):
        b = {"model": OR_MODEL, "messages": _wire_messages(messages), "stream": True, "usage": {"include": True}}
        if LLAMA_ENDPOINT:
            b["stream_options"] = {"include_usage": True}   # llama.cpp: token counts in the last chunk
        if use_tools and tools:
            b["tools"] = tools
            b["tool_choice"] = "auto"
        if _reasoning:
            b["reasoning"] = {"effort": _reasoning}
        return json.dumps(b).encode()

    tools_on = bool(tools)
    body = _build_llm_body(tools_on)
    content = ""
    tcs = {}
    reasoning_open = False
    reasoning_txt = ""
    # Only retry the connection setup (mid-stream is not sensibly retryable,
    # since tokens may already have flowed).
    r = None
    _turn_step[0] += 1
    _t0 = time.monotonic()
    for attempt in range(LLM_RETRIES + 1):
        req = urllib.request.Request(_llm_url(), data=body, method="POST",
                                     headers=_llm_headers())
        try:
            r = urllib.request.urlopen(req, timeout=LLM_STREAM_TIMEOUT)
            break
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", "replace")[:400]
            # With large string arguments (e.g. a whole file), local models often
            # produce broken tool-call JSON -> llama.cpp answers 500
            # ("Failed to parse tool call arguments as JSON"). A retry
            # with the same body fails again immediately; instead retry ONCE without
            # tools: the model then emits the answer as text/code
            # instead of losing the whole turn.
            if (e.code == 500 and tools_on
                    and ("tool call" in err_body.lower() or "tool_call" in err_body.lower())):
                tools_on = False
                body = _build_llm_body(False)
                on_token("\n⚠️ Invalid tool-call JSON from the local model — "
                         "round retried without tools (answer as text).\n")
                continue
            if e.code == 400 and "image" in err_body.lower() \
                    and _strip_history_images(messages):
                body = _build_llm_body(tools_on)
                on_token("\n⚠️ The provider rejected an image in the history — "
                         "images removed, turn retried.\n")
                continue
            m = f"⚠️ {LLM_NAME} HTTP {e.code}: {err_body[:300]}"
            if LLAMA_ENDPOINT and e.code == 503:
                m = _LOADING_MSG
            elif e.code in _RETRY_CODES and attempt < LLM_RETRIES:
                _retry_sleep(attempt); continue
            report_usage({}, ms=int((time.monotonic() - _t0) * 1000), ok=False, err=m)
            on_token(m); return {"role": "assistant", "content": m}
        except Exception as e:
            m = f"⚠️ {LLM_NAME} error: {e!r}"
            if LLAMA_ENDPOINT and _conn_dropped(e):
                m = _llama_dropped_msg(messages)
            elif _retry_after(e, attempt):
                _retry_sleep(attempt); continue
            report_usage({}, ms=int((time.monotonic() - _t0) * 1000), ok=False, err=m)
            on_token(m); return {"role": "assistant", "content": m}
    got_usage = False
    try:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except Exception:
                continue
            if chunk.get("usage"):          # the last chunk carries the billing
                got_usage = True
                report_usage(chunk["usage"], ms=int((time.monotonic() - _t0) * 1000))
            try:
                delta = chunk["choices"][0]["delta"]
            except (KeyError, IndexError):
                continue
            # OpenRouter calls it "reasoning", llama.cpp (Qwen3 et al.) "reasoning_content"
            rzn = delta.get("reasoning") or delta.get("reasoning_content")
            if rzn:
                if not reasoning_open:
                    on_token(THINK_START); reasoning_open = True
                reasoning_txt += rzn
                on_token(rzn)
            c = delta.get("content")
            if c:
                if reasoning_open:
                    on_token(THINK_END); reasoning_open = False
                content += c
                on_token(c)
            _tcs = delta.get("tool_calls") or []
            if _tcs and reasoning_open:
                on_token(THINK_END); reasoning_open = False
            for tc in _tcs:
                i = tc.get("index", 0)
                slot = tcs.setdefault(i, {"id": "", "type": "function",
                                         "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                f = tc.get("function") or {}
                if f.get("name"):
                    slot["function"]["name"] += f["name"]
                if f.get("arguments"):
                    slot["function"]["arguments"] += f["arguments"]
        if reasoning_open:
            on_token(THINK_END)
    except Exception as e:
        # aborted mid-stream: keep what was already streamed, report the rest.
        m = f"⚠️ {LLM_NAME} stream aborted: {e!r}"
        on_token(m)
        content += ("\n" + m)
    if LLAMA_ENDPOINT and not content and not reasoning_txt and not tcs and not got_usage:
        # llama.cpp answered 200 and then died (an image did that): the stream
        # ends cleanly with nothing in it — not an empty reply, a dropped one.
        m = _llama_dropped_msg(messages)
        report_usage({}, ms=int((time.monotonic() - _t0) * 1000), ok=False, err=m)
        on_token(m)
        content = m
    # Some reasoning models emit EVERYTHING as thinking and leave content empty
    # -> instead of an empty answer, keep the thinking (otherwise "_(empty reply)_").
    msg = {"role": "assistant", "content": content or reasoning_txt or None}
    if tcs:
        msg["tool_calls"] = [tcs[i] for i in sorted(tcs)]
    return msg


def run_stream(user_message, on_token, image=None, deadline=0.0, kind="stream", turn=None):
    _deadline[0] = float(deadline or 0)
    _turn_id[0] = turn or uuid.uuid4().hex[:8]
    """Like run(), but streams the answer tokens via on_token. Tool rounds
    produce no text; the final answer is streamed.
    image: optional base64 JPEG -> sent as vision content to OpenRouter."""
    user_message = _expand_prompt(user_message)
    if user_message.strip() == "/reset":
        del _history[1:]
        on_token("🔄 Context reset.")
        return
    if user_message.startswith("/reasoning"):
        on_token(_set_reasoning(user_message))
        return
    if user_message.startswith("/goal"):
        on_token(_set_goal(user_message))
        return
    if user_message.strip() == "/tools":
        on_token(_tools_report())
        return
    if user_message.startswith("/model"):
        on_token(_set_model(user_message))
        return
    if user_message.startswith("/steps"):
        on_token(_set_steps(user_message))
        return
    if user_message.startswith(("/aside", "/branch")):
        on_token(_branch_open(user_message))
        return
    if user_message.startswith("/back"):
        on_token(_branch_close(user_message))
        return
    # /fresh: stateless as in run(), conversation untouched. Heartbeats
    # need no streaming — emit the answer once.
    if user_message.startswith("/fresh"):
        m = user_message[len("/fresh"):].strip()
        on_token(_tool_loop([{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": m}]))
        return
    _trim_history()
    _inject_playbooks()
    _inject_missions()
    _inject_memory_index()
    _inject_now()
    _recall(user_message)
    if image:
        content = [
            {"type": "text", "text": user_message or "What is in the image?"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}},
        ]
    else:
        content = user_message
    _history.append({"role": "user", "content": content})
    _busy[0] = True
    _trace_begin(kind)
    outcome = "error"
    try:
        if _goal:
            # With an active goal the answer is refined against the judge (not
            # streamed) and then emitted as a whole.
            ans = _run_goal(_history, user_message)
            on_token(ans)
            outcome = _outcome_of(ans)
            return
        for _ in _step_iter():
            _drain_steer(_history, on_token)
            if _out_of_time():
                _history.append({"role": "system", "content": _DEADLINE_NOTE})
                _history.append(or_chat_stream(_history, [], on_token))
                on_token("\n\n⏱️ (time budget exhausted — partial result)")
                outcome = "deadline"
                return
            msg = or_chat_stream(_history, TOOLS, on_token)
            _history.append(msg)
            tcs = msg.get("tool_calls")
            if not tcs:
                if _drain_steer(_history, on_token):
                    continue
                outcome = _outcome_of(msg.get("content"))
                return
            for tc in tcs:
                fn = tc["function"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                on_token(f"\n\U0001f527 {fn['name']} \u2026")
                _hb_stop = threading.Event()
                def _heartbeat(ev=_hb_stop):
                    while not ev.wait(HEARTBEAT_SEC):
                        try:
                            on_token(" \u00b7")
                        except Exception:
                            return
                _hb = threading.Thread(target=_heartbeat, daemon=True)
                _hb.start()
                try:
                    out = exec_tool(fn["name"], args)
                finally:
                    _hb_stop.set()
                    _hb.join(timeout=1)
                on_token("\n")
                log("tool", fn["name"], "->", "(redacted)" if fn["name"] == "get_secret" else out[:80].replace("\n", " "))
                _history.append({"role": "tool", "tool_call_id": tc["id"], "content": out})
        on_token("\n(max tool steps reached)")
        outcome = "max_steps"
    finally:
        _busy[0] = False
        _trace_end(outcome)
        _maybe_learn(_history, user_message, outcome)


# Tool plugins (pi.dev extension idea, ported): one .py file per tool,
# placed on the config disk by the manager (/config/plugins). Convention:
#   DESC = "…"; PARAMS = {...}; REQUIRED = [...];  def run(**kwargs): ...
# The filename (without .py) becomes the tool name. The microVM is the sandbox.
PLUGIN_TOOLS = set()
PLUGIN_DIR = os.environ.get("PLUGIN_DIR", "/config/plugins")


def load_plugins():
    import importlib.util, sys
    if not os.path.isdir(PLUGIN_DIR):
        return
    for entry in sorted(os.listdir(PLUGIN_DIR)):
        path = os.path.join(PLUGIN_DIR, entry)
        syspath_add = None
        if os.path.isdir(path):
            # multi-file tool: folder <name>/ with entry file tool.py (or
            # __init__.py / <name>.py). The folder goes on sys.path so that
            # internal imports (import helper) work.
            name = os.path.basename(path)
            src = None
            for cand in ("tool.py", "__init__.py", name + ".py"):
                if os.path.isfile(os.path.join(path, cand)):
                    src = os.path.join(path, cand); break
            if not src:
                log(f"plugin '{name}' ignored: no tool.py/__init__.py in the folder")
                continue
            syspath_add = path
        elif path.endswith(".py"):
            name, src = os.path.basename(path)[:-3], path
        else:
            continue
        if name in BUILTIN and name not in PLUGIN_TOOLS:
            log(f"plugin '{name}' ignored: collides with a built-in tool")
            continue
        try:
            if syspath_add and syspath_add not in sys.path:
                sys.path.insert(0, syspath_add)
            spec = importlib.util.spec_from_file_location("plugin_" + name, src)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            BUILTIN[name] = (mod.run, str(getattr(mod, "DESC", name))[:300],
                             getattr(mod, "PARAMS", {}), getattr(mod, "REQUIRED", []))
            PLUGIN_TOOLS.add(name)
            log(f"plugin loaded: {name}")
        except Exception as e:
            log(f"plugin '{name}' ERROR: {e!r}")


def init():
    global TOOLS
    os.makedirs(WORKDIR, exist_ok=True)
    load_plugins()
    TOOLS = builtin_schema() + init_mcp()
    log(f"agent ready: backend={LLM_BACKEND} url={_llm_url()} model={OR_MODEL} tools={len(TOOLS)} workdir={WORKDIR}")
