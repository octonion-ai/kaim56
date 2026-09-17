# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""Memory as files: one folder per instance, mounted into its VM at /memory.

Why: the flat key/value store and the embedding index are invisible — nobody
can read, correct or grep them, and the agent cannot navigate them. Instinct's
pattern (a git repo of Markdown with [[wiki-links]], navigated by grep, a
timeline that coarsens like human memory) fits a platform whose agents already
have files, grep and a workspace. Layout under BASE/memory/<instance>/:

  MEMORY.md        index: one line per note, pointers to the timeline (regenerated)
  notes/<slug>.md  one note per key — written by the manager (memory_store) or
                   by the agent itself (it owns the folder read-write)
  timeline/        YYYY-MM-DD.md raw entries; coarsened to one trimmed line per
                   entry after RAW_DAYS, folded into YYYY-Www.md after DAILY_DAYS

Every change the manager makes is committed to a git repo in the folder; the
hourly sweep commits what the agent wrote on its own. Part of the mgr package:
no imports from manager.py; stdlib only.
"""
import os
import re
import subprocess
import threading
import time
from datetime import date

MEMORY_ROOT = None                 # via configure(BASE)
OWNER = None                       # (uid, gid) the folders belong to: the NFS squash user,
                                   # group = operator; None = leave as created (tests, non-root)
RAW_DAYS = 2                       # raw entries stay this many days
DAILY_DAYS = 14                    # trimmed daily files stay this many days, then weekly
LINE_MAX = 120                     # a trimmed timeline line
RAW_MAX = 400                      # a raw one (today's context, before coarsening)
INDEX_MAX_NOTES = 200
_lock = threading.Lock()


def configure(base: str) -> None:
    global MEMORY_ROOT
    MEMORY_ROOT = os.path.join(base, "memory")


def _own(path, is_dir=False):
    """Give a file/folder the manager wrote to the guest user: the VM must be
    able to write next to it, the operator's group may read it."""
    if not OWNER:
        return
    try:
        os.chown(path, OWNER[0], OWNER[1])
        os.chmod(path, 0o2750 if is_dir else 0o640)
    except OSError:
        pass


def _own_tree(d):
    for sub in ("", "notes", "timeline"):
        p = os.path.join(d, sub)
        _own(p, True)
        for f in os.listdir(p):
            fp = os.path.join(p, f)
            if os.path.isfile(fp):
                _own(fp)
    _own_git(d)


def _own_git(d):
    """C-1: the .git tree must belong to the guest user too — git now runs AS
    that user (see _git), so a root-owned .git (as older folders have) would
    make every commit fail. Walk only when the top is not ours yet (cheap)."""
    g = os.path.join(d, ".git")
    if not OWNER or not os.path.isdir(g):
        return
    try:
        st = os.stat(g)
        if (st.st_uid, st.st_gid) == OWNER:
            return
        for root, dirs, files in os.walk(g):
            for name in dirs:
                _own(os.path.join(root, name), True)
            for name in files:
                _own(os.path.join(root, name))
        _own(g, True)
    except OSError:
        pass


def _safe(name):
    """An instance name as the manager mints them — anything else (a path,
    a dot-prefix, an empty string) is refused rather than sanitized, so a
    name can never point outside MEMORY_ROOT."""
    n = str(name or "")
    return n if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", n) else ""


def slug(key):
    s = re.sub(r"[^a-z0-9]+", "-", str(key or "").lower()).strip("-")
    return s[:80] or "note"


