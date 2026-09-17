# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""Context offloading: large tool outputs are written to the workspace and replaced by a preview; offload_read fetches a slice on demand.

Part of the openrouter agent package (runs inside the VM): no import from the package root. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import json
import os
import re
import uuid

from . import config as _config


# --- 3) Context-Offloader ---------------------------------------------------
OFFLOAD_DIR = os.path.join(_config.WORKDIR, ".offload")
OFFLOAD_MIN = int(os.environ.get("OFFLOAD_MIN", str(_config.MAX_TOOL_OUT)))
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
        return out[:_config.MAX_TOOL_OUT]   # offloading failed -> fall back: hard-truncate
    preview = _smart_preview(out, OFFLOAD_PREVIEW)
    return (preview + f"\n\n[… full output offloaded ({len(out)} characters). "
            f"Read verbatim with offload_read(id=\"{oid}\", offset=0).]")


def t_offload_read(id="", offset=0, length=None):
    """Read an offloaded tool output (see the offload reference) in chunks."""
    length = int(length) if length else _config.MAX_TOOL_OUT
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
