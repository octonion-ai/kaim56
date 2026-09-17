# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""Skills from experience: after a long successful turn the agent distils a skill proposal and sends it to the manager for approval.

Part of the openrouter agent package (runs inside the VM): no import from the package root. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import json
import os
import threading

from . import config as _config
from . import llm as _llm
from . import mgrclient as _mgrclient
from . import observe as _observe


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
        reply = _llm.or_chat(msgs, []).get("content") or ""
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
        return _mgrclient._mgr(_mgrclient._manager_base(), "/api/skill-proposals",
                    {"name": d["name"], "description": d["description"], "content": d["content"],
                     "turn": _observe._turn_id[0], "note": f"distilled after: {str(user_text)[:120]}"}, timeout=10)
    except Exception:
        return None


def _maybe_learn(hist, user_text, outcome):
    """Fire the distillation in the background when the turn qualifies:
    enabled, ended well, at least SKILL_LEARN_MIN_STEPS model calls."""
    if not SKILL_LEARN or outcome != "ok" or _observe._turn_step[0] < SKILL_LEARN_MIN_STEPS:
        return False
    if str(user_text).startswith("/"):
        return False
    # A-4: the distillation is an extra background LLM call — log it so the
    # per-turn cost is not invisible (its usage is booked via or_chat under this
    # turn id). SKILL_LEARN=0 in the instance config turns it off per instance.
    _config.log(f"skill-learn: distilling a skill proposal from this turn "
        f"({_observe._turn_step[0]} steps) — extra model call; set SKILL_LEARN=0 to disable")
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
