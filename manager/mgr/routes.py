# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""Routing table for the manager's HTTP handler.

Why this exists: the handler grew into a chain of ~78 sequential
``if self.path == …`` / ``startswith(…)`` branches. That chain has one nasty
property — a prefix branch standing before an exact branch silently swallows
it, and nothing tells you. It bit us twice (``/api/skills/`` vs ``/api/skills``,
and the mission routes).

Here the two kinds are kept apart: exact paths in a dict, prefixes in a list
that is always tried longest-first, and exact ALWAYS wins over prefix. With
that, the ordering in which routes happen to be registered no longer decides
who answers — shadowing cannot happen by construction.

The table is also readable from outside (:meth:`Router.inventory`), which is
what makes an "which route is not authenticated?" audit a loop instead of a
reading exercise.

Part of the mgr package: no imports from manager.py (no cycles).
"""
import json
import urllib.request

from mgr import util as _util


class Router:
    """Exact paths and path prefixes per HTTP method."""

    def __init__(self):
        # {method: {path: handler}} and {method: [(prefix, handler)]}
        self._exact = {}
        self._prefix = {}

    # ---- registration ----------------------------------------------------
    def add(self, method, path, handler, prefix=False, admin=False):
        """Register a route. `admin=True` means: not reachable from a guest VM.

        Access being a property of the route — instead of a list of paths kept
        somewhere else in the handler — is the point: it can be read back, so
        "which route is open to the agents?" is answerable without reading code.
        """
        method = method.upper()
        entry = (handler, bool(admin))
        if prefix:
            lst = self._prefix.setdefault(method, [])
            if any(p == path for p, _ in lst):
                raise ValueError(f"prefix route registered twice: {method} {path}")
            lst.append((path, entry))
            # Longest first: /api/instances/x/stop must win over /api/instances/.
            lst.sort(key=lambda t: len(t[0]), reverse=True)
        else:
            table = self._exact.setdefault(method, {})
            if path in table:
                raise ValueError(f"exact route registered twice: {method} {path}")
            table[path] = entry
        return handler

    def get(self, path, prefix=False, admin=False):
        """Decorator: register a GET route."""
        def deco(fn):
            return self.add("GET", path, fn, prefix, admin)
        return deco

    def post(self, path, prefix=False, admin=False):
        """Decorator: register a POST route."""
        def deco(fn):
            return self.add("POST", path, fn, prefix, admin)
        return deco

    # ---- lookup ----------------------------------------------------------
    def resolve(self, method, path):
        """``(handler, admin)`` for this request, or None when nothing matches.

        The query string is not part of the match; a trailing slash is only
        stripped when that turns the path into a known exact route (so
        ``/katfs/`` keeps its meaning as a prefix).
        """
        p = path.split("?", 1)[0]
        exact = self._exact.get(method.upper(), {})
        if p in exact:
            return exact[p]
        if p.endswith("/") and p[:-1] in exact:
            return exact[p[:-1]]
        for prefix, entry in self._prefix.get(method.upper(), []):
            if p.startswith(prefix):
                return entry
        return None

    # ---- introspection ---------------------------------------------------
    def inventory(self):
        """[(method, kind, path, admin_only)] for every route, sorted."""
        out = []
        for method, table in self._exact.items():
            out += [(method, "exact", p, e[1]) for p, e in table.items()]
        for method, lst in self._prefix.items():
            out += [(method, "prefix", p, e[1]) for p, e in lst]
        return sorted(out)

    def conflicts(self):
        """Exact routes that a registered prefix would also match.

        Not an error here — the exact route wins — but worth listing, because
        in a hand-written if-chain the same pair is a bug waiting to happen.
        """
        out = []
        for method, table in self._exact.items():
            for p in table:
                for prefix, _ in self._prefix.get(method, []):
                    if p.startswith(prefix):
                        out.append((method, p, prefix))
        return sorted(out)


def source_routes(source):
    """Route literals as they appear in a hand-written if-chain.

    Reads the handler source instead of the table, so the guard test covers the
    branches that have NOT been migrated yet. Returns
    [(kind, path, line_number)] in the order the source tests them.
    """
    import re
    out = []
    pat = re.compile(
        r"""(?P<eq>self\.path\s*==\s*["'](?P<p1>/[^"']*)["'])"""
        r"""|(?P<eqp>_p{1,2}\s*==\s*["'](?P<p2>/[^"']*)["'])"""
        r"""|(?P<sw>self\.path\.startswith\(\s*["'](?P<p3>/[^"']*)["'])"""
        r"""|(?P<swp>_p{1,2}\.startswith\(\s*["'](?P<p4>/[^"']*)["'])""")
    for n, line in enumerate(source.splitlines(), 1):
        for m in pat.finditer(line):
            if m.group("eq") or m.group("eqp"):
                out.append(("exact", m.group("p1") or m.group("p2"), n))
            else:
                out.append(("prefix", m.group("p3") or m.group("p4"), n))
    return out


def shadowed(routes):
    """Exact routes that an EARLIER prefix in the same chain swallows.

    That is the actual defect: the prefix branch runs first, so the exact
    branch below it is dead code. Returns [(exact_path, prefix_path, line)].
    """
    out = []
    for kind, path, line in routes:
        if kind != "exact":
            continue
        for pkind, ppath, pline in routes:
            if pkind == "prefix" and pline < line and path.startswith(ppath):
                out.append((path, ppath, line))
    return out


ROUTER = Router()



def _msg_route(method, path, prefix=False, admin=True):
    """Decorator for the {"msg": …} family (UI actions): the function returns
    a status string; exceptions become "error: …" instead of a dropped
    connection, exactly as the old chain did."""
    def deco(fn):
        def wrapped(h):
            try:
                msg = fn(h)
            except _util.BodyTooLarge:
                raise
            except Exception as e:
                msg = f"error: {e!r}"
            return json.dumps({"msg": msg}).encode(), "application/json"
        wrapped.__name__ = fn.__name__
        ROUTER.add(method, path, wrapped, prefix=prefix, admin=admin)
        return fn
    return deco


def _qs(h):
    return urllib.parse.parse_qs(h.path.partition("?")[2])


def _tail(h, prefix):
    """Path remainder after `prefix`, query stripped, URL-decoded per segment."""
    return [urllib.parse.unquote(x) for x in h.path.split("?", 1)[0][len(prefix):].split("/")]


# Request bodies are read whole into the root process: cap them. A guest (or
# anyone on the LAN) must not be able to hand the manager a gigabyte.
BODY_MAX = 4 * 1024 * 1024            # JSON routes
BODY_MAX_LLM = 8 * 1024 * 1024        # chat completions (long contexts, images)
BODY_MAX_AUDIO = 32 * 1024 * 1024     # STT uploads
