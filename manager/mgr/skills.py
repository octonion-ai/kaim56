# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""Skills library (expert knowledge the agent loads on demand) and skills from experience: proposals, lint, approval, session search.

Part of the mgr package: no import from manager.py. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import json
import os
import re
import time
import uuid

from mgr import chats as _chats
from mgr import notify as _notify
from mgr import paths as _paths
from mgr import store as _store


# ---- Skills library (expert knowledge, loaded on demand by the agent) -------
SKILLS_FILE = os.path.join(_paths.BASE, "skills.json")


def load_skills():
    try:
        with open(SKILLS_FILE) as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return data
    except (FileNotFoundError, ValueError):
        pass
    return []


def save_skills(items):
    if not isinstance(items, list):
        return -1
    try:
        with open(SKILLS_FILE, "w") as fh:
            json.dump(items, fh, indent=2, ensure_ascii=False)
        return len(items)
    except OSError:
        return -1


def upsert_skill(name, description, content):
    name = re.sub(r"[^a-z0-9_-]", "", (name or "").lower())
    if not name:
        return "invalid name (only a-z 0-9 _ -)"
    items = [s for s in load_skills() if s.get("name") != name]
    items.append({"name": name, "description": description or "", "content": content or ""})
    save_skills(items)
    return f"skill '{name}' saved"


def delete_skill(name):
    save_skills([s for s in load_skills() if s.get("name") != name])
    return f"skill '{name}' deleted"


# ---- skills from experience -------------------------------------------------
# After a long, successful turn the agent distills the way it went into a
# SKILL proposal (Hermes' learning loop, with our approval gate): it lands
# here, the operator approves it in the Skills tab, only then it enters the
# catalog every agent loads from. Nothing an agent writes becomes a skill by
# itself.
SKILL_PROPOSALS_FILE = os.path.join(_paths.BASE, "skill_proposals.json")
PROPOSALS_MAX = 50
_SECRETISH = re.compile(r"(sk-or-[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9]{16,}|Bearer [A-Za-z0-9._-]{16,}|hf_[A-Za-z0-9]{16,})")


def load_proposals():
    try:
        with open(SKILL_PROPOSALS_FILE) as fh:
            d = json.load(fh)
        return d if isinstance(d, list) else []
    except (FileNotFoundError, ValueError):
        return []


def save_proposals(items):
    tmp = SKILL_PROPOSALS_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(items, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, SKILL_PROPOSALS_FILE)


def skill_lint(name, description, content):
    """'' when a proposal is acceptable, else why not. Advisory rules in the
    spirit of Hermes' linter: a usable name, a one-line description, a body
    that is a procedure and not a dump, nothing that looks like a credential."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,47}", name or ""):
        return "name must be 2-48 chars of a-z 0-9 _ -"
    if not (description or "").strip() or len(description) > 200:
        return "description must be one line (1-200 chars)"
    body = (content or "").strip()
    if len(body) < 80:
        return "content too short to be a procedure"
    if len(body) > 12000:
        return "content over 12 kB — a skill is a procedure, not a log"
    if _SECRETISH.search(body):
        return "content looks like it contains a credential"
    return ""


def proposal_add(instance, name, description, content, turn="", note=""):
    name = re.sub(r"[^a-z0-9_-]", "", str(name or "").lower())[:48]
    why = skill_lint(name, str(description or "").strip(), str(content or ""))
    if why:
        return None, why
    items = [p for p in load_proposals() if not (p.get("name") == name and p.get("status") == "proposed")]
    pid = uuid.uuid4().hex[:10]
    items.append({"id": pid, "ts": int(time.time()), "instance": str(instance or "")[:80], "turn": str(turn or "")[:16],
                  "name": name, "description": str(description).strip()[:200], "content": str(content).strip(),
                  "note": str(note or "")[:200], "status": "proposed",
                  "update": any(s.get("name") == name for s in load_skills())})
    items = items[-PROPOSALS_MAX:]
    save_proposals(items)
    try:
        _notify.notify_add(instance or "skills", f"Skill proposal: {name}",
                   f"{str(description).strip()[:160]} — review in the Skills tab", link="")
    except Exception:
        pass
    return pid, "ok"


def proposal_decide(pid, approve):
    items = load_proposals()
    p = next((x for x in items if x.get("id") == pid), None)
    if p is None:
        return "unknown"
    if approve:
        msg = upsert_skill(p["name"], p.get("description", ""), p.get("content", ""))
        p["status"] = "approved"
    else:
        msg = f"proposal '{p['name']}' discarded"
        p["status"] = "discarded"
    p["decided"] = int(time.time())
    save_proposals(items)
    return msg


def sessions_search(query, instance=None, limit=10):
    """Exact search over chats and task runs (FTS5), index refreshed from
    chats.json when it changed."""
    try:
        mt = os.path.getmtime(_chats.CHATS_FILE)
    except OSError:
        mt = 0
    _store.sessions_refresh(_chats.load_chats(), mt)
    return _store.sessions_query(query, instance=instance, limit=limit)