def folder(instance):
    """The instance's memory folder (created with the layout on first use)."""
    inst = _safe(instance)
    if not inst or not MEMORY_ROOT:
        return None
    d = os.path.join(MEMORY_ROOT, inst)
    for sub in ("", "notes", "timeline"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    if not os.path.exists(os.path.join(d, ".git")):
        _git(d, "init", "-q")
        _git(d, "config", "user.name", "kaim56")
        _git(d, "config", "user.email", "kaim56@localhost")
    if not os.path.exists(os.path.join(d, "MEMORY.md")):
        _write_index(d, inst)
    _own_tree(d)
    return d


def _git_invocation(d, *args):
    """C-1 (security review): the memory folder is exported READ-WRITE to the
    guest, so a compromised agent can plant .git/hooks/* or set core.fsmonitor
    in .git/config. Git therefore (1) runs as the GUEST user, never as root —
    a hook that fires runs with the guest's rights only — and (2) has every
    config-driven execution path switched off on the command line, which
    overrides the guest-writable .git/config. Returns (argv, env, run_kwargs)."""
    cmd = ["git", "-C", d, "-c", "safe.directory=*",
           "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
           "-c", "core.sshCommand=/bin/false", *args]
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "HOME": d, "GIT_TERMINAL_PROMPT": "0"}
    kw = {}
    if OWNER and os.geteuid() == 0:
        kw = {"user": OWNER[0], "group": OWNER[1], "extra_groups": [OWNER[1]]}   # drop root's groups too
    return cmd, env, kw


def _git(d, *args):
    try:
        cmd, env, kw = _git_invocation(d, *args)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False, env=env, **kw)
    except (OSError, subprocess.SubprocessError):
        return None


def commit(instance, msg):
    """Commit everything in the folder (the agent's own edits included)."""
    d = folder(instance)
    if not d:
        return False
    with _lock:
        _git(d, "add", "-A")
        r = _git(d, "commit", "-q", "-m", msg[:200])
    return bool(r is not None and r.returncode == 0)


# ---- notes ------------------------------------------------------------------
def note_write(instance, key, value):
    """Mirror a memory_store into notes/<slug>.md (value None = delete)."""
    d = folder(instance)
    if not d or not key:
        return None
    p = os.path.join(d, "notes", slug(key) + ".md")
    if value is None:
        try:
            os.unlink(p)
        except OSError:
            pass
    else:
        body = value if isinstance(value, str) else repr(value)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(f"# {key}\n\n{body.rstrip()}\n\n"
                     f"<!-- key: {key} · updated: {time.strftime('%Y-%m-%d %H:%M')} -->\n")
        _own(p)
    rebuild_index(instance)
    return p


def _note_head(p):
    try:
        with open(p, encoding="utf-8") as fh:
            lines = [l.strip() for l in fh.read().split("\n")]
    except OSError:
        return "", ""
    title = next((l[2:] for l in lines if l.startswith("# ")), os.path.basename(p)[:-3])
    first = next((l for l in lines if l and not l.startswith("#") and not l.startswith("<!--")), "")
    return title, first[:LINE_MAX]


def rebuild_index(instance):
    """MEMORY.md: what is here, one line per note, plus the timeline files —
    small enough to sit in every turn's context."""
    d = folder(instance)
    if not d:
        return ""
    return _write_index(d, _safe(instance))


def _write_index(d, instance):
    notes = sorted(f for f in os.listdir(os.path.join(d, "notes")) if f.endswith(".md"))
    tl = sorted(f for f in os.listdir(os.path.join(d, "timeline")) if f.endswith(".md"))
    out = [f"# Memory of {instance}", "",
           "Notes are Markdown files in `notes/` (one per topic, link with [[slug]]),",
           "the timeline in `timeline/` (raw per day, trimmed after a couple of days,",
           "weekly after two weeks). Grep this folder; write notes yourself.", ""]
    out.append(f"## Notes ({len(notes)})")
    for f in notes[:INDEX_MAX_NOTES]:
        title, first = _note_head(os.path.join(d, "notes", f))
        out.append(f"- [[{f[:-3]}]] {title}" + (f" — {first}" if first else ""))
    if len(notes) > INDEX_MAX_NOTES:
        out.append(f"- … {len(notes) - INDEX_MAX_NOTES} more (ls notes/)")
    out.append("")
    out.append(f"## Timeline ({len(tl)} files)")
    out.append("- " + ", ".join(f[:-3] for f in tl[-8:]) if tl else "- (empty)")
    text = "\n".join(out) + "\n"
    with open(os.path.join(d, "MEMORY.md"), "w", encoding="utf-8") as fh:
        fh.write(text)
    _own(os.path.join(d, "MEMORY.md"))
    return text


def index_text(instance, max_lines=60):
    d = folder(instance)
    if not d:
        return ""
    try:
        with open(os.path.join(d, "MEMORY.md"), encoding="utf-8") as fh:
            lines = fh.read().split("\n")
    except OSError:
        return ""
    return "\n".join(lines[:max_lines])


# ---- timeline ---------------------------------------------------------------
def _one_line(s, n=LINE_MAX):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def timeline_add(instance, kind, user_text="", reply_text="", when=None):
    """Append one raw entry to today's timeline file."""
    d = folder(instance)
    if not d:
        return None
    when = when or time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(when))
    p = os.path.join(d, "timeline", day + ".md")
    new = not os.path.exists(p)
    with open(p, "a", encoding="utf-8") as fh:
        if new:
            fh.write(f"# {day}\n\n")
        fh.write(f"- {time.strftime('%H:%M', time.localtime(when))} [{_safe(kind) or 'turn'}] "
                 f"{_one_line(user_text, RAW_MAX)}"
                 + (f" → {_one_line(reply_text, RAW_MAX)}" if reply_text else "") + "\n")
    _own(p)
    if new:
        rebuild_index(instance)
    return p


def _entries(p):
    try:
        with open(p, encoding="utf-8") as fh:
            return [l.rstrip("\n") for l in fh if l.startswith("- ")]
    except OSError:
        return []


def _is_trimmed(p):
    try:
        with open(p, encoding="utf-8") as fh:
            return fh.readline().rstrip().endswith("(trimmed)")
    except OSError:
        return True


def coarsen(instance, today=None):
    """raw → daily (trimmed lines) after RAW_DAYS; daily → weekly after
    DAILY_DAYS. Deterministic, no model: what we keep is the head of each
    entry, what we fold is the count and the first heads of each day."""
    d = folder(instance)
    if not d:
        return {}
    today = today or date.today()
    tdir = os.path.join(d, "timeline")
    stats = {"trimmed": 0, "folded": 0}
    for f in sorted(os.listdir(tdir)):
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})\.md", f)
        if not m:
            continue
        day = date(int(m[1]), int(m[2]), int(m[3]))
        age = (today - day).days
        p = os.path.join(tdir, f)
        if age > DAILY_DAYS:
            heads = [e[2:] for e in _entries(p)]
            week = os.path.join(tdir, f"{day.isocalendar()[0]}-W{day.isocalendar()[1]:02d}.md")
            new = not os.path.exists(week)
            with open(week, "a", encoding="utf-8") as fh:
                if new:
                    fh.write(f"# week {day.isocalendar()[1]:02d}/{day.isocalendar()[0]}\n\n")
                fh.write(f"- {day}: {len(heads)} entries" + (" — " + " · ".join(_one_line(h)[:60] for h in heads[:3]) if heads else "") + "\n")
            os.unlink(p)
            stats["folded"] += 1
        elif age > RAW_DAYS and not _is_trimmed(p):
            trimmed = [_one_line(e) for e in _entries(p)]
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(f"# {day} (trimmed)\n\n" + "\n".join(trimmed) + "\n")
            stats["trimmed"] += 1
    rebuild_index(instance)
    _own_tree(d)
    return stats


def sweep(instances):
    """Hourly: coarsen every instance's timeline and commit whatever changed
    (the agent's own notes included)."""
    out = {}
    for inst in instances:
        try:
            out[inst] = coarsen(inst)
            commit(inst, "sweep: coarsen timeline, capture agent edits")
        except Exception as e:      # one broken folder must not stop the others
            out[inst] = {"error": repr(e)[:120]}
    return out
