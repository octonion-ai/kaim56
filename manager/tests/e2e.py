#!/usr/bin/env python3
"""End-to-end and unit tests for kAIm56 (manager + openrouter agent).

Stdlib-only (unittest), in line with the project's philosophy — no test
dependency. Three tiers, depending on the environment:

  * OFFLINE  — agent/manager functions directly (import), no VM, no network.
               Always run.
  * HTTP     — against the running manager on 127.0.0.1:8700. Skipped when
               the manager is not reachable.
  * LIVE     — round-trip to a running agent VM (orchestrator). Only the free
               /goal path (no model call). Skipped when the instance is not
               running.

Run:  python3 tests/e2e.py            (or ./run-tests.sh)
One tier only:  python3 tests/e2e.py AgentLogic
"""
import base64
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile

# --- paths to the modules under test ----------------------------------------
FC_DIR = os.environ.get("FC_DIR", "/home/ulrich/firecracker")
AGENT_PATH = os.environ.get("AGENT_PATH", "/home/ulrich/openrouter-agent/agent.py")
MANAGER_PATH = os.path.join(FC_DIR, "manager.py")
MANAGER_URL = os.environ.get("MANAGER_URL", "http://127.0.0.1:8700")

# manager.py imports the sibling module `chatui` -> its directory must be on the
# search path, otherwise the import fails in the manager unit tests.
if FC_DIR not in sys.path:
    sys.path.insert(0, FC_DIR)


def _load(name, path, env=None):
    """Load a module from a file; optionally set os.environ first."""
    if env:
        os.environ.update(env)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _http(path, method="GET", body=None, timeout=8):
    """(status, text) against the manager. Raises on a connection error."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(MANAGER_URL + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _readj(path):
    with open(path) as fh:
        return json.load(fh)


def _manager_up():
    try:
        st, _ = _http("/api/agents", timeout=4)
        return st == 200
    except Exception:
        return False


def _orchestrator_running():
    try:
        st, txt = _http("/api/agents", timeout=4)
        if st != 200:
            return False
        for a in json.loads(txt).get("agents", []):
            if a.get("name") == "orchestrator":
                return bool(a.get("running"))
    except Exception:
        return False
    return False


# ===========================================================================
# OFFLINE: Agent-Logik
# ===========================================================================
class AgentLogic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="e2e-agent-")
        cls.a = _load("agent_e2e", AGENT_PATH,
                      {"CLAUDE_WORKDIR": cls.tmp, "OPENROUTER_API_KEY": "dummy"})
        cls.a.report_usage = lambda *a, **k: None   # no network to the manager

    def test_llama_tool_json_500_falls_back_to_no_tools(self):
        """llama.cpp 500 due to broken tool-call JSON -> retry the round without
        tools (text reply) instead of losing the turn."""
        import io, urllib.error
        a = self.a
        calls = []
        good = [b'data: {"choices":[{"delta":{"content":"ok"}}]}\n', b'data: [DONE]\n']
        class FakeResp:
            headers = {}
            def __iter__(self): return iter(good)
        def fake_urlopen(req, *ar, **kw):
            body = req.data.decode()
            has_tools = '"tools"' in body
            calls.append("tools" if has_tools else "notools")
            if has_tools:
                raise urllib.error.HTTPError("http://x", 500, "err", {},
                    io.BytesIO(b'{"error":{"message":"Failed to parse tool call arguments as JSON"}}'))
            return FakeResp()
        saved = (a.urllib.request.urlopen, a._llm_headers, a._llm_url)
        a.urllib.request.urlopen = fake_urlopen
        a._llm_headers = lambda: {"Content-Type": "application/json"}
        a._llm_url = lambda: "http://x/v1/chat/completions"
        toks = []
        try:
            msg = a.or_chat_stream([{"role": "user", "content": "hi"}],
                                   [{"type": "function", "function": {"name": "t", "parameters": {}}}],
                                   toks.append)
        finally:
            (a.urllib.request.urlopen, a._llm_headers, a._llm_url) = saved
        self.assertEqual(calls, ["tools", "notools"])     # first with, then without tools
        self.assertIn("ok", "".join(toks))                # text reply came through
        self.assertEqual(msg["content"], "ok")

    def test_reasoning_content_streamed(self):
        """llama.cpp/Qwen3 sends thinking as reasoning_content — must be streamed;
        content stays the actual reply."""
        a = self.a
        lines = [
            b'data: {"choices":[{"delta":{"role":"assistant","content":null}}]}\n',
            b'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}\n',
            b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n',
            b'data: [DONE]\n',
        ]
        class FakeResp:
            headers = {}
            def __iter__(self): return iter(lines)
        saved = (a.urllib.request.urlopen, a._llm_headers, a._llm_url)
        a.urllib.request.urlopen = lambda *ar, **kw: FakeResp()
        a._llm_headers = lambda: {"Content-Type": "application/json"}
        a._llm_url = lambda: "http://x/v1/chat/completions"
        toks = []
        try:
            msg = a.or_chat_stream([{"role": "user", "content": "hi"}], None, toks.append)
        finally:
            (a.urllib.request.urlopen, a._llm_headers, a._llm_url) = saved
        out = "".join(toks)
        self.assertIn("thinking", out)            # reasoning_content not dropped
        self.assertIn("Hello", out)               # content streamed
        self.assertEqual(msg["content"], "Hello")

    def test_reasoning_only_no_empty_reply(self):
        """Thinking only, no content -> fall back to the thinking instead of an empty reply."""
        a = self.a
        lines = [
            b'data: {"choices":[{"delta":{"reasoning_content":"only thought"}}]}\n',
            b'data: [DONE]\n',
        ]
        class FakeResp:
            headers = {}
            def __iter__(self): return iter(lines)
        saved = (a.urllib.request.urlopen, a._llm_headers, a._llm_url)
        a.urllib.request.urlopen = lambda *ar, **kw: FakeResp()
        a._llm_headers = lambda: {"Content-Type": "application/json"}
        a._llm_url = lambda: "http://x/v1/chat/completions"
        try:
            msg = a.or_chat_stream([{"role": "user", "content": "hi"}], None, lambda t: None)
        finally:
            (a.urllib.request.urlopen, a._llm_headers, a._llm_url) = saved
        self.assertEqual(msg["content"], "only thought")  # no None -> no _(empty reply)_

    def test_steps_unlimited(self):
        """/steps accepts a number 1..x and 'unlimited' (0 = unlimited)."""
        import itertools
        a = self.a; saved = a.MAX_STEPS
        try:
            a._set_steps("/steps 5"); self.assertEqual(a.MAX_STEPS, 5)
            self.assertEqual(list(a._step_iter()), [0, 1, 2, 3, 4])
            a._set_steps("/steps 999"); self.assertEqual(a.MAX_STEPS, 999)   # no more 60-cap
            r = a._set_steps("/steps unlimited")
            self.assertLessEqual(a.MAX_STEPS, 0); self.assertIn("unlimited", r)
            self.assertIsInstance(a._step_iter(), itertools.count)           # unlimited
        finally:
            a.MAX_STEPS = saved

    def test_tool_heartbeat_keeps_stream_alive(self):
        """During a slow tool the stream must send a visible tool-status token
        (🔧) and periodic heartbeats (·), otherwise an idle timeout cuts the
        connection mid-sentence (slow local models)."""
        import time as _t
        a = self.a
        toks = []; calls = {"n": 0}
        def fake_stream(hist, tools, on_token):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "c1", "type": "function",
                     "function": {"name": "shell", "arguments": "{}"}}]}
            on_token("done"); return {"role": "assistant", "content": "done"}
        def fake_exec(name, args):
            _t.sleep(0.15); return "ok"
        saved = (a.or_chat_stream, a.exec_tool, a.HEARTBEAT_SEC, a._drain_steer, a._goal)
        a.or_chat_stream = fake_stream; a.exec_tool = fake_exec
        a.HEARTBEAT_SEC = 0.03; a._drain_steer = lambda *x: False; a._goal = None
        try:
            del a._history[1:]
            a.run_stream("build something", toks.append)
        finally:
            (a.or_chat_stream, a.exec_tool, a.HEARTBEAT_SEC, a._drain_steer, a._goal) = saved
        out = "".join(toks)
        self.assertIn("\U0001f527", out)   # Tool-Status
        self.assertIn("\u00b7", out)        # Heartbeat waehrend Tool-Lauf
        self.assertIn("done", out)           # final reply afterwards

    # --- Backend-Auswahl (Kernstueck orcarouter/llama/openrouter) -----------
    def _select(self, env):
        """Run the backend-selection block from agent.py with env and return
        the resulting variables (isolated, without re-import)."""
        src = open(AGENT_PATH).read()
        block = src[src.index("LLAMA_ENDPOINT = os.environ"):src.index("WORKDIR = os.environ")]
        g = {"os": type("O", (), {"environ": dict(env)})(),
             "OR_URL": "https://openrouter.ai/api/v1/chat/completions",
             "OR_MODEL": env.get("OPENROUTER_MODEL", "openai/gpt-4o")}
        # The block calls os.environ.get(...) -> we need a real mapping.
        g["os"].environ = dict(env)
        exec(block, g)
        return g

    def test_bare_mcp_tool_name_resolves_when_unique(self):
        """gemini dropped the 'mrmusic__' prefix and got 'unknown tool'. A bare
        name resolves when exactly one MCP tool matches; two candidates stay
        unknown; built-ins are untouched. /tools lists the registry."""
        a = self.a
        class Srv:
            def __init__(self): self.calls = []
            def call(self, tool, args): self.calls.append((tool, args)); return "ON"
        old_tools, old_mcp, old_audit = dict(a._mcp_tools), dict(a._mcp), a.audit
        try:
            a.audit = lambda *x, **k: None
            srv = Srv()
            a._mcp_tools.clear(); a._mcp.clear()
            a._mcp_tools["mrmusic__mrmusic_power"] = ("mrmusic", "mrmusic_power")
            a._mcp["mrmusic"] = srv
            self.assertEqual(a._resolve_tool_name("mrmusic_power"), "mrmusic__mrmusic_power")
            self.assertEqual(a._resolve_tool_name("mrmusic__mrmusic_power"), "mrmusic__mrmusic_power")
            self.assertEqual(a._resolve_tool_name("bash"), "bash")
            self.assertEqual(a.exec_tool("mrmusic_power", {"on": True}), "ON")
            self.assertEqual(srv.calls, [("mrmusic_power", {"on": True})])
            a._mcp_tools["other__mrmusic_power"] = ("other", "mrmusic_power")
            self.assertEqual(a._resolve_tool_name("mrmusic_power"), "mrmusic_power")   # ambiguous
            self.assertIn("unknown tool", a.exec_tool("mrmusic_power", {}))
            rep = a._tools_report()
            self.assertIn("mcp (2): mrmusic__mrmusic_power, other__mrmusic_power", rep)
            self.assertIn("built-in (", rep)
        finally:
            a._mcp_tools.clear(); a._mcp_tools.update(old_tools)
            a._mcp.clear(); a._mcp.update(old_mcp)
            a.audit = old_audit

    def test_now_line_gives_the_model_a_clock(self):
        """Every turn carries exactly one [Now] system line in the instance's
        timezone; a second injection replaces, never duplicates."""
        a = self.a
        old_tz = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "Europe/Berlin"
            line = a._now_line()
            self.assertTrue(line.startswith("[Now] "))
            self.assertIn("(Europe/Berlin, UTC+0", line)             # +01:00 or +02:00
            self.assertRegex(line, r"T18:30:00\+0[12]:00")             # tool-input example
            self.assertIn(time.strftime("%Y-%m-%d"), line)   # host and guest tz agree today
            a._history[:] = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"},
                             {"role": "assistant", "content": "Es ist 16:36 Uhr."}]
            a._inject_now(); a._inject_now()
            nows = [m for m in a._history if m["role"] == "system" and m["content"].startswith("[Now]")]
            self.assertEqual(len(nows), 1)
            self.assertEqual(a._history[0]["content"], "sys")
            self.assertTrue(a._history[-1]["content"].startswith("[Now]"))   # last = next to the question
            self.assertIn("outdated", a._history[-1]["content"])
            # playbooks and missions sit next to the question as well (rules at
            # the top were ignored: "12:31 Uhr" against a no-"Uhr" rule)
            old_get = a._mgr_get
            try:
                a._mgr_get = lambda base, path, timeout=30: (
                    json.dumps({"playbooks": [{"text": "keine Uhr"}]}) if "playbooks" in path
                    else json.dumps({"missions": []}))
                a._inject_playbooks(); a._inject_playbooks()
                pbs = [m for m in a._history if m["role"] == "system" and m["content"].startswith(a.PLAYBOOK_TAG)]
                self.assertEqual(len(pbs), 1)
                self.assertTrue(a._history[-1]["content"].startswith(a.PLAYBOOK_TAG))
                self.assertEqual(a._history[0]["content"], "sys")
            finally:
                a._mgr_get = old_get
            os.environ["TZ"] = "Not/AZone"
            self.assertTrue(a._now_line().startswith("[Now] "))   # falls back, never raises
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz

    def test_memory_index_injected_from_folder(self):
        """With MEMORY_DIR the head of MEMORY.md sits in every turn's context
        (one block, refreshed); without a folder nothing is injected."""
        a = self.a
        tmp = tempfile.mkdtemp(prefix="e2e-memidx-")
        open(os.path.join(tmp, "MEMORY.md"), "w").write("# Memory of vc\n\n- [[radio]] Radio — Swiss Classic\n")
        old = a.MEMORY_DIR
        try:
            a.MEMORY_DIR = tmp
            a._history[:] = [{"role": "system", "content": "sys"}]
            a._inject_memory_index(); a._inject_memory_index()
            blocks = [m for m in a._history if m["content"].startswith(a.MEMINDEX_TAG)]
            self.assertEqual(len(blocks), 1)
            self.assertIn("[[radio]]", blocks[0]["content"])
            self.assertIn(tmp, blocks[0]["content"])
            a.MEMORY_DIR = ""
            a._inject_memory_index()
            self.assertEqual([m for m in a._history if m["content"].startswith(a.MEMINDEX_TAG)], [])
        finally:
            a.MEMORY_DIR = old

    def test_spawn_subagent_rides_the_task_path(self):
        """spawn_subagent no longer calls admin routes (403 for guests since
        08-14): it posts create_task target=ephemeral, wait=true, with the
        chosen model, and returns the result text."""
        a = self.a
        calls = []
        old = a._mgr
        try:
            a._mgr = lambda base, path, payload, timeout=30: (calls.append((path, payload, timeout)),
                                                              json.dumps({"ok": True, "result": "42"}))[1]
            self.assertEqual(a.t_spawn_subagent("count things", model="google/gemini-2.5-flash"), "42")
            path, payload, timeout = calls[0]
            self.assertEqual(path, "/api/task")
            self.assertEqual(payload, {"message": "count things", "target": "ephemeral",
                                       "wait": True, "model": "google/gemini-2.5-flash"})
            self.assertGreaterEqual(timeout, 600)
            a._mgr = lambda *x, **k: json.dumps({"error": "target 'ephemeral' not allowed"})
            self.assertIn("not allowed", a.t_spawn_subagent("x"))
            self.assertIn("missing", a.t_spawn_subagent("  "))
            # create_task forwards the model too
            calls.clear(); a._mgr = lambda base, path, payload, timeout=30: (calls.append(payload), json.dumps({"id": "1", "status": "pending", "target": "ephemeral"}))[1]
            a.t_create_task("later", model="m/x")
            self.assertEqual(calls[0]["model"], "m/x")
        finally:
            a._mgr = old

    def test_backend_openrouter_default(self):
        g = self._select({})
        self.assertEqual(g["LLM_BACKEND"], "openrouter")
        self.assertEqual(g["LLM_KEY_SECRET"], "OPENROUTER_API_KEY")

    def test_backend_orcarouter_default_url(self):
        g = self._select({"ORCAROUTER_MODEL": "tencent/hy3"})
        self.assertEqual(g["LLM_BACKEND"], "orcarouter")
        self.assertEqual(g["OR_MODEL"], "tencent/hy3")
        self.assertEqual(g["OR_URL"], "https://api.orcarouter.ai/v1/chat/completions")
        self.assertEqual(g["LLM_KEY_SECRET"], "ORCAROUTER_API_KEY")

    def test_backend_orcarouter_selfhost_url(self):
        g = self._select({"ORCAROUTER_MODEL": "x/y", "ORCAROUTER_URL": "http://localhost:8000/v1"})
        self.assertEqual(g["OR_URL"], "http://localhost:8000/v1/chat/completions")

    def test_backend_llama_wins(self):
        g = self._select({"LLAMA_ENDPOINT": "http://h:8080/v1", "LLAMA_MODEL": "qwen",
                          "ORCAROUTER_MODEL": "should-be-ignored"})
        self.assertEqual(g["LLM_BACKEND"], "llama")
        self.assertEqual(g["OR_MODEL"], "qwen")

    # --- Context-Offloader --------------------------------------------------
    def test_offload_roundtrip(self):
        big = "X" * 20000
        out = self.a._finalize_output("http_fetch", big)
        self.assertLess(len(out), len(big))
        self.assertIn('offload_read(id="', out)
        import re
        oid = re.search(r'id="([^"]+)"', out).group(1)
        back = self.a.t_offload_read(id=oid, offset=0, length=25000)
        self.assertIn("XXXX", back)
        self.assertGreaterEqual(len(back), 19000)

    def test_offload_small_passthrough(self):
        self.assertEqual(self.a._finalize_output("bash", "kurz"), "kurz")

    def test_offload_read_missing(self):
        self.assertIn("not found", self.a.t_offload_read(id="gibtsnicht"))

    def test_offload_read_always_enabled(self):
        old = self.a._TOOL_ALLOW
        try:
            self.a._TOOL_ALLOW = {"bash"}          # strikte Allowlist
            self.assertTrue(self.a.tool_enabled("offload_read"))
            self.assertFalse(self.a.tool_enabled("http_fetch"))
        finally:
            self.a._TOOL_ALLOW = old

    # --- Tool-Hook / Guardrails ---------------------------------------------
    def test_http_fetch_html_becomes_readable_text(self):
        """A modern page is 90% markup — hard-truncated raw HTML cut content
        off before it appeared and the model called pages "too complex". The
        conversion must drop scripts/tags, resolve entities, keep link targets."""
        a = self.a
        html = ("<html><head><title>x</title><script>var a=1;</script>"
                "<style>.x{}</style></head><body>\r\n"
                "<div class='nav'><a href='https://firma.de/jobs'>Karriere</a></div>"
                "<h1>Die gr&ouml;&szlig;ten Medizintechnik-Firmen</h1>"
                "<ul><li>Alpha GmbH &amp; Co.</li><li>Beta AG</li></ul>"
                "<p>Umsatz: 3&nbsp;Mio.</p></body></html>")
        t = a._html_to_text(html)
        self.assertNotIn("<", t)                       # no tags survive
        self.assertNotIn("var a=1", t)                 # scripts gone
        self.assertIn("Die größten Medizintechnik-Firmen", t)
        self.assertIn("Alpha GmbH & Co.", t)
        self.assertIn("Beta AG", t)
        self.assertIn("Karriere [https://firma.de/jobs]", t)   # link target kept
        self.assertNotIn("\n\n\n", t)                 # no blank-line runs

    def test_web_search_reports_blocked_backends_instead_of_no_results(self):
        """DDG went behind a bot challenge (HTTP 202 + anomaly page); the old
        tool turned that into "no results" and the model concluded the thing
        searched for does not exist. A dead backend must be NAMED."""
        a = self.a
        old_d, old_b = a._ddg_search, a._bing_search
        try:
            a._ddg_search = lambda q, c: None                # challenge
            a._bing_search = lambda q, c: (_ for _ in ()).throw(OSError("net down"))
            out = a.t_web_search("anything")
            self.assertIn("unavailable", out)
            self.assertIn("duckduckgo: blocked", out)
            self.assertIn("bing", out)
            self.assertIn("NOT an empty result", out)
            # A backend that answers with an EMPTY list is a real empty result.
            a._ddg_search = lambda q, c: []
            self.assertEqual(a.t_web_search("gibberishquery"), "no results")
            # And the fallback chain: DDG blocked, Bing delivers.
            a._ddg_search = lambda q, c: None
            a._bing_search = lambda q, c: [("Titel", "https://x.de", "Schnipsel")]
            out = a.t_web_search("x")
            self.assertIn("Titel", out)
            self.assertIn("https://x.de", out)
        finally:
            a._ddg_search, a._bing_search = old_d, old_b

    def test_bing_redirect_urls_are_decoded(self):
        import base64
        a = self.a
        target = "https://de.wikipedia.org/wiki/Unternehmen"
        b64 = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
        href = f"https://www.bing.com/ck/a?!&amp;&amp;p=xyz&amp;u=a1{b64}&amp;ntb=1"
        self.assertEqual(a._bing_real_url(href), target)
        # Without the redirect wrapper the URL passes through untouched.
        self.assertEqual(a._bing_real_url("https://example.org/x"), "https://example.org/x")

    def test_rejected_history_image_is_stripped_and_counted(self):
        """A provider that rejects an image the history has long carried kills
        EVERY later turn (found live: an instance whose memory stayed empty
        because no turn ever reached the tools). The strip helper must remove
        exactly the image parts and leave the text."""
        a = self.a
        hist = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": [
                {"type": "text", "text": "was ist das?"},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
            ]},
            {"role": "assistant", "content": "eine Katze"},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,BBBB"}},
            ]},
        ]
        n = a._strip_history_images(hist)
        self.assertEqual(n, 2)
        flat = __import__("json").dumps(hist)
        self.assertNotIn("image_url", flat)             # no image parts left
        self.assertIn("was ist das?", flat)             # text survives
        self.assertIn("image removed", flat)
        self.assertEqual(a._strip_history_images(hist), 0)   # idempotent

    def test_offload_preview_outlines_json(self):
        """A head-slice of a big JSON is an unclosed brace of the first record.
        The preview should say WHAT the payload is instead."""
        a = self.a
        rows = [{"id": i, "name": f"row {i}", "value": i * 3.14} for i in range(500)]
        out = __import__("json").dumps({"total": 500, "rows": rows})
        assert len(out) > a.OFFLOAD_MIN, "test payload must trigger offloading"
        got = a._finalize_output("http_fetch", out)
        self.assertIn("[JSON structure]", got)
        self.assertIn("rows", got)                     # the key survives
        self.assertIn("500 items", got)                # the count survives
        self.assertIn("offload_read", got)             # the full text is reachable
        self.assertLess(len(got), a.OFFLOAD_PREVIEW + 400)

    def test_offload_preview_folds_logs_and_keeps_errors(self):
        a = self.a
        noise = "GET /health 200 0.001s"
        lines = [noise] * 800 + ["ERROR: db connection refused"] + [noise] * 800
        out = "\n".join(lines)
        assert len(out) > a.OFFLOAD_MIN
        got = a._finalize_output("bash", out)
        self.assertIn("repeats ×800", got)             # duplicates folded
        self.assertIn("ERROR: db connection refused", got)   # the problem survives
        self.assertIn("1601 lines", got)
        self.assertLess(len(got), a.OFFLOAD_PREVIEW + 400)

    def test_offload_preview_plain_text_stays_head_slice(self):
        a = self.a
        out = ("word " * 20000).strip()
        got = a._finalize_output("read_file", out)
        self.assertTrue(got.startswith("word word"))
        self.assertIn("offload_read", got)

    def test_list_skills_names_only_without_query(self):
        """67 skills × full description cost ~2.5k tokens per call — the bare
        list must stay cheap, the descriptions come via query."""
        a = self.a
        catalog = __import__("json").dumps([
            {"name": "docker", "description": "Docker expert for containers"},
            {"name": "kubernetes", "description": "K8s operations expert"},
            {"name": "git-expert", "description": "Git operations expert"},
        ])
        old = a._mgr_get
        try:
            a._mgr_get = lambda base, path, **k: catalog
            bare = a.t_list_skills()
            self.assertIn("docker", bare)
            self.assertNotIn("containers", bare)       # no descriptions
            hit = a.t_list_skills(query="container")
            self.assertIn("Docker expert", hit)        # description on demand
            self.assertNotIn("git-expert", hit)
            miss = a.t_list_skills(query="quantum")
            self.assertIn("No skill matches", miss)
        finally:
            a._mgr_get = old

    def test_hook_denylist_blocks_rmrf(self):
        allow, reason = self.a._hook_before_tool("bash", {"command": "sudo rm -rf / --no-preserve-root"})
        self.assertFalse(allow)
        self.assertIn("rm -rf /", reason)

    def test_hook_denylist_blocks_forkbomb(self):
        allow, _ = self.a._hook_before_tool("bash", {"command": ":(){:|:&};:"})
        self.assertFalse(allow)

    def test_hook_allows_normal(self):
        allow, _ = self.a._hook_before_tool("bash", {"command": "ls -la"})
        self.assertTrue(allow)

    def test_hitl_default_off(self):
        self.assertFalse(self.a.HITL)   # opt-in, otherwise it blocks nothing

    def test_notify_tool_registered(self):
        self.assertIn("notify", self.a.BUILTIN)

    # --- /model: Laufzeit-Modellwechsel --------------------------------------
    def test_model_switch(self):
        a = self.a
        old = (a.OR_MODEL, a.OR_URL, a.LLM_NAME, a.LLM_KEY_SECRET, a.LLM_BACKEND, a.OR_KEY)
        try:
            self.assertIn("Model:", a._set_model("/model"))
            a._set_model("/model orcarouter:foo/bar")
            self.assertEqual(a.OR_MODEL, "foo/bar")
            self.assertIn("orcarouter.ai", a.OR_URL)
            self.assertEqual(a.LLM_KEY_SECRET, "ORCAROUTER_API_KEY")
            a._set_model("/model only-model-id")           # without provider: model only
            self.assertEqual(a.OR_MODEL, "only-model-id")
            self.assertIn("orcarouter.ai", a.OR_URL)       # Backend unveraendert
        finally:
            (a.OR_MODEL, a.OR_URL, a.LLM_NAME, a.LLM_KEY_SECRET, a.LLM_BACKEND, a.OR_KEY) = old

    # --- Steering -------------------------------------------------------------
    def test_steering_queue(self):
        a = self.a
        self.assertFalse(a.steer_push("x"))               # idle -> reject
        a._busy[0] = True
        try:
            self.assertTrue(a.steer_push("hold course"))
            hist = []
            self.assertTrue(a._drain_steer(hist))
            self.assertEqual(hist[0]["role"], "user")
            self.assertIn("hold course", hist[0]["content"])
            self.assertIn("[Steering", hist[0]["content"])
            self.assertFalse(a._drain_steer(hist))         # queue empty
        finally:
            a._busy[0] = False

    # --- Prompt-Templates -----------------------------------------------------
    def test_prompt_expansion(self):
        a = self.a
        a._prompts_cache["map"] = {"daily": "Write the daily briefing."}
        a._prompts_cache["ts"] = __import__("time").time()
        self.assertEqual(a._expand_prompt("/daily"), "Write the daily briefing.")
        self.assertEqual(a._expand_prompt("/daily just short"),
                         "Write the daily briefing. just short")
        self.assertEqual(a._expand_prompt("/reset"), "/reset")     # built-in takes precedence
        self.assertEqual(a._expand_prompt("/gibtsnicht"), "/gibtsnicht")
        self.assertEqual(a._expand_prompt("normal text"), "normal text")

    # --- Plugin-Loader ----------------------------------------------------------
    def test_plugin_loader(self):
        a = self.a
        tmp = tempfile.mkdtemp(prefix="e2e-plug-")
        with open(os.path.join(tmp, "echoplug.py"), "w") as fh:
            fh.write('DESC="Echo"\nPARAMS={"t":{"type":"string"}}\nREQUIRED=["t"]\n'
                     'def run(t):\n    return "ECHO:" + t\n')
        with open(os.path.join(tmp, "bash.py"), "w") as fh:      # collision -> ignore
            fh.write('DESC="evil"\ndef run():\n    return "no"\n')
        old_dir = a.PLUGIN_DIR
        try:
            a.PLUGIN_DIR = tmp
            a.load_plugins()
            self.assertIn("echoplug", a.BUILTIN)
            self.assertIn("echoplug", a.PLUGIN_TOOLS)
            self.assertEqual(a.BUILTIN["echoplug"][0]("hi"), "ECHO:hi")
            self.assertNotIn("bash", a.PLUGIN_TOOLS)              # collision blocked
        finally:
            a.PLUGIN_DIR = old_dir
            a.BUILTIN.pop("echoplug", None)
            a.PLUGIN_TOOLS.discard("echoplug")

    def test_turn_deadline_and_per_turn_steps(self):
        """A turn with a deadline stops calling tools before it runs out and
        answers with what it has; '/steps N <text>' (alias /maxSteps) caps the
        steps for that turn only."""
        a = self.a
        old = a.or_chat, a.exec_tool, a.MAX_STEPS, a._deadline[0], list(a._history)
        calls = []
        try:
            def chat(msgs, tools, model=None):
                final = msgs[-1].get("content") == a._DEADLINE_NOTE
                calls.append(not final)
                if not final:
                    return {"role": "assistant", "content": None,
                            "tool_calls": [{"id": "1", "function": {"name": "web_search", "arguments": "{}"}}]}
                return {"role": "assistant", "content": "partial: 2 hits so far"}
            a.or_chat = chat
            a.exec_tool = lambda name, args: "hit"
            hist = [{"role": "system", "content": "s"}, {"role": "user", "content": "search"}]
            a._deadline[0] = time.time() + a.DEADLINE_MARGIN - 1          # already inside the margin
            out = a._tool_loop(hist)
            self.assertIn("partial: 2 hits", out)
            self.assertIn("time budget", out)
            self.assertEqual(calls, [False])                                # one call, tools off
            self.assertTrue(any(a._DEADLINE_NOTE in str(m.get("content")) for m in hist))
            # a deadline far away: the loop runs its steps as usual
            calls.clear(); a._deadline[0] = time.time() + 3600; a.MAX_STEPS = 2
            self.assertEqual(a._tool_loop([{"role": "user", "content": "x"}]), "(max tool steps reached)")
            self.assertEqual(calls, [True, True])
            # /steps N <text> for one turn, /maxSteps alias, then the old cap again
            a.MAX_STEPS = 12
            self.assertEqual(a._turn_steps("/maxSteps 100 Tägliche Jobsuche"), (100, "Tägliche Jobsuche"))
            self.assertEqual(a._turn_steps("/steps unlimited go"), (0, "go"))
            self.assertIsNone(a._turn_steps("/steps 30"))                   # the setter, not a turn
            self.assertIsNone(a._turn_steps("Jobsuche /steps 3"))
            seen = []
            old_loop = a._tool_loop
            a._tool_loop = lambda hist: (seen.append(a.MAX_STEPS), "ok")[1]
            try:
                self.assertEqual(a.run("/steps 3 hallo", deadline=0), "ok")
            finally:
                a._tool_loop = old_loop
            self.assertEqual(seen, [3])
            self.assertEqual(a.MAX_STEPS, 12)
        finally:
            a.or_chat, a.exec_tool, a.MAX_STEPS, a._deadline[0] = old[:4]
            a._history[:] = old[4]

    def test_turn_markers_and_spans(self):
        """A turn posts start/end markers (kind, steps, ms, outcome), every LLM
        call a usage span with turn/step/ms, every tool call an audit line with
        ms; slash commands produce no trace."""
        a = self.a
        posts = []
        old = a._mgr, a.or_chat, a.exec_tool, a.report_usage, list(a._history), a.MAX_STEPS, a.BUILTIN.get("web_search")
        try:
            a._mgr = lambda base, path, payload=None, timeout=60: posts.append((path, payload))
            a.report_usage = lambda u, ms=None, ok=True, err="": posts.append(("/api/usage", {"turn": a._turn_id[0], "step": a._turn_step[0], "ms": ms, "ok": ok, "err": err}))
            calls = [0]
            def chat(msgs, tools, model=None):
                calls[0] += 1
                a._turn_step[0] += 1; a.report_usage({}, ms=5)      # what or_chat does
                if calls[0] == 1:
                    return {"role": "assistant", "content": None,
                            "tool_calls": [{"id": "1", "function": {"name": "web_search", "arguments": "{}"}}]}
                return {"role": "assistant", "content": "done"}
            a.or_chat = chat
            a.BUILTIN["web_search"] = (lambda **kw: "1. hit", {}, [])
            a.MAX_STEPS = 5
            self.assertEqual(a.run("/fresh find it", kind="task", turn="abc12345"), "done")
            self.assertTrue(all(b["turn"] == "abc12345" for p, b in posts if p == "/api/trace"))   # named by the bridge
            kinds = [(p, b.get("event")) for p, b in posts if p == "/api/trace"]
            self.assertEqual(kinds, [("/api/trace", "start"), ("/api/trace", "end")])
            end = next(b for p, b in posts if p == "/api/trace" and b["event"] == "end")
            self.assertEqual((end["kind"], end["steps"], end["outcome"]), ("fresh", 2, "ok"))
            self.assertGreaterEqual(end["ms"], 0)
            turn = end["turn"]
            usage = [b for p, b in posts if p == "/api/usage"]
            self.assertEqual([u["step"] for u in usage], [1, 2])
            self.assertTrue(all(u["turn"] == turn for u in usage))
            audits = [b for p, b in posts if p == "/api/audit"]
            self.assertEqual(len(audits), 1)
            self.assertEqual(audits[0]["turn"], turn)
            self.assertIn("ms", audits[0])
            posts.clear()
            a.run("/steps")                                       # a slash command: no markers
            self.assertEqual([p for p, _ in posts if p == "/api/trace"], [])
            self.assertEqual(a._outcome_of("(max tool steps reached)"), "max_steps")
            self.assertEqual(a._outcome_of("x ⏱️ (time budget exhausted — partial result)"), "deadline")
            self.assertEqual(a._outcome_of("⚠️ LLM HTTP 500"), "error")
        finally:
            a._mgr, a.or_chat, a.exec_tool, a.report_usage = old[:4]
            a._history[:] = old[4]; a.MAX_STEPS = old[5]
            if old[6] is not None:
                a.BUILTIN["web_search"] = old[6]
            else:
                a.BUILTIN.pop("web_search", None)

    def test_skill_distilled_after_a_long_successful_turn(self):
        """After a turn with enough model calls that ended well, one extra
        model call distills a SKILL proposal and files it; NONE files
        nothing; short turns, failed turns and slash commands never trigger."""
        a = self.a
        posts = []
        old = a.or_chat, a._mgr, a.SKILL_LEARN, a.SKILL_LEARN_MIN_STEPS, a._turn_step[0], a.threading.Thread

        class SyncThread:
            def __init__(self, target=None, args=(), daemon=None): self.t, self.a = target, args
            def start(self): self.t(*self.a)
        try:
            a._mgr = lambda base, path, payload=None, timeout=60: posts.append((path, payload)) or "ok"
            a.threading.Thread = SyncThread
            a.SKILL_LEARN, a.SKILL_LEARN_MIN_STEPS = True, 3
            hist = [{"role": "system", "content": "s"}, {"role": "user", "content": "old"},
                    {"role": "assistant", "content": "old answer"},
                    {"role": "user", "content": "find jobs"},
                    {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "function": {"name": "web_search", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "1", "content": "x" * 5000},
                    {"role": "assistant", "content": "done"}]
            sl = a._turn_slice(hist, "find jobs")
            self.assertEqual(sl[0]["content"], "find jobs"); self.assertEqual(len(sl), 4)
            self.assertEqual(len(sl[2]["content"]), 1500)                       # tool output trimmed
            a.or_chat = lambda msgs, tools, model=None: {"role": "assistant", "content":
                '```json\n{"name": "job-search", "description": "Daily search", "content": "# Purpose\\n..."}\n```'}
            a._turn_step[0] = 4
            self.assertTrue(a._maybe_learn(hist, "find jobs", "ok"))
            self.assertEqual(posts[-1][0], "/api/skill-proposals")
            self.assertEqual(posts[-1][1]["name"], "job-search")
            self.assertIn("find jobs", posts[-1][1]["note"])
            posts.clear()
            a.or_chat = lambda msgs, tools, model=None: {"role": "assistant", "content": "NONE"}
            self.assertTrue(a._maybe_learn(hist, "find jobs", "ok")); self.assertEqual(posts, [])
            a._turn_step[0] = 2
            self.assertFalse(a._maybe_learn(hist, "find jobs", "ok"))              # too short
            a._turn_step[0] = 9
            self.assertFalse(a._maybe_learn(hist, "find jobs", "max_steps"))       # did not end well
            self.assertFalse(a._maybe_learn(hist, "/fresh x", "ok"))               # slash command
            a.SKILL_LEARN = False
            self.assertFalse(a._maybe_learn(hist, "find jobs", "ok"))
        finally:
            a.or_chat, a._mgr, a.SKILL_LEARN, a.SKILL_LEARN_MIN_STEPS, a._turn_step[0], a.threading.Thread = old

    # --- Tree-Chat: /branch + /back -------------------------------------------
    def test_branch_and_back(self):
        a = self.a
        old_hist = list(a._history)
        old_chat = a.or_chat
        try:
            a._history[:] = [{"role": "system", "content": "s"},
                             {"role": "user", "content": "main topic"}]
            a.or_chat = lambda msgs, tools, model=None: {"role": "assistant",
                                                         "content": "essence of the follow-up"}
            out = a._branch_open("/branch piper")
            self.assertIn("depth 1", out)
            self.assertEqual(a._branch_depth(), 1)
            a._history.append({"role": "user", "content": "follow-up?"})
            a._history.append({"role": "assistant", "content": "reply in the branch"})
            out = a._branch_close("/back")
            self.assertEqual(a._branch_depth(), 0)
            self.assertIn("main topic", out)
            # branch content gone, sidenote present, origin intact
            joined = " | ".join(str(m.get("content")) for m in a._history)
            self.assertNotIn("reply in the branch", joined)
            self.assertIn(a.NOTE_TAG, joined)
            self.assertIn("main topic", joined)
            # /back without a branch
            self.assertIn("No open", a._branch_close("/back"))
        finally:
            a.or_chat = old_chat
            a._history[:] = old_hist

    def test_branch_drop(self):
        a = self.a
        old_hist = list(a._history)
        try:
            a._history[:] = [{"role": "system", "content": "s"}]
            a._branch_open("/branch x")
            a._history.append({"role": "user", "content": "geheim"})
            a._branch_close("/back drop")
            joined = " | ".join(str(m.get("content")) for m in a._history)
            self.assertNotIn("geheim", joined)
            self.assertNotIn(a.NOTE_TAG, joined)   # spurlos
        finally:
            a._history[:] = old_hist

    # --- Goal-Kommando ------------------------------------------------------
    def test_goal_set_show_off(self):
        try:
            self.assertIn("No goal", self.a._set_goal("/goal show"))
            self.a._set_goal("/goal Antworte knapp.")
            self.assertEqual(self.a._goal, "Antworte knapp.")
            self.a._set_goal("/goal off")
            self.assertIsNone(self.a._goal)
        finally:
            self.a._goal = None

    # --- Summarizing conversation manager -----------------------------------
    def test_local_model_crash_on_image_is_not_retried_and_images_leave_history(self):
        """llama.cpp closed the connection on every image (a crash, then a
        model reload): no retry queue behind it, the image parts leave the
        history so the next turn cannot trigger it again, the user gets one
        clear line. A 503 while the model loads is answered the same way."""
        import http.client, urllib.error
        a = self.a
        old = a.LLAMA_ENDPOINT, a.urllib.request.urlopen, a._retry_sleep
        slept = []
        try:
            a.LLAMA_ENDPOINT = "http://10.0.0.5:8080/"
            a._retry_sleep = lambda n: slept.append(n)
            msgs = [{"role": "system", "content": "S"},
                    {"role": "user", "content": [{"type": "text", "text": "was siehst du?"},
                                                 {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}]}]
            def drop(req, timeout=None):
                raise http.client.RemoteDisconnected("Remote end closed connection without response")
            a.urllib.request.urlopen = drop
            r = a.or_chat(msgs, [])
            self.assertIn("image was removed", r["content"])
            self.assertEqual(slept, [])                                              # no retry
            self.assertFalse(any(isinstance(p, dict) and p.get("type") == "image_url"
                                 for m in msgs if isinstance(m.get("content"), list) for p in m["content"]))
            r = a.or_chat(msgs, [])
            self.assertIn("probably restarting", r["content"])                      # no image any more
            def loading(req, timeout=None):
                raise urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", {}, io.BytesIO(b'{"error":{"message":"Loading model"}}'))
            a.urllib.request.urlopen = loading
            r = a.or_chat(msgs, [])
            self.assertIn("loading its model", r["content"])
            self.assertEqual(r.get("role"), "assistant")   # the error reply lands in the history: it needs a role (llama.cpp 500 "Missing 'role'")
            self.assertEqual(slept, [])
        finally:
            a.LLAMA_ENDPOINT, a.urllib.request.urlopen, a._retry_sleep = old

    def test_local_model_empty_stream_after_image_counts_as_dropped(self):
        """The streaming variant of the crash: llama.cpp sends the 200 headers,
        dies on the image, the stream ends with nothing — that was shown as
        "(empty reply)" and the image stayed in the history."""
        import io
        a = self.a
        old = a.LLAMA_ENDPOINT, a.urllib.request.urlopen, a.report_usage
        reported = []
        try:
            a.LLAMA_ENDPOINT = "http://10.0.0.5:8080/"
            a.report_usage = lambda u, ms=None, ok=True, err="": reported.append((ok, err))
            a.urllib.request.urlopen = lambda req, timeout=None: io.BytesIO(b"")     # 200, then nothing
            msgs = [{"role": "system", "content": "S"},
                    {"role": "user", "content": [{"type": "text", "text": "was siehst du?"},
                                                 {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}]}]
            out = []
            r = a.or_chat_stream(msgs, [], out.append)
            self.assertIn("image was removed", r["content"])
            self.assertIn("image was removed", "".join(out))
            self.assertEqual(reported, [(False, r["content"])])
            self.assertNotIn("image_url", json.dumps(msgs))
        finally:
            a.LLAMA_ENDPOINT, a.urllib.request.urlopen, a.report_usage = old

    def test_spawn_subagent_passes_the_sandbox(self):
        """The tool sends tools/egress/skill as one `sandbox` object, and none
        when nothing narrows."""
        a = self.a
        seen = []
        old = a._mgr
        try:
            a._mgr = lambda base, path, payload=None, timeout=60: seen.append((path, payload)) or '{"result": "done"}'
            self.assertEqual(a.t_spawn_subagent("do it"), "done")
            self.assertNotIn("sandbox", seen[-1][1])
            a.t_spawn_subagent("do it", tools="bash,read_file", egress="none", skill="pdf-digest")
            self.assertEqual(seen[-1][1]["sandbox"], {"tools": "bash,read_file", "egress": "none", "skill": "pdf-digest"})
            self.assertEqual(seen[-1][1]["target"], "ephemeral")
        finally:
            a._mgr = old

    def test_auto_reset_after_idle_minutes(self):
        """AUTO_RESET_MIN: a context that idled longer than that starts over at
        the next turn; a recent turn, a fresh context or 0 leave it alone."""
        a = self.a
        old = a.AUTO_RESET_MIN, a._last_turn[0], list(a._history)
        try:
            a._history[:] = [{"role": "system", "content": "S"}, {"role": "user", "content": "u"}, {"role": "assistant", "content": "a"}]
            a.AUTO_RESET_MIN = 0; a._last_turn[0] = time.time() - 3600
            self.assertFalse(a._auto_reset()); self.assertEqual(len(a._history), 3)
            a.AUTO_RESET_MIN = 30; a._last_turn[0] = time.time() - 600
            self.assertFalse(a._auto_reset()); self.assertEqual(len(a._history), 3)      # 10 min idle: keep
            a._last_turn[0] = time.time() - 3600
            self.assertTrue(a._auto_reset()); self.assertEqual(len(a._history), 1)       # 60 min idle: fresh
            a._last_turn[0] = time.time() - 3600
            self.assertFalse(a._auto_reset())                                             # already fresh: nothing to drop
            a._last_turn[0] = 0.0; a._history.append({"role": "user", "content": "u"})
            self.assertFalse(a._auto_reset())                                             # first turn after boot: keep
        finally:
            a.AUTO_RESET_MIN, a._last_turn[0] = old[0], old[1]; a._history[:] = old[2]

    def test_llm_timeouts_and_retry_policy_for_local_models(self):
        """A local model gets long timeouts and no retry after a timeout (it is
        still busy with the request that timed out); cloud backends keep the
        short timeouts and retry."""
        a = self.a
        old = a.LLAMA_ENDPOINT
        try:
            self.assertGreaterEqual(a.LLM_STREAM_TIMEOUT, a.LLM_TIMEOUT)
            a.LLAMA_ENDPOINT = "http://10.0.0.5:8080/"
            self.assertFalse(a._retry_after(TimeoutError("timed out"), 0))
            self.assertFalse(a._retry_after(OSError("The read operation timed out"), 0))
            self.assertTrue(a._retry_after(OSError("connection reset"), 0))
            a.LLAMA_ENDPOINT = ""
            self.assertTrue(a._retry_after(TimeoutError("timed out"), 0))
            self.assertFalse(a._retry_after(TimeoutError("timed out"), a.LLM_RETRIES))
        finally:
            a.LLAMA_ENDPOINT = old

    def test_wire_messages_never_leaves_adjacent_assistant(self):
        """The goal loop appends assistant answers with a note between them;
        folding the notes for a local model must not leave two assistant
        messages adjacent (Qwen: HTTP 400 "2 or more assistant messages")."""
        a = self.a
        old = a.FOLD_SYSTEM
        try:
            a.FOLD_SYSTEM = True
            msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "u"},
                    {"role": "assistant", "content": "first"},
                    {"role": "system", "content": "critique"},          # folded away
                    {"role": "assistant", "content": "second"}]
            w = a._wire_messages(msgs)
            roles = [m["role"] for m in w]
            self.assertEqual(roles, ["system", "user", "assistant"])     # the two answers merged
            self.assertIn("first", w[-1]["content"]); self.assertIn("second", w[-1]["content"])
            # a tool-call assistant is never merged away
            msgs2 = [{"role": "system", "content": "S"}, {"role": "user", "content": "u"},
                     {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
                     {"role": "tool", "tool_call_id": "1", "content": "r"},
                     {"role": "assistant", "content": "done"}]
            self.assertEqual([m["role"] for m in a._wire_messages(msgs2)],
                             ["system", "user", "assistant", "tool", "assistant"])
        finally:
            a.FOLD_SYSTEM = old

    def test_recall_dedups_and_budgets(self):
        """A-3: a recall hit already in the memory index is dropped, exact
        duplicates are dropped, and the block is capped by RECALL_MAX_CHARS."""
        a = self.a
        old = a._mgr, list(a._history), a.MEMORY_DIR, a.RECALL_MAX_CHARS
        try:
            a.MEMORY_DIR = ""
            a.RECALL_MAX_CHARS = 60
            hits = [{"text": "Ulrich bikes to work", "score": 0.9},
                    {"text": "Ulrich bikes to work", "score": 0.9},
                    {"text": "likes Radio Lauder in the mornings", "score": 0.9},
                    {"text": "a third distinct fact that pushes over the budget", "score": 0.9}]
            a._mgr = lambda base, path, payload=None, timeout=60: json.dumps({"hits": hits})
            a._history[:] = [{"role": "system", "content": a.SYSTEM},
                             {"role": "system", "content": a.MEMINDEX_TAG + " index: Ulrich bikes to work"}]
            a._recall("how does he commute?")
            block = [mm for mm in a._history if str(mm.get("content","")).startswith(a.RECALL_TAG)]
            self.assertEqual(len(block), 1)
            txt = block[0]["content"]
            self.assertNotIn("bikes to work", txt)
            self.assertIn("Radio Lauder", txt)
            self.assertNotIn("third distinct fact", txt)
            self.assertEqual(txt.count("- "), 1)
        finally:
            a._mgr, a.MEMORY_DIR, a.RECALL_MAX_CHARS = old[0], old[2], old[3]
            a._history[:] = old[1]

    def test_maybe_learn_gated_and_visible(self):
        """A-4: skill-learning fires only on a long successful non-slash turn,
        is off when SKILL_LEARN is false, and logs when it fires (visibility)."""
        a = self.a
        old = a.SKILL_LEARN, a.SKILL_LEARN_MIN_STEPS, a._turn_step[0], a.threading, a.log
        started = []; logged = []
        class _T:
            def __init__(self, target=None, args=(), daemon=None): pass
            def start(self): started.append(1)
        try:
            a.threading = type("x", (), {"Thread": _T})
            a.log = lambda *m, **k: logged.append(" ".join(str(x) for x in m))
            a.SKILL_LEARN_MIN_STEPS = 5
            a.SKILL_LEARN = True; a._turn_step[0] = 6
            self.assertTrue(a._maybe_learn([], "do a multi-step job", "ok")); self.assertEqual(len(started), 1)
            self.assertTrue(any("skill-learn" in x for x in logged))
            started.clear()
            self.assertFalse(a._maybe_learn([], "/reset", "ok")); self.assertEqual(started, [])   # slash: no
            self.assertFalse(a._maybe_learn([], "x", "error")); self.assertEqual(started, [])      # bad outcome: no
            a._turn_step[0] = 2
            self.assertFalse(a._maybe_learn([], "x", "ok")); self.assertEqual(started, [])         # too short: no
            a._turn_step[0] = 6; a.SKILL_LEARN = False
            self.assertFalse(a._maybe_learn([], "x", "ok")); self.assertEqual(started, [])         # disabled: no
        finally:
            a.SKILL_LEARN, a.SKILL_LEARN_MIN_STEPS, a._turn_step[0], a.threading, a.log = old

    def test_office_tools_helpers_and_missing_libs(self):
        """write_xlsx/write_docx: row normalisation (lists, objects, JSON) and
        the Markdown-subset parser are pure; without the libs the tools say so
        instead of crashing (the libs live in the rootfs, not on the host)."""
        a = self.a
        self.assertEqual(a._rows_norm([["a", "b"], [1, 2]]), [["a", "b"], [1, 2]])
        self.assertEqual(a._rows_norm([{"firma": "X", "ort": "K"}, {"firma": "Y", "link": "u"}]),
                         [["firma", "ort", "link"], ["X", "K", ""], ["Y", "", "u"]])
        self.assertEqual(a._rows_norm('[["h"],["v"]]'), [["h"], ["v"]])
        with self.assertRaises(ValueError):
            a._rows_norm({"not": "a list"})
        blocks = a._md_blocks("# Titel\n\nErster Absatz\nzweite Zeile\n\n- eins\n- **zwei**\n\n## Sub\nText")
        self.assertEqual(blocks, [("h", 1, "Titel"), ("p", "Erster Absatz zweite Zeile"),
                                  ("li", "eins"), ("li", "**zwei**"), ("h", 2, "Sub"), ("p", "Text")])
        import importlib
        have_x = importlib.util.find_spec("openpyxl") is not None
        have_d = importlib.util.find_spec("docx") is not None
        if not have_x:
            self.assertIn("openpyxl", a.t_write_xlsx("t.xlsx", [["a"]]))
        if not have_d:
            self.assertIn("python-docx", a.t_write_docx("t.docx", "# x"))
        self.assertIn("write_xlsx", a.BUILTIN); self.assertIn("write_docx", a.BUILTIN)   # registered tools

    def test_reset_clears_the_goal(self):
        """/reset drops a stale goal; otherwise every later turn runs the goal
        loop (which is what broke the uncensored instance)."""
        a = self.a
        old = a._goal
        try:
            a._goal = "write a file to the host"
            out = []
            a.run_stream("/reset", out.append)
            self.assertIsNone(a._goal)
            self.assertIn("reset", "".join(out).lower())
        finally:
            a._goal = old

    def test_wire_strict_alternation_invariant(self):
        """A-5: after wiring for a local model, no two adjacent messages share a
        role (user OR assistant), and tool_calls/tool sequences are untouched."""
        a = self.a
        old = a.FOLD_SYSTEM
        try:
            a.FOLD_SYSTEM = True
            msgs = [{"role": "system", "content": "S"},
                    {"role": "user", "content": "u1"},
                    {"role": "user", "content": "u2 (steer)"},          # two users -> must merge
                    {"role": "assistant", "content": "a1"},
                    {"role": "system", "content": "note"},              # folded away
                    {"role": "assistant", "content": "a2"},             # two assistants -> merge
                    {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
                    {"role": "tool", "tool_call_id": "1", "content": "r"},
                    {"role": "assistant", "content": "done"}]
            w = a._wire_messages(msgs)
            roles = [m["role"] for m in w]
            for i in range(1, len(roles)):
                self.assertFalse(roles[i] == roles[i-1] and roles[i] in ("user", "assistant")
                                 and not w[i].get("tool_calls") and not w[i-1].get("tool_calls"),
                                 f"adjacent {roles[i]} at {i}: {roles}")
            self.assertIn("u1", w[1]["content"]); self.assertIn("u2", w[1]["content"])
            self.assertEqual(roles.count("tool"), 1)                    # tool sequence intact
            self.assertEqual(roles[-1], "assistant"); self.assertEqual(w[-1]["content"], "done")
        finally:
            a.FOLD_SYSTEM = old

    def test_wire_messages_folds_system_notes_for_local_models(self):
        """Qwen3's chat template in llama.cpp rejects a system message that is
        not the first one; the agent's [Memory]/[Playbooks]/date notes are
        such messages. For the llama backend they fold into the first system
        message, in order; other backends get the history untouched."""
        a = self.a
        old = a.FOLD_SYSTEM
        msgs = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "u1"},
                {"role": "system", "content": "[Memory] m"}, {"role": "assistant", "content": "a1"},
                {"role": "system", "content": "   "}, {"role": "system", "content": "[Now] d"},
                {"role": "user", "content": "u2"}]
        try:
            a.FOLD_SYSTEM = True
            w = a._wire_messages(msgs)
            self.assertEqual([m["role"] for m in w], ["system", "user", "assistant", "user"])
            self.assertEqual(w[0]["content"], "SYS\n\n[Memory] m\n\n[Now] d")
            self.assertEqual(msgs[0]["content"], "SYS")                           # the history itself is untouched
            self.assertEqual(a._wire_messages([{"role": "user", "content": "x"}]), [{"role": "user", "content": "x"}])
            a.FOLD_SYSTEM = False
            self.assertIs(a._wire_messages(msgs), msgs)
        finally:
            a.FOLD_SYSTEM = old

    def test_summarizing_split(self):
        a = self.a
        old_sum, old_max, old_keep, old_hist = a._summarize, a.CTX_MAX_MSGS, a.CTX_PRESERVE_RECENT, list(a._history)
        try:
            a._summarize = lambda msgs, prior="": f"MOCK(prior={prior or '-'},n={len(msgs)})"
            a.CTX_MAX_MSGS, a.CTX_PRESERVE_RECENT = 6, 4
            h = [{"role": "system", "content": a.SYSTEM}]
            for i in range(5):
                h += [{"role": "user", "content": f"f{i}"}, {"role": "assistant", "content": f"a{i}"}]
            a._history[:] = h
            a._trim_history()
            self.assertTrue(a._history[1]["content"].startswith(a.SUMMARY_TAG))
            self.assertEqual(a._history[2]["role"], "user")        # recent an user-Grenze
            self.assertEqual(a._history[-1]["content"], "a4")      # juengste bleibt
        finally:
            a._summarize, a.CTX_MAX_MSGS, a.CTX_PRESERVE_RECENT = old_sum, old_max, old_keep
            a._history[:] = old_hist

    def test_summarizing_folds_prior(self):
        a = self.a
        seen = {}
        old_sum, old_max, old_keep, old_hist = a._summarize, a.CTX_MAX_MSGS, a.CTX_PRESERVE_RECENT, list(a._history)
        try:
            def fake_sum(msgs, prior=""):
                seen["prior"] = prior
                return "NEU"
            a._summarize = fake_sum
            a.CTX_MAX_MSGS, a.CTX_PRESERVE_RECENT = 4, 2
            h = [{"role": "system", "content": a.SYSTEM},
                 {"role": "system", "content": a.SUMMARY_TAG + " ALT"}]
            for i in range(4):
                h += [{"role": "user", "content": f"f{i}"}, {"role": "assistant", "content": f"a{i}"}]
            a._history[:] = h
            a._trim_history()
            self.assertEqual(seen.get("prior"), "ALT")             # alte Zusammenfassung eingefaltet
        finally:
            a._summarize, a.CTX_MAX_MSGS, a.CTX_PRESERVE_RECENT = old_sum, old_max, old_keep
            a._history[:] = old_hist

    # --- Retry / leere-Tools-Fix im Request-Body ----------------------------
    def test_or_chat_omits_empty_tools(self):
        a = self.a
        captured = {}

        class FakeResp:
            def read(self):
                return json.dumps({"choices": [{"message": {"role": "assistant", "content": "ok"}}],
                                   "usage": {}}).encode()

        def fake_urlopen(req, timeout=0):
            captured["body"] = json.loads(req.data.decode())
            return FakeResp()

        orig = a.urllib.request.urlopen
        try:
            a.urllib.request.urlopen = fake_urlopen
            a.or_chat([{"role": "user", "content": "hi"}], [])       # leere Tools
            self.assertNotIn("tools", captured["body"])
            self.assertNotIn("tool_choice", captured["body"])
            a.or_chat([{"role": "user", "content": "hi"}],
                      [{"type": "function", "function": {"name": "x", "parameters": {}}}])
            self.assertIn("tools", captured["body"])
            self.assertEqual(captured["body"]["tool_choice"], "auto")
        finally:
            a.urllib.request.urlopen = orig

    # --- Key-Injection-Proxy (Keys verlassen den Host nie) -------------------
    def test_key_proxy_url_and_no_bearer(self):
        """With KEY_PROXY=1, _llm_url() points at the manager proxy path and
        or_chat sends NO Authorization bearer — otherwise the key would end up
        in the guest request again and the whole detour would be pointless."""
        a = self.a
        captured = {}

        class FakeResp:
            def read(self):
                return json.dumps({"choices": [{"message": {
                    "role": "assistant", "content": "ok"}}], "usage": {}}).encode()

        def fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            captured["headers"] = {k.lower(): v for k, v in req.header_items()}
            return FakeResp()

        orig_open, orig_base = a.urllib.request.urlopen, a._manager_base
        old = (a.OR_MODEL, a.OR_URL, a.LLM_NAME, a.LLM_KEY_SECRET,
               a.LLM_BACKEND, a.OR_KEY)
        os.environ["KEY_PROXY"] = "1"
        a.OR_KEY = "sk-super-geheim"          # darf NIE im Request auftauchen
        a.LLM_BACKEND = "openrouter"
        try:
            a.urllib.request.urlopen = fake_urlopen
            a._manager_base = lambda: "http://172.30.0.1:8700"
            self.assertEqual(
                a._llm_url(),
                "http://172.30.0.1:8700/api/llm/openrouter/chat/completions")
            a.or_chat([{"role": "user", "content": "hi"}], [])
            self.assertEqual(
                captured["url"],
                "http://172.30.0.1:8700/api/llm/openrouter/chat/completions")
            self.assertNotIn("authorization", captured["headers"])
            self.assertNotIn("sk-super-geheim", json.dumps(captured["headers"]))
            # /model-Wechsel muss im Proxy-Modus die PROXY-URL wechseln
            a._set_model("/model orcarouter:tencent/hy3")
            self.assertEqual(
                a._llm_url(),
                "http://172.30.0.1:8700/api/llm/orcarouter/chat/completions")
        finally:
            os.environ.pop("KEY_PROXY", None)
            a.urllib.request.urlopen, a._manager_base = orig_open, orig_base
            (a.OR_MODEL, a.OR_URL, a.LLM_NAME, a.LLM_KEY_SECRET,
             a.LLM_BACKEND, a.OR_KEY) = old

    def test_key_proxy_off_keeps_direct_url(self):
        """Without KEY_PROXY everything stays as before: direct backend URL."""
        a = self.a
        os.environ.pop("KEY_PROXY", None)
        self.assertEqual(a._llm_url(), a.OR_URL)


# ===========================================================================
# OFFLINE: Manager-Funktionen
# ===========================================================================
class ManagerFunctions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _load("manager_e2e", MANAGER_PATH)

    def test_resource_stats_shape(self):
        """resource_stats returns per instance size + live fields; an instance
        without a PID counts as not running (live values None)."""
        m = self.m
        old_load = m.load_instances
        m.load_instances = lambda: [{"name": "e2e-res-xyz", "vcpus": 4, "mem_mib": 2048, "config": {}}]
        try:
            r = next(x for x in m.resource_stats() if x["name"] == "e2e-res-xyz")
            self.assertEqual(r["vcpus"], 4)
            self.assertEqual(r["mem_mib"], 2048)
            self.assertFalse(r["running"])
            self.assertIsNone(r["rss_mb"])
            for k in ("cpu_pct", "upper_used_mb", "persist", "name"):
                self.assertIn(k, r)
        finally:
            m.load_instances = old_load

    def test_gateway_strips_noncharacters(self):
        """Layer-A extension (watermarks-remover): Unicode noncharacters and
        permanently reserved default-ignorable code points are removed; normal
        text and emoji are left untouched."""
        import text_unicode as tu
        for cp in (0xFDD0, 0xFFFE, 0x1FFFE, 0x2065, 0xFFF5, 0xE0000):
            out, _st = tu.clean_text("A" + chr(cp) + "B")
            self.assertEqual(out, "AB", "U+%04X not removed" % cp)
        self.assertEqual(tu.clean_text("Hallo Welt")[0], "Hallo Welt")
        self.assertEqual(tu.clean_text("x" + chr(0x2764) + chr(0xFE0F) + "y")[0],
                         "x" + chr(0x2764) + chr(0xFE0F) + "y")

    def test_plugin_hash_pinning(self):
        """Content-hash pinning: upload pins automatically; a direct file change
        -> modified=True; approve re-pins -> modified=False; delete removes the pin."""
        m = self.m
        import tempfile, os
        tmp = tempfile.mkdtemp(prefix="e2e-pin-")
        old_src, old_pins = m.PLUGINS_SRC, m.PLUGIN_PINS_FILE
        m.PLUGINS_SRC = tmp
        m.PLUGIN_PINS_FILE = os.path.join(tmp, ".pins.json")
        try:
            self.assertIsNone(m.plugin_write_py("foo", "DESC='x'\ndef run():\n    return 1\n"))
            lst = {p["name"]: p for p in m.list_plugins()}
            self.assertTrue(lst["foo"]["pinned"])
            self.assertFalse(lst["foo"]["modified"])
            with open(os.path.join(tmp, "foo", "tool.py"), "a") as fh:
                fh.write("# tampered\n")
            lst = {p["name"]: p for p in m.list_plugins()}
            self.assertTrue(lst["foo"]["modified"])          # Manipulation erkannt
            m.plugin_pin("foo")                              # Approve
            lst = {p["name"]: p for p in m.list_plugins()}
            self.assertFalse(lst["foo"]["modified"])
            m.plugin_delete("foo")
            self.assertNotIn("foo", m.load_plugin_pins())    # Pin mit weg
        finally:
            m.PLUGINS_SRC, m.PLUGIN_PINS_FILE = old_src, old_pins

    def test_plugin_files_link_into_vscode(self):
        """Approve must never be blind: each file name links to the file in the
        host's VS Code (code-server deep link: folder + payload openFile with
        the remote authority). Without CODE_URL/CODE_ROOT: no link, no crash."""
        m = self.m
        old = m._settings.CODE_URL, m._settings.CODE_ROOT
        try:
            m._settings.CODE_URL, m._settings.CODE_ROOT = "http://192.168.0.10:8443/", "/home/coder/firecracker"
            u = m.plugin_code_link("folder", "greeter", "lib/h.py")
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(u).query)
            self.assertTrue(u.startswith("http://192.168.0.10:8443/?"))
            self.assertEqual(q["folder"], ["/home/coder/firecracker"])
            self.assertEqual(json.loads(q["payload"][0]),
                             [["openFile", "vscode-remote://192.168.0.10:8443/home/coder/firecracker/plugins/greeter/lib/h.py"]])
            u1 = m.plugin_code_link("file", "dice", "dice.py")
            self.assertIn("/home/coder/firecracker/plugins/dice.py", json.loads(
                urllib.parse.parse_qs(urllib.parse.urlsplit(u1).query)["payload"][0])[0][1])
            m._settings.CODE_ROOT = ""
            self.assertEqual(m.plugin_code_link("folder", "greeter", "tool.py"), "")
            # list_plugins carries the links per file
            tmp = tempfile.mkdtemp(prefix="e2e-pluglink-")
            old_src, old_pins = m.PLUGINS_SRC, m.PLUGIN_PINS_FILE
            m.PLUGINS_SRC, m.PLUGIN_PINS_FILE = tmp, os.path.join(tmp, ".pins.json")
            try:
                m.plugin_write_py("foo", "DESC='x'\n")
                self.assertEqual(m.list_plugins()[0]["links"], {"tool.py": ""})
                m._settings.CODE_ROOT = "/home/coder/firecracker"
                self.assertIn("plugins/foo/tool.py", urllib.parse.unquote(
                    m.list_plugins()[0]["links"]["tool.py"]))
            finally:
                m.PLUGINS_SRC, m.PLUGIN_PINS_FILE = old_src, old_pins
        finally:
            m._settings.CODE_URL, m._settings.CODE_ROOT = old
        # the old in-manager viewer route is gone: guests never had it, admins use VS Code
        self.assertNotIn(("GET", "/api/plugins/"), {(meth, p) for meth, _, p, _ in m.ROUTER.inventory()})

    def test_set_instance_tools_roundtrip(self):
        """Saving policy tools: a subset persists (tools_all=False), ALL tools
        removes the field (tools_all=True), unknown names are filtered out.
        Regression for 'after saving, all tools are active again'."""
        m = self.m
        import tempfile, os
        tmp = tempfile.mkdtemp(prefix="e2e-tools-")
        with open(os.path.join(tmp, "toolinst.json"), "w") as fh:
            json.dump({"name": "toolinst", "template": "openrouter", "config": {}}, fh)
        old_dir, old_load, old_run = m._paths.INST_DIR, m.load_instances, m.is_running
        m._paths.INST_DIR = tmp
        m.load_instances = lambda: [_readj(os.path.join(tmp, "toolinst.json"))]
        m.is_running = lambda inst: False
        try:
            picks = sorted(m.AGENT_TOOL_NAMES)[:3]
            m.set_instance_tools("toolinst", picks)
            cfg = _readj(os.path.join(tmp, "toolinst.json"))["config"]
            self.assertEqual(set(cfg["AGENT_TOOLS"].split(",")), set(picks))   # Subset bleibt
            self.assertFalse(m.effective_policy(_readj(os.path.join(tmp, "toolinst.json")))["tools_all"])

            m.set_instance_tools("toolinst", list(m.AGENT_TOOL_NAMES))
            cfg2 = _readj(os.path.join(tmp, "toolinst.json"))["config"]
            self.assertNotIn("AGENT_TOOLS", cfg2)                              # alle -> Feld raus
            self.assertTrue(m.effective_policy(_readj(os.path.join(tmp, "toolinst.json")))["tools_all"])

            m.set_instance_tools("toolinst", ["kein_tool", picks[0]])
            cfg3 = _readj(os.path.join(tmp, "toolinst.json"))["config"]
            self.assertEqual(cfg3["AGENT_TOOLS"], picks[0])                    # unbekannte gefiltert
        finally:
            m._paths.INST_DIR, m.load_instances, m.is_running = old_dir, old_load, old_run

    def test_plugin_zip_and_slip_guard(self):
        """A multi-file zip lands in the tool folder; a ../ path (zip-slip) must NOT
        be extracted outside; a zip without an entry file is rejected."""
        m = self.m
        import tempfile, os, io, zipfile
        tmp = tempfile.mkdtemp(prefix="e2e-plug-")
        old = m.PLUGINS_SRC
        m.PLUGINS_SRC = tmp
        try:
            buf = io.BytesIO(); z = zipfile.ZipFile(buf, "w")
            z.writestr("tool.py", "DESC='x'\nPARAMS={}\nREQUIRED=[]\ndef run():\n    return 1\n")
            z.writestr("helper.py", "x=1\n"); z.close()
            self.assertIsNone(m.plugin_write_zip("mytool", buf.getvalue()))
            self.assertTrue(os.path.isfile(os.path.join(tmp, "mytool", "tool.py")))
            self.assertTrue(os.path.isfile(os.path.join(tmp, "mytool", "helper.py")))
            b2 = io.BytesIO(); z2 = zipfile.ZipFile(b2, "w")
            z2.writestr("tool.py", "def run():\n    return 1\n")
            z2.writestr("../evil.py", "boom\n"); z2.close()
            m.plugin_write_zip("slip", b2.getvalue())
            self.assertFalse(os.path.exists(os.path.join(tmp, "evil.py")))
            b3 = io.BytesIO(); z3 = zipfile.ZipFile(b3, "w"); z3.writestr("readme.txt", "x"); z3.close()
            self.assertIsNotNone(m.plugin_write_zip("noentry", b3.getvalue()))
        finally:
            m.PLUGINS_SRC = old

    def test_playbook_add_imports_present(self):
        """Regression: mgr/rules.pb_add uses uuid+time -> they must be imported,
        otherwise /api/playbook-add crashes and the agent sees RemoteDisconnected."""
        import mgr.rules as rules, tempfile, os
        tmp = tempfile.mkdtemp(prefix="e2e-pb-")
        old = rules.PLAYBOOKS_FILE
        rules.PLAYBOOKS_FILE = os.path.join(tmp, "pb.json")
        try:
            pid = rules.pb_add("inst", "a rule")         # NameError on missing import
            self.assertTrue(pid and pid != "exists")
            self.assertEqual(len(rules.pb_list("inst")), 1)
        finally:
            rules.PLAYBOOKS_FILE = old

    def test_merge_chats_tombstones(self):
        """A deletion propagates and does not resurrect — except on a genuine,
        NEWER edit (then the tombstone is dropped)."""
        m = self.m
        tmp = tempfile.mkdtemp(prefix="e2e-tomb-")
        oc, ot = m.CHATS_FILE, m.TOMBSTONES_FILE
        m.CHATS_FILE = os.path.join(tmp, "chats.json")
        m.TOMBSTONES_FILE = os.path.join(tmp, "tombs.json")
        try:
            NOW = 1_800_000_000_000
            has = lambda: any(c.get("id") == "x" for c in m.load_chats())
            m.merge_chats([{"id": "x", "updatedAt": NOW - 5000,
                            "messages": [{"user": True, "text": "hi"}]}])
            self.assertTrue(has())
            m.merge_chats({"chats": [], "tombstones": {"x": NOW}})       # delete
            self.assertFalse(has())
            self.assertIn("x", m.load_tombstones())
            m.merge_chats([{"id": "x", "updatedAt": NOW - 1000,          # Re-Push alt
                            "messages": [{"user": True, "text": "hi"}]}])
            self.assertFalse(has())                                      # bleibt weg
            m.merge_chats([{"id": "x", "updatedAt": NOW + 9000,          # echte Bearbeitung
                            "messages": [{"user": True, "text": "edit"}]}])
            self.assertTrue(has())                                       # aufersteht
            self.assertNotIn("x", m.load_tombstones())                   # Tombstone weg
            # A bogus far-future timestamp (leaked sync fixture, year 2286) must
            # still be deletable — it must not beat the deletion.
            m.merge_chats([{"id": "y", "updatedAt": 10000000000001,
                            "messages": [{"user": True, "text": "SYNC"}]}])
            self.assertTrue(any(c.get("id") == "y" for c in m.load_chats()))
            m.merge_chats({"chats": [], "tombstones": {"y": NOW}})       # delete now
            self.assertFalse(any(c.get("id") == "y" for c in m.load_chats()))
            m.merge_chats([{"id": "y", "updatedAt": 10000000000001,      # re-push the future chat
                            "messages": [{"user": True, "text": "SYNC"}]}])
            self.assertFalse(any(c.get("id") == "y" for c in m.load_chats()))   # stays deleted
        finally:
            m.CHATS_FILE, m.TOMBSTONES_FILE = oc, ot

    def test_provider_switch_sets_and_clears_keys(self):
        m = self.m
        tmp = tempfile.mkdtemp(prefix="e2e-inst-")
        inst = {"name": "e2e-switch", "config": {"OPENROUTER_MODEL": "google/gemini-2.5-flash"}}
        with open(os.path.join(tmp, "e2e-switch.json"), "w") as fh:
            json.dump(inst, fh)
        old_dir, old_load = m._paths.INST_DIR, m.load_instances
        try:
            m._paths.INST_DIR = tmp
            m.load_instances = lambda: [_readj(os.path.join(tmp, "e2e-switch.json"))]
            msg = m.set_model("e2e-switch", "orcarouter:tencent/hy3")
            cfg = _readj(os.path.join(tmp, "e2e-switch.json"))["config"]
            self.assertEqual(cfg.get("ORCAROUTER_MODEL"), "tencent/hy3")
            self.assertNotIn("OPENROUTER_MODEL", cfg)               # anderer Provider entfernt
        finally:
            m._paths.INST_DIR, m.load_instances = old_dir, old_load

    def test_provider_switch_ignores_free_suffix(self):
        """':free' model variants must NOT be read as a provider."""
        m = self.m
        tmp = tempfile.mkdtemp(prefix="e2e-inst2-")
        with open(os.path.join(tmp, "e2e-free.json"), "w") as fh:
            json.dump({"name": "e2e-free", "config": {"OPENROUTER_MODEL": "x"}}, fh)
        old_dir, old_load = m._paths.INST_DIR, m.load_instances
        try:
            m._paths.INST_DIR = tmp
            m.load_instances = lambda: [_readj(os.path.join(tmp, "e2e-free.json"))]
            m.set_model("e2e-free", "mistralai/mistral-7b-instruct:free")
            cfg = _readj(os.path.join(tmp, "e2e-free.json"))["config"]
            self.assertEqual(cfg.get("OPENROUTER_MODEL"), "mistralai/mistral-7b-instruct:free")
            self.assertNotIn("ORCAROUTER_MODEL", cfg)
        finally:
            m._paths.INST_DIR, m.load_instances = old_dir, old_load

    def test_hitl_lifecycle(self):
        m = self.m
        import mgr.signal as sigmod
        old_send = sigmod.signal_send
        try:
            sigmod.signal_send = lambda text, to=None: (True, "sent")
            hid = m.hitl_create("orchestrator", "bash", "rm foo")
            self.assertIsNotNone(hid)
            self.assertEqual(m.hitl_status(hid), "pending")
            self.assertTrue(m.hitl_resolve(hid, True))
            self.assertEqual(m.hitl_status(hid), "approved")
            self.assertFalse(m.hitl_resolve(hid, True))     # not resolvable twice
            self.assertEqual(m.hitl_status("nonexistent"), "unknown")
        finally:
            sigmod.signal_send = old_send

    def test_hitl_no_signal_no_block(self):
        """If the Signal send fails (no recipient), hitl_create returns None
        -> the agent then does not block."""
        m = self.m
        import mgr.signal as sigmod
        old_send = sigmod.signal_send
        try:
            sigmod.signal_send = lambda text, to=None: (False, "no recipient")
            self.assertIsNone(m.hitl_create("x", "bash", "y"))
        finally:
            sigmod.signal_send = old_send

    def test_katfs_zip_recursive(self):
        m = self.m
        tree = {".": [{"name": "a.txt", "dir": False}, {"name": "sub", "dir": True}],
                "sub": [{"name": "b.txt", "dir": False}]}
        files = {"a.txt": b"AAA", "sub/b.txt": b"BBB"}

        def fake_proxy(op, share, path, recursive=False, body=None):
            if op == "ls":
                return 200, "application/json", json.dumps({"entries": tree.get(path or ".", [])}).encode()
            if op == "read":
                return 200, "application/octet-stream", files[path]
            raise AssertionError(op)

        import mgr.katfs as kmod
        old = kmod.katfs_proxy_fs
        try:
            kmod.katfs_proxy_fs = fake_proxy
            data, stats = m.katfs_zip("share1", ".")
            zf = zipfile.ZipFile(io.BytesIO(data))
            names = sorted(zf.namelist())
            self.assertEqual(names, ["a.txt", "sub/b.txt"])
            self.assertEqual(zf.read("sub/b.txt"), b"BBB")
            self.assertEqual(stats["files"], 2)
        finally:
            kmod.katfs_proxy_fs = old

    def test_audit_records_error_text_and_result(self):
        """ok alone cannot distinguish a healthy call from one that failed
        politely — the trail now carries the error text, a result excerpt and
        the turn id (that is what makes a saddler review possible at all)."""
        m = self.m
        tmp = tempfile.mkdtemp(prefix="e2e-audit-")
        old = m.AUDIT_DIR
        try:
            m.AUDIT_DIR = tmp
            m.audit_append("inst-a", "http_fetch", "https://x.example", False,
                           err="⚠️ HTTP 403: blocked", turn="t1234")
            m.audit_append("inst-a", "web_search", "cronn", True,
                           result="1. cronn GmbH …", turn="t1234")
            recs = m.audit_read("inst-a")     # newest first
            self.assertEqual(len(recs), 2)
            self.assertEqual(recs[1]["err"], "⚠️ HTTP 403: blocked")
            self.assertNotIn("result", recs[1])
            self.assertEqual(recs[0]["result"], "1. cronn GmbH …")
            self.assertEqual({r["turn"] for r in recs}, {"t1234"})
        finally:
            m.AUDIT_DIR = old

    def test_ha_alias_learns_via_fake_websocket(self):
        """learn_alias speaks the HA WebSocket protocol: handshake -> auth ->
        read current aliases -> append -> update. Verified against a fake HA
        server on a real loopback socket (frames masked from us, unmasked from
        it), so the framing itself is exercised, not mocked away."""
        import threading, struct as _st, socket as _sock, mgr.haalias as ha

        def ws_recv(conn, buf):
            def need(n):
                while len(buf) < n:
                    buf.extend(conn.recv(4096))
            need(2)
            masked = buf[1] & 0x80
            ln = buf[1] & 0x7f
            i = 2
            if ln == 126:
                need(4); ln = _st.unpack(">H", bytes(buf[2:4]))[0]; i = 4
            need(i + (4 if masked else 0) + ln)
            if masked:
                m = bytes(buf[i:i + 4]); i += 4
                data = bytes(buf[i + k] ^ m[k % 4] for k in range(ln))
            else:
                data = bytes(buf[i:i + ln])
            del buf[:i + ln]
            return json.loads(data)

        def ws_send(conn, obj):
            d = json.dumps(obj).encode(); ln = len(d)
            hdr = bytearray([0x81])
            if ln < 126:
                hdr.append(ln)
            else:
                hdr += bytes([126]) + _st.pack(">H", ln)
            conn.sendall(bytes(hdr) + d)   # server frames unmasked

        srv = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
        srv.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        captured = {}

        def fake_ha():
            conn, _ = srv.accept()
            buf = bytearray()
            hs = b""
            while b"\r\n\r\n" not in hs:
                hs += conn.recv(4096)
            import hashlib as _h, base64 as _b
            key = [l.split(": ", 1)[1] for l in hs.decode().split("\r\n")
                   if l.lower().startswith("sec-websocket-key")][0]
            acc = _b.b64encode(_h.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")
                                       .encode()).digest()).decode()
            conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                          f"Connection: Upgrade\r\nSec-WebSocket-Accept: {acc}\r\n\r\n").encode())
            ws_send(conn, {"type": "auth_required"})
            auth = ws_recv(conn, buf); captured["token"] = auth.get("access_token")
            ws_send(conn, {"type": "auth_ok"})
            get = ws_recv(conn, buf); captured["get"] = get
            ws_send(conn, {"id": get["id"], "success": True,
                           "result": {"aliases": ["schon da"]}})
            upd = ws_recv(conn, buf); captured["upd"] = upd
            ws_send(conn, {"id": upd["id"], "success": True,
                           "result": {"entity_entry": {"aliases": upd["aliases"]}}})
            conn.close()

        t = threading.Thread(target=fake_ha, daemon=True); t.start()
        old_tgt, old_tok = ha.ha_ws_target, ha.ha_token
        try:
            ha.configure(lambda: ("127.0.0.1", port), lambda: "tok-123")
            msg = ha.learn_alias("Gartenhaus denke rechts", "light.gartenhaus_decke_rechts")
        finally:
            ha.configure(old_tgt, old_tok)
            srv.close()
        t.join(timeout=5)
        self.assertEqual(captured["token"], "tok-123")
        self.assertEqual(captured["get"]["entity_id"], "light.gartenhaus_decke_rechts")
        self.assertEqual(captured["upd"]["aliases"], ["schon da", "Gartenhaus denke rechts"])
        self.assertIn("learned", msg)

    def test_ha_alias_rejects_bad_input(self):
        import mgr.haalias as ha
        self.assertIn("error", ha.learn_alias("", "light.x"))
        self.assertIn("error", ha.learn_alias("Wort", "noentityid"))

    def test_ha_control_matches_exact_area_and_fuzzy(self):
        """control() picks the right target server-side: a strong single match
        beats the area (even when it contains the area word), an area name plus
        a group cue switches the whole room, and a fuzzy single hit learns the
        alias. HA REST/WS are stubbed — the MATCHING logic is what's tested."""
        import mgr.haalias as ha
        idx = {"areas": {"gh": "Gartenhaus"}, "entities": [
            {"entity_id": "light.gartenhaus_decke_rechts",
             "names": ["Gartenhaus Decke rechts"], "area_id": "gh"},
            {"entity_id": "light.gartenhaus_decke_links",
             "names": ["Gartenhaus Decke links"], "area_id": "gh"},
            {"entity_id": "light.kitchen", "names": ["Küche"], "area_id": "k"}]}
        calls, learned = [], []
        old = (ha._entity_index, ha._rest, ha.learn_alias)
        try:
            ha._entity_index = lambda: idx
            ha._rest = lambda path, payload=None: calls.append((path, payload))
            ha.learn_alias = lambda spoken, eid: learned.append((spoken, eid)) or "learned"

            # exact -> single lamp, no learning
            calls.clear(); learned.clear()
            r = ha.control("Gartenhaus Decke rechts", "on")
            self.assertIn("turn_on", calls[0][0])
            self.assertEqual(calls[0][1]["entity_id"], "light.gartenhaus_decke_rechts")
            self.assertEqual(learned, [])

            # mishearing -> strong fuzzy single, learns alias (NOT the area)
            calls.clear(); learned.clear()
            r = ha.control("Gartenhaus Tecke rechts", "off")
            self.assertEqual(calls[0][1]["entity_id"], "light.gartenhaus_decke_rechts")
            self.assertEqual(learned, [("Gartenhaus Tecke rechts", "light.gartenhaus_decke_rechts")])

            # area + cue -> whole room (list of both lights)
            calls.clear(); learned.clear()
            r = ha.control("Licht im Gartenhaus", "off")
            self.assertEqual(sorted(calls[0][1]["entity_id"]),
                             ["light.gartenhaus_decke_links", "light.gartenhaus_decke_rechts"])

            # 2026-09-08: "Gartenhauslicht an" scored 0.76 against the relay
            # switch.gartenhaus_switch_l1 and switched THAT. Area + cue must
            # beat a merely good fuzzy hit; a near-exact one still wins.
            idx["entities"].append({"entity_id": "switch.gartenhaus_switch_l1",
                                    "names": ["gartenhaus_switch L1"], "area_id": "gh"})
            calls.clear(); learned.clear()
            r = ha.control("Gartenhauslicht", "on")
            self.assertEqual(sorted(calls[0][1]["entity_id"]),
                             ["light.gartenhaus_decke_links", "light.gartenhaus_decke_rechts"], r)
            self.assertEqual(learned, [])
            calls.clear(); learned.clear()
            r = ha.control("Gartenhaus Tecke rechts", "on")      # near-exact still single
            self.assertEqual(calls[0][1]["entity_id"], "light.gartenhaus_decke_rechts")

            # nonsense -> no switch
            calls.clear()
            r = ha.control("völliger Unsinn xyz", "on")
            self.assertIn("error", r)
            self.assertEqual(calls, [])
        finally:
            ha._entity_index, ha._rest, ha.learn_alias = old

    def test_ha_control_validates_action(self):
        import mgr.haalias as ha
        self.assertIn("error", ha.control("Licht", "blink"))
        self.assertIn("error", ha.control("", "on"))

    def test_saddler_digest_groups_and_reflects(self):
        """Failures grouped by error SHAPE (digits/urls normalised), current
        week next to the previous one — the drop after a patch is the
        reflection step."""
        import mgr.saddler as sad
        tmp = tempfile.mkdtemp(prefix="e2e-sad-")
        old = sad.AUDIT_DIR
        now = int(time.time())
        try:
            sad.AUDIT_DIR = tmp
            with open(os.path.join(tmp, "aiagent.jsonl"), "w") as fh:
                for ts, ok, err in (
                        (now - 3600, False, "⚠️ HTTP 403: https://a.example/x blocked"),
                        (now - 7200, False, "⚠️ HTTP 403: https://b.example/y blocked"),
                        (now - 8 * 86400, False, "⚠️ HTTP 403: https://c.example/z blocked"),
                        (now - 3600, True, "")):
                    fh.write(json.dumps({"ts": ts, "tool": "http_fetch",
                                         "target": "t", "ok": ok, "err": err}) + "\n")
            d = sad.digest(days=7)
            self.assertEqual(d["totals"]["cur_failed"], 2)
            self.assertEqual(d["totals"]["prev_failed"], 1)
            self.assertEqual(len(d["groups"]), 1, "same error shape must be ONE group")
            g = d["groups"][0]
            self.assertEqual((g["cur"], g["prev"]), (2, 1))
            self.assertIn("<url>", g["error"])
            txt = sad.render(d)
            self.assertIn("2x (1x)", txt)
            self.assertIn("http_fetch", txt)
        finally:
            sad.AUDIT_DIR = old

    def test_websearch_backend_order_and_key_stays_home(self):
        """Brave first WHEN the key is set; without it the reason is named.
        The key itself never leaves the manager — the agents only see results."""
        from mgr import websearch as ws
        old_get, old_ddg, old_bing = ws.get_setting, ws._ddg, ws._bing
        try:
            ws._ddg = lambda q, c: None                       # challenge
            ws._bing = lambda q, c: [("BingTitel", "https://b.example", "s")]
            # Without a key: brave is skipped WITH the reason, bing delivers.
            ws.get_setting = lambda k: ""
            out = ws.web_search("x", 3)
            self.assertIn("BingTitel", out)
            # All dead -> the error names every backend and the why.
            ws._bing = lambda q, c: (_ for _ in ()).throw(OSError("down"))
            out = ws.web_search("x", 3)
            self.assertIn("brave: no API key", out)
            self.assertIn("duckduckgo: blocked", out)
            self.assertIn("NOT an empty result", out)
            # With a key, brave's results win outright.
            ws.get_setting = lambda k: "fake-key" if k == "BRAVE_API_KEY" else ""
            old_brave = ws._brave
            ws._brave = lambda q, c: [("BraveTitel", "https://brave.example", "sn")]
            try:
                out = ws.web_search("x", 3)
            finally:
                ws._brave = old_brave
            self.assertIn("BraveTitel", out)
        finally:
            ws.get_setting, ws._ddg, ws._bing = old_get, old_ddg, old_bing

    def test_websearch_bing_redirects_decoded(self):
        import base64
        from mgr.websearch import bing_real_url
        target = "https://de.wikipedia.org/wiki/Unternehmen"
        b64 = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
        self.assertEqual(bing_real_url(
            f"https://www.bing.com/ck/a?!&amp;&amp;p=x&amp;u=a1{b64}&amp;ntb=1"), target)
        self.assertEqual(bing_real_url("https://example.org/y"), "https://example.org/y")

    def test_extract_docx(self):
        """DOCX is a ZIP of XML — built in the test, no fixtures on disk."""
        import io, zipfile
        from mgr.extract import extract_document
        W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        doc = (f'<?xml version="1.0"?><w:document xmlns:w="{W}"><w:body>'
               f'<w:p><w:r><w:t>Erste Zeile mit Umlauten: äöü.</w:t></w:r></w:p>'
               f'<w:p><w:r><w:t>Zweiter</w:t></w:r><w:r><w:t xml:space="preserve"> Absatz.</w:t></w:r></w:p>'
               f'</w:body></w:document>')
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("word/document.xml", doc)
        text, note = extract_document("brief.docx", buf.getvalue())
        self.assertIn("Erste Zeile mit Umlauten: äöü.", text)
        self.assertIn("Zweiter Absatz.", text)          # runs joined, paragraphs split
        self.assertEqual(text.splitlines()[0], "Erste Zeile mit Umlauten: äöü.")
        self.assertEqual(note, "")

    def test_extract_pdf_builtin(self):
        """A minimal Flate-compressed PDF, built in the test. Covers the
        fallback path used when the host has no pdftotext."""
        import zlib
        from mgr import extract as ex
        content = zlib.compress(
            b"BT /F1 12 Tf (Hello from a ) Tj (tiny PDF.) Tj T* "
            b"[(Second) ( line) (.)] TJ ET")
        pdf = (b"%PDF-1.4\n1 0 obj\n<< /Length " + str(len(content)).encode()
               + b" /Filter /FlateDecode >>\nstream\n" + content
               + b"\nendstream\nendobj\ntrailer\n<<>>\n%%EOF")
        old_which = ex.shutil.which
        try:
            ex.shutil.which = lambda _n: None          # force the builtin path
            text, _ = ex.extract_document("doc.pdf", pdf)
        finally:
            ex.shutil.which = old_which
        self.assertIn("Hello from a tiny PDF.", text)
        self.assertIn("Second line.", text)

    @unittest.skipUnless(
        __import__("shutil").which("docker") and __import__("subprocess").run(
            ["docker", "image", "inspect", "kaim56-pdftotext"],
            capture_output=True).returncode == 0,
        "kaim56-pdftotext image not available")
    def test_extract_pdf_via_docker_fallback(self):
        """The host has no pdftotext; the poppler container must cover what the
        built-in extractor cannot (CID/subset fonts — the case a user hit)."""
        import zlib
        from mgr import extract as ex
        content = zlib.compress(b"BT /F1 12 Tf (Container weg funktioniert.) Tj ET")
        pdf = (b"%PDF-1.4\n"
               b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
               b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
               b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
               b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n"
               b"4 0 obj\n<< /Length " + str(len(content)).encode()
               + b" /Filter /FlateDecode >>\nstream\n" + content + b"\nendstream\nendobj\n"
               b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n"
               b"trailer\n<< /Root 1 0 R >>\n%%EOF")
        old_which, old_builtin = ex.shutil.which, ex._pdf_text_builtin
        old_probe = ex._docker_image_ok
        def no_pdftotext(name):
            return None if name == "pdftotext" else old_which(name)
        try:
            ex.shutil.which = no_pdftotext
            ex._pdf_text_builtin = lambda d: ""       # builtin "cannot" -> docker must
            ex._docker_image_ok = None                # re-probe with real which()
            ex.shutil.which = old_which               # probe needs real docker path
            ex.shutil.which = no_pdftotext
            text, _ = ex.extract_document("cid.pdf", pdf)
        finally:
            ex.shutil.which, ex._pdf_text_builtin = old_which, old_builtin
            ex._docker_image_ok = old_probe
        self.assertIn("Container weg funktioniert.", text)

    def test_extract_refuses_garbage_instead_of_feeding_it(self):
        """CID-font PDFs decode to noise — the caller must get an error, not
        gibberish that quietly poisons the model's context."""
        import zlib
        from mgr import extract as ex
        noise = bytes(range(1, 32)) * 40               # unprintable soup
        content = zlib.compress(b"BT (" + noise.replace(b"(", b"").replace(b")", b"")
                                .replace(b"\\", b"") + b") Tj ET")
        pdf = (b"%PDF-1.4\nstream\n" + content + b"\nendstream\n%%EOF")
        old_which = ex.shutil.which
        try:
            ex.shutil.which = lambda _n: None
            with self.assertRaises(ValueError):
                ex.extract_document("scan.pdf", pdf)
        finally:
            ex.shutil.which = old_which

    def test_extract_plain_and_unsupported(self):
        from mgr.extract import extract_document
        text, _ = extract_document("notes.md", "# Titel\nInhalt äöü".encode())
        self.assertIn("Inhalt äöü", text)
        with self.assertRaises(ValueError):
            extract_document("video.mp4", b"\x00\x01\x02")
        with self.assertRaises(ValueError):
            extract_document("empty.txt", b"   ")

    def test_extract_caps_huge_documents(self):
        from mgr import extract as ex
        text, note = ex.extract_document("big.txt", (b"x" * (ex.MAX_CHARS + 500)))
        self.assertEqual(len(text), ex.MAX_CHARS)
        self.assertIn("truncated", note)

    def test_no_route_is_shadowed_by_an_earlier_prefix(self):
        """Guard for the if-chain: a prefix branch standing BEFORE an exact
        branch swallows it, and the exact branch becomes dead code. That has
        bitten twice (/api/skills/ vs /api/skills). Anything migrated into the
        router is immune by construction; this covers what is still a chain."""
        from mgr.routes import source_routes, shadowed
        src = open(MANAGER_PATH).read()
        get_src = src[src.index("    def _do_GET(self):"):src.index("    def _do_POST(self):")]
        post_src = src[src.index("    def _do_POST(self):"):]
        for name, part in (("GET", get_src), ("POST", post_src)):
            bad = shadowed(source_routes(part))
            self.assertEqual(bad, [], f"{name}: dead branches behind a prefix: {bad}")

    def test_router_prefers_exact_over_prefix(self):
        """The property the chain could not guarantee: registration order does
        not decide who answers."""
        from mgr.routes import Router
        r = Router()
        r.add("GET", "/api/skills/", lambda h: "prefix", prefix=True)   # first!
        r.add("GET", "/api/skills", lambda h: "exact")
        r.add("GET", "/api/skills/deep/", lambda h: "deeper", prefix=True)
        self.assertEqual(r.resolve("GET", "/api/skills")[0](None), "exact")
        self.assertEqual(r.resolve("GET", "/api/skills?x=1")[0](None), "exact")
        self.assertEqual(r.resolve("GET", "/api/skills/docker")[0](None), "prefix")
        # Longest prefix wins, whatever the order of registration.
        self.assertEqual(r.resolve("GET", "/api/skills/deep/x")[0](None), "deeper")
        self.assertIsNone(r.resolve("GET", "/nope"))
        self.assertIsNone(r.resolve("POST", "/api/skills"))     # method matters

    def test_router_refuses_duplicate_routes(self):
        from mgr.routes import Router
        r = Router()
        r.add("GET", "/x", lambda h: 1)
        with self.assertRaises(ValueError):
            r.add("GET", "/x", lambda h: 2)
        r.add("GET", "/y/", lambda h: 1, prefix=True)
        with self.assertRaises(ValueError):
            r.add("GET", "/y/", lambda h: 2, prefix=True)

    def test_router_inventory_records_who_may_call(self):
        """The inventory is what makes an access audit a loop instead of a
        reading exercise: every route says whether guests may call it."""
        m = self.m
        inv = m.ROUTER.inventory()
        self.assertTrue(inv, "the router should carry routes")
        for method, kind, path, admin in inv:
            self.assertIn(method, ("GET", "POST"))
            self.assertIn(kind, ("exact", "prefix"))
            self.assertTrue(path.startswith("/"))
            self.assertIsInstance(admin, bool)
        by_route = {(meth, p): admin for meth, _, p, admin in inv}
        # Settings once served the API keys in plain text — guests must not see it.
        self.assertTrue(by_route[("GET", "/api/settings")], "/api/settings must stay admin-only")
        self.assertTrue(by_route[("POST", "/api/settings")])
        self.assertTrue(by_route[("GET", "/api/instances")])
        # The agents need these reads, so they are deliberately open to guests —
        # the writes on the same paths are admin-only.
        self.assertFalse(by_route[("GET", "/api/skills")])
        self.assertFalse(by_route[("GET", "/api/personas")])
        self.assertTrue(by_route[("POST", "/api/skills")])
        self.assertTrue(by_route[("POST", "/api/personas")])
        # The whole HTTP surface lives in the table now: no if-chain left.
        from mgr.routes import source_routes
        src = open(MANAGER_PATH).read()
        chain = src[src.index("    def _do_GET(self):"):src.index("    def _dispatch(self, method):")]
        self.assertEqual([p for _, p, _ in source_routes(chain)], [],
                         "a path literal crept back into the handler chain")
        self.assertGreater(len(inv), 90)

    def test_skills_page_carries_no_contents(self):
        """Regression: the page inlined the COMPLETE skills.json. With the
        imported catalog (~870 KB) that would ship on every page load — only
        name + description belong in the page, the body comes from
        GET /api/skills/<name> when editing."""
        m = self.m
        marker = "SKILL-BODY-MARKER-DO-NOT-INLINE"
        old = m.load_skills
        try:
            m.load_skills = lambda: [{"name": "e2e-skill", "description": "kurz",
                                      "content": marker + " x" * 5000}]
            page = m.render()
            self.assertIn("e2e-skill", page)          # name/description are in
            self.assertIn("kurz", page)
            self.assertNotIn(marker, page)            # the body is NOT
        finally:
            m.load_skills = old

    def test_footer_code_link_optional(self):
        """The editor link is host-specific (site.json CODE_URL): set = link in
        the footer, unset = no placeholder left over in the page."""
        m = self.m
        old = m._settings.CODE_URL
        try:
            m._settings.CODE_URL = "http://example.invalid:8443/"
            page = m.render()
            self.assertIn('href="http://example.invalid:8443/"', page)
            self.assertIn('rel="noopener noreferrer"', page)
            m._settings.CODE_URL = ""
            page = m.render()
            self.assertNotIn("__CODE_LINK__", page)
        finally:
            m._settings.CODE_URL = old

    def test_tool_catalog_matches_agent(self):
        """Drift guard: every tool in the agent (BUILTIN) must be in the manager
        catalog (AGENT_TOOLS_CATALOG) — otherwise it is missing from the create
        form and a tool allowlist blocks it silently (happened with mission_start
        and offload_read). And vice versa: no catalog entry without a real tool."""
        m = self.m
        a = _load("agent_cat_e2e", AGENT_PATH,
                  {"CLAUDE_WORKDIR": tempfile.mkdtemp(prefix="e2e-cat-"),
                   "OPENROUTER_API_KEY": "dummy"})
        agent_tools = set(a.BUILTIN.keys())
        catalog = set(m.AGENT_TOOL_NAMES)
        missing_in_catalog = agent_tools - catalog
        self.assertFalse(missing_in_catalog,
                         f"tools in the agent but not in the manager catalog: {sorted(missing_in_catalog)}")
        ghost_in_catalog = catalog - agent_tools
        self.assertFalse(ghost_in_catalog,
                         f"catalog entries without a real agent tool: {sorted(ghost_in_catalog)}")

    def test_provider_model_key_covers_all(self):
        m = self.m
        for k in m.PROVIDER_MODEL_KEY.values():
            self.assertIn(k, m.MODEL_KEYS)

    def test_llm_proxy_route_registered(self):
        """Injection gateway: the path must be in the guest allowlist (otherwise
        403 for the VM), the upstreams must match the secret names, and the
        settings toggle must appear in the schema."""
        m = self.m
        self.assertIn("/api/llm/", m.GUEST_POST_PREFIXES)
        self.assertEqual(set(m.LLM_PROXY_UPSTREAMS), {"openrouter", "orcarouter"})
        for url, keyname in m.LLM_PROXY_UPSTREAMS.values():
            self.assertTrue(url.endswith("/chat/completions"), url)
            self.assertIn(keyname, m._settings.SECRET_PARAMS)
        self.assertIn("LLM_KEY_PROXY", [s["key"] for s in m._settings.SETTINGS_SCHEMA])

    def test_overlay_upper_lifecycle(self):
        m = self.m
        tmp = tempfile.mkdtemp(prefix="e2e-ov-")
        old_run, old_inst = m._paths.RUN_DIR, m._paths.INST_DIR
        try:
            m._paths.RUN_DIR = tmp; m._paths.INST_DIR = tmp
            inst = {"name": "e2e-ov", "rootfs": "instances/openrouter-rootfs.ext4"}
            # Wegwerf-Upper landet in RUN_DIR
            self.assertTrue(m.upper_path(inst).startswith(tmp))
            self.assertIn(".upper.ext4", m.upper_path(inst))
            # Persistenter Upper in INST_DIR mit anderem Namen
            inst["persist_disk"] = True
            self.assertIn("-upper.ext4", m.upper_path(inst))
            p = m.make_upper(inst)
            self.assertTrue(os.path.exists(p))
            size1 = os.path.getsize(p)
            self.assertEqual(size1, m.UPPER_PERSIST_SIZE_MB * 1024 * 1024)
            # persist: second call uses the existing file (no reset)
            with open(p, "r+b") as fh:
                fh.seek(0); marker = fh.read(4)
            self.assertEqual(m.make_upper(inst), p)
        finally:
            m._paths.RUN_DIR, m._paths.INST_DIR = old_run, old_inst

    def test_overlay_bootarg_in_config(self):
        m = self.m
        inst = next((i for i in m.load_instances()
                     if i.get("rootfs") in m.OVERLAY_ROOTFS), None)
        if not inst:
            self.skipTest("no overlay instance available")
        old_mk = m.make_upper
        try:
            m.make_upper = lambda i: "/tmp/fake-upper.ext4"   # no real mkfs in the test
            cfg = m.gen_config(inst)
        finally:
            m.make_upper = old_mk
        self.assertIn("fc_upper=/dev/vd", cfg["boot-source"]["boot_args"])
        root = next(d for d in cfg["drives"] if d["drive_id"] == "rootfs")
        self.assertTrue(root["is_read_only"], "Basis muss read-only sein")
        self.assertEqual(cfg["drives"][-1]["drive_id"], "upper")

    def test_mission_lifecycle(self):
        m = self.m
        tmp = tempfile.mkdtemp(prefix="e2e-mi-")
        import mgr.missions as mmod
        old_file = mmod.MISSIONS_FILE
        old_notify = mmod.notify_add
        try:
            mmod.MISSIONS_FILE = os.path.join(tmp, "missions.json")
            mmod.notify_add = lambda *a, **k: ("x", "ok")   # no real push in the test
            mid, note = m.mission_start("orchestrator", "Testziel", ["s1", "s2"])
            self.assertTrue(mid)
            self.assertEqual(m.mission_start("orchestrator", "", [])[0], None)
            self.assertEqual(m.mission_update("orchestrator", mid, step=1,
                                              status="doing", task_id="t-1"), "ok")
            inst, mi, st = m.mission_for_task("t-1")
            self.assertEqual((inst, mi["id"], st["n"]), ("orchestrator", mid, 1))
            self.assertEqual(m.mission_update("orchestrator", mid, step=1,
                                              status="done", result="ok"), "ok")
            self.assertIsNone(m.mission_for_task("t-1")[1])   # done -> no more trigger
            self.assertEqual(m.mission_admin("orchestrator", mid, "pause"), "ok")
            self.assertEqual(m.mission_admin("orchestrator", mid, "resume"), "ok")
            self.assertEqual(m.mission_finish("orchestrator", mid, "fertig"), "ok")
            done = m.mission_list("orchestrator")[0]
            self.assertEqual(done["status"], "done")
            self.assertIn("cannot", m.mission_admin("orchestrator", mid, "abort"))
        finally:
            mmod.MISSIONS_FILE = old_file
            mmod.notify_add = old_notify

    def test_voice_turns_show_up_in_the_shared_chat(self):
        """A voice client's turns land in the shared store as a 'Voice · inst'
        conversation (live on the web); /reset archives it and the next turn
        opens a new one. Web-chat ids are left alone."""
        m = self.m
        tmp = tempfile.mkdtemp(prefix="e2e-voice-")
        oc, ot = m.CHATS_FILE, m.TOMBSTONES_FILE
        m.CHATS_FILE, m.TOMBSTONES_FILE = os.path.join(tmp, "chats.json"), os.path.join(tmp, "tombs.json")
        m._voice_sessions.clear()
        try:
            self.assertEqual(m.voice_session("vc", "10.0.0.9", "1757000000000"), "")   # web chat
            self.assertEqual(m.voice_session("vc", "10.0.0.9", "voice-1757"), "voice-vc-1757")
            s1 = m.voice_session("vc", "10.0.0.9")            # ESP: manager-kept
            self.assertTrue(s1.startswith("voice-vc-"))
            self.assertEqual(m.voice_session("vc", "10.0.0.9"), s1)   # stable
            m.chat_log_append("vc", s1, "Radio aus", "Radio ist aus.", kind="voice")
            m.chat_log_append("vc", s1, "Wie spät?", "16:36 Uhr.", kind="voice")
            m.voice_session("vc", "10.0.0.9", reset=True)
            time.sleep(1.1)                                     # session ids carry seconds
            s2 = m.voice_session("vc", "10.0.0.9")
            self.assertNotEqual(s1, s2)
            m.chat_log_append("vc", s2, "Hallo", "Hallo!", kind="voice")
            chats = {c["id"]: c for c in m.load_chats()}
            self.assertEqual(len(chats[s1]["messages"]), 4)     # archived, intact
            self.assertEqual(len(chats[s2]["messages"]), 2)
            self.assertEqual(chats[s1]["instance"], "vc")
            self.assertTrue(chats[s1]["title"].startswith("Voice · vc · "))
        finally:
            m.CHATS_FILE, m.TOMBSTONES_FILE = oc, ot
            m._voice_sessions.clear()

    def test_tts_reads_no_tool_lines(self):
        """Read aloud must skip tool status, think blocks, fences and decor —
        filtered in the manager so web, app and ESP get it for free."""
        m = self.m
        self.assertEqual(m.speakable_text("🔧 ha_control …\nEs gab ein Problem."), "Es gab ein Problem.")
        self.assertEqual(m.speakable_text("  🔧 caldav__list-events …\n\nAm Freitag: **Frühstück**."),
                         "Am Freitag: Frühstück.")
        self.assertEqual(m.speakable_text("⟦think⟧ plan ⟦/think⟧Es ist 10 Uhr."), "Es ist 10 Uhr.")
        self.assertEqual(m.speakable_text("Siehe [Doku](https://x.y/z) und https://a.b/c."), "Siehe Doku und")
        self.assertIn("Codeblock übersprungen", m.speakable_text("Hier:\n```py\nprint(1)\n```\nfertig"))
        self.assertEqual(m.speakable_text(""), "")

    def test_stt_recent_ring(self):
        """What did STT hear? Newest first, bounded, in memory only."""
        m = self.m
        m._stt_recent.clear()
        m.stt_remember("Radio aus", 1.2, "192.168.1.5")
        m.stt_remember("Wie spät ist es?", 1.8, "192.168.1.5")
        r = m.stt_recent()
        self.assertEqual([x["text"] for x in r], ["Wie spät ist es?", "Radio aus"])
        for i in range(m.STT_RECENT_MAX + 5):
            m.stt_remember(f"t{i}", 1, "x")
        self.assertEqual(len(m.stt_recent()), m.STT_RECENT_MAX)
        by_path = {p: admin for _, _, p, admin in m.ROUTER.inventory()}
        self.assertTrue(by_path["/api/stt-recent"])
        self.assertTrue(by_path["/api/stt-recent/audio"])
        m._stt_audio.clear()
        m.stt_remember("a", 1, "x", audio=b"RIFFaaa", ctype="audio/wav")
        m.stt_remember("b", 1, "x", audio=b"RIFFbbb", ctype="audio/wav")
        self.assertEqual(m.stt_audio(0)[3], b"RIFFbbb")
        self.assertEqual(m.stt_audio(1)[3], b"RIFFaaa")
        self.assertIsNone(m.stt_audio(2))
        m._stt_recent.clear(); m._stt_audio.clear()

    def test_hub_processes_get_the_host_timezone(self):
        """caldav-mcp formats event times in its process TZ — the manager hands
        every hub process the host zone; a catalog entry may still override."""
        import mgr.mcp as mcpmod
        old = mcpmod.HUB_TZ
        try:
            mcpmod.HUB_TZ = "Europe/Berlin"
            self.assertEqual(mcpmod.hub_env(None), {"TZ": "Europe/Berlin"})
            self.assertEqual(mcpmod.hub_env({"A": "1"}), {"TZ": "Europe/Berlin", "A": "1"})
            self.assertEqual(mcpmod.hub_env({"TZ": "UTC"})["TZ"], "UTC")
        finally:
            mcpmod.HUB_TZ = old
        self.assertEqual(self.m._mcp.HUB_TZ, self.m.HOST_TZ)

    def test_mcp_servers_validated_against_catalog(self):
        """The Policy tab assigns MCPs through the config route: names must
        exist in the catalog, spaces are tolerated, empty means none."""
        m = self.m
        old = m.load_mcps
        try:
            m.load_mcps = lambda: [{"name": "homeassistant"}, {"name": "caldav"}]
            self.assertEqual(m.mcp_servers_error("homeassistant, caldav"), "")
            self.assertEqual(m.mcp_servers_error(""), "")
            self.assertIn("calendar", m.mcp_servers_error("caldav,calendar"))
        finally:
            m.load_mcps = old

    def test_terminal_ws_requires_our_origin_and_page_is_served_by_manager(self):
        """H-2: the terminal WebSocket handshake needs a present, allowed Origin
        (403 otherwise). H-3: the terminal page comes from the manager's own
        webterm.py, never proxied from the guest."""
        m = self.m
        tunneled = []
        old = m.load_instances, m.is_running, m.net_of
        try:
            inst = {"name": "vm1", "index": 3, "template": "openrouter"}
            m.load_instances = lambda: [inst]; m.is_running = lambda i: True
            m.net_of = lambda i: {"guest": "172.30.3.2", "tap": "fc3"}
            def ws(path, origin=None):
                h = self._handler(path, "10.0.0.9")
                h.headers["Connection"] = "Upgrade"; h.headers["Upgrade"] = "websocket"
                if origin is not None:
                    h.headers["Origin"] = origin
                h._ws_tunnel = lambda guest, port, p: tunneled.append((guest, port, p)) or None
                h._term_route("vm1", "term/ws")
                return h
            self.assertEqual(self._status(ws("/i/vm1/term/ws")), 403)                              # no Origin
            self.assertEqual(self._status(ws("/i/vm1/term/ws", "http://evil.example")), 403)        # foreign
            self.assertEqual(tunneled, [])
            h = ws("/i/vm1/term/ws", "http://localhost:8700")                                        # ours
            self.assertEqual(tunneled, [("172.30.3.2", m.TERM_GUEST_PORT, "/ws")])
            # the page: manager-served, the guest proxy must NOT be consulted
            h = self._handler("/i/vm1/term/", "10.0.0.9")
            h._proxy = lambda *a, **k: (_ for _ in ()).throw(AssertionError("terminal page was proxied from the guest"))
            h._term_route("vm1", "term/")
            self.assertEqual(self._status(h), 200)
            self.assertIn(b"<!doctype html>", h.wfile.getvalue().lower())
        finally:
            m.load_instances, m.is_running, m.net_of = old

    def test_proxied_guest_html_is_sandboxed(self):
        """H-3: HTML relayed from a guest carries Content-Security-Policy: sandbox;
        JSON/plain answers (the chat API) do not."""
        m = self.m
        import types
        old = m.load_instances, m.is_running, m.net_of, m.urllib.request.urlopen, m.instance_by_ip
        try:
            inst = {"name": "vm1", "index": 3, "template": "openrouter"}
            m.load_instances = lambda: [inst]; m.is_running = lambda i: True
            m.net_of = lambda i: {"guest": "172.30.3.2", "tap": "fc3"}; m.instance_by_ip = lambda ip: None
            class Resp:
                def __init__(self, ct, body): self.status, self.headers, self._b = 200, {"Content-Type": ct}, body
                def read(self, n=-1):
                    b, self._b = self._b, b""; return b
                def __enter__(self): return self
                def __exit__(self, *a): return False
            for ct, path, want in (("text/html; charset=utf-8", "/i/vm1/", True),
                                   ("application/json", "/i/vm1/api/tools", False)):
                m.urllib.request.urlopen = lambda req, timeout=0, _ct=ct: Resp(_ct, b"<p>hi</p>" if "html" in _ct else b"{}")
                h = self._handler(path, "10.0.0.9"); h._do_GET()
                raw = h.wfile.getvalue()
                self.assertEqual(self._status(h), 200)
                self.assertEqual(b"content-security-policy: sandbox" in raw.lower(), want, (ct, raw[:200]))
        finally:
            m.load_instances, m.is_running, m.net_of, m.urllib.request.urlopen, m.instance_by_ip = old

    def test_memfs_git_never_runs_as_root_with_hooks(self):
        """C-1: git in the guest-writable memory folder must run as the guest
        user (when the manager is root) with every config-driven execution
        path disabled on the command line — a planted hook can neither run as
        root nor run at all."""
        import mgr.memfs as mf
        old = mf.OWNER, mf.os.geteuid
        try:
            mf.OWNER = (1234, 5678); mf.os.geteuid = lambda: 0
            cmd, env, kw = mf._git_invocation("/tmp/x", "commit", "-q", "-m", "m")
            self.assertEqual((kw.get("user"), kw.get("group"), kw.get("extra_groups")), (1234, 5678, [5678]))
            joined = " ".join(cmd)
            self.assertIn("core.hooksPath=/dev/null", joined)
            self.assertIn("core.fsmonitor=false", joined)
            self.assertEqual(env["GIT_CONFIG_GLOBAL"], "/dev/null"); self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
            self.assertEqual(cmd[-4:], ["commit", "-q", "-m", "m"])
            mf.os.geteuid = lambda: 1000                      # not root (tests, dev box): no user switch, flags stay
            cmd, env, kw = mf._git_invocation("/tmp/x", "add", "-A")
            self.assertEqual(kw, {}); self.assertIn("core.hooksPath=/dev/null", " ".join(cmd))
            mf.OWNER = None
            self.assertEqual(mf._git_invocation("/tmp/x", "add")[2], {})
        finally:
            mf.OWNER, mf.os.geteuid = old
        # and a hook planted in a real repo does not fire through _git
        import mgr.memfs as mf2
        tmp = tempfile.mkdtemp(prefix="e2e-c1-")
        r = mf2._git(tmp, "init", "-q"); self.assertIsNotNone(r)
        hooks = os.path.join(tmp, ".git", "hooks"); os.makedirs(hooks, exist_ok=True)
        marker = os.path.join(tmp, "PWNED")
        with open(os.path.join(hooks, "pre-commit"), "w") as fh:
            fh.write(f"#!/bin/sh\ntouch {marker}\n")
        os.chmod(os.path.join(hooks, "pre-commit"), 0o755)
        with open(os.path.join(tmp, "f.md"), "w") as fh:
            fh.write("x")
        mf2._git(tmp, "config", "user.email", "t@t"); mf2._git(tmp, "config", "user.name", "t")
        mf2._git(tmp, "add", "-A"); mf2._git(tmp, "commit", "-q", "-m", "c")
        self.assertFalse(os.path.exists(marker), "a planted pre-commit hook ran through the manager's git")

    def test_memory_folder_notes_timeline_coarsening(self):
        """Memory as files: a note per key with a regenerated index, one raw
        timeline entry per turn, coarsened deterministically (trimmed after
        RAW_DAYS, folded into a weekly file after DAILY_DAYS), git-committed."""
        import mgr.memfs as mf
        from datetime import date
        tmp = tempfile.mkdtemp(prefix="e2e-memfs-")
        old = mf.MEMORY_ROOT
        try:
            mf.MEMORY_ROOT = os.path.join(tmp, "memory")
            d = mf.folder("vc")
            self.assertTrue(os.path.isdir(os.path.join(d, ".git")))
            p = mf.note_write("vc", "Lieblingssender", "Swiss Classic, morgens leise.")
            self.assertTrue(p.endswith("notes/lieblingssender.md"))
            idx = mf.index_text("vc")
            self.assertIn("[[lieblingssender]] Lieblingssender — Swiss Classic", idx)
            mf.note_write("vc", "Lieblingssender", None)
            self.assertNotIn("lieblingssender", mf.index_text("vc"))
            self.assertIsNone(mf.folder("../x"))          # no traversal via the name
            # timeline: raw today, trimmed at 3 days, weekly at 15 days
            import time as _t
            now = _t.time()
            mf.timeline_add("vc", "voice", "Radio an " * 40, "OK.", when=now)
            mf.timeline_add("vc", "voice", "Wie spät?", "12:32.", when=now - 3 * 86400)
            mf.timeline_add("vc", "task", "MSFT-Kurs holen", "499.7", when=now - 20 * 86400)
            files = sorted(os.listdir(os.path.join(d, "timeline")))
            self.assertEqual(len(files), 3)
            raw_today = open(os.path.join(d, "timeline", files[-1])).read()
            self.assertIn("[voice] Radio an", raw_today)
            st = mf.coarsen("vc", today=date.today())
            self.assertEqual(st, {"trimmed": 1, "folded": 1})
            files = sorted(os.listdir(os.path.join(d, "timeline")))
            self.assertTrue(any("-W" in f for f in files), files)           # weekly file exists
            self.assertEqual(sum(1 for f in files if "-W" not in f), 2)    # today + the trimmed day
            self.assertTrue(mf.commit("vc", "test"))
            self.assertEqual(mf.coarsen("vc", today=date.today()), {"trimmed": 0, "folded": 0})
        finally:
            mf.MEMORY_ROOT = old

    def test_memory_folder_is_mounted_into_the_guest(self):
        """The folder rides the host-folder mechanism: exported to the guest
        only, mounted read-write at /memory; the config disk names it."""
        m = self.m
        import mgr.memfs as mf
        tmp = tempfile.mkdtemp(prefix="e2e-memmount-")
        old = mf.MEMORY_ROOT
        try:
            mf.MEMORY_ROOT = os.path.join(tmp, "memory")
            inst = {"name": "vc", "index": 8, "template": "openrouter", "rootfs": "instances/openrouter-rootfs.ext4",
                    "mounts": [{"host": tmp, "guest": "/mnt/x"}]}
            specs = m.mount_specs(inst)
            mem = [s for s in specs if s["guest"] == "/memory"]
            self.assertEqual(len(mem), 1)
            self.assertFalse(mem[0]["ro"])
            self.assertTrue(mem[0]["host"].endswith("/memory/vc"))
            self.assertEqual(mem[0]["fsid"], 4000 + 8 * 16 + 15)
            self.assertEqual(m.mount_specs({**inst, "template": "claude", "rootfs": "instances/claude-rootfs.ext4"})[0]["guest"], "/mnt/x")
        finally:
            mf.MEMORY_ROOT = old

    def test_secret_broker_requires_guest_readable(self):
        """A release lets the HUB substitute a key on the host; the raw value
        reaches a VM only when the key is also in guest_readable."""
        m = self.m
        guest = {"name": "hass", "template": "openrouter", "index": 7, "config": {}}
        old = m.instance_by_ip, m.load_secret_policy, m.secret_store
        try:
            m.instance_by_ip = lambda ip: guest if ip == "172.30.7.2" else None
            m.secret_store = lambda: {"HA_TOKEN": "t0k", "OPENROUTER_API_KEY": "k3y"}
            m.load_secret_policy = lambda: {"by_template": {"openrouter": ["OPENROUTER_API_KEY"]},
                                            "by_instance": {"hass": ["HA_TOKEN"]},
                                            "guest_readable": ["HA_TOKEN"]}
            self.assertEqual(m.allowed_secret_keys(guest), {"OPENROUTER_API_KEY", "HA_TOKEN"})
            self.assertEqual(m.guest_readable_keys(guest), {"HA_TOKEN"})
            h = self._handler("/api/secret/HA_TOKEN", "172.30.7.2"); h._do_GET()
            self.assertIn(b'"value": "t0k"', h.wfile.getvalue())
            h = self._handler("/api/secret/OPENROUTER_API_KEY", "172.30.7.2"); h._do_GET()
            self.assertIn(b" 403 ", h.wfile.getvalue().split(b"\r\n", 1)[0])   # released, not readable
            h = self._handler("/api/secrets", "172.30.7.2"); h._do_GET()
            self.assertIn(b'"allowed": ["HA_TOKEN"]', h.wfile.getvalue())
            # the saver keeps the third list, deduplicated and sorted
            tmp = tempfile.mkdtemp(prefix="e2e-secpol-"); oldf = m.SECRET_POLICY_FILE
            try:
                m.SECRET_POLICY_FILE = os.path.join(tmp, "p.json")
                m.save_secret_policy({"by_template": {}, "by_instance": {}, "guest_readable": ["B", "A", "B", 3]})
                self.assertEqual(json.load(open(m.SECRET_POLICY_FILE))["guest_readable"], ["A", "B"])
            finally:
                m.SECRET_POLICY_FILE = oldf
        finally:
            m.instance_by_ip, m.load_secret_policy, m.secret_store = old

    def test_harness_disk_rebuilds_when_agent_source_changes(self):
        """The agent code rides a read-only drive built from AGENT_SRC: built
        once, reused while the sources' content is unchanged (mtimes are not
        trusted), rebuilt when a file changes; an incomplete source set or no
        AGENT_SRC gives no drive. Every rootfs carrying this agent uses it."""
        m = self.m
        if not shutil.which("mkfs.ext4", path="/usr/sbin:/sbin:" + os.environ.get("PATH", "")):
            self.skipTest("mkfs.ext4 not available")
        tmp = tempfile.mkdtemp(prefix="e2e-harness-")
        src = os.path.join(tmp, "src"); os.makedirs(src); run = os.path.join(tmp, "run"); os.makedirs(run)
        for n in ("agent.py", "run_agent.py", "webterm.py"):
            with open(os.path.join(src, n), "w") as fh:
                fh.write(f"# {n}\n")
        old = m.AGENT_SRC, m.HARNESS_IMG, m._paths.RUN_DIR
        try:
            m.AGENT_SRC, m.HARNESS_IMG, m._paths.RUN_DIR = src, os.path.join(run, "harness.ext4"), run
            img = m.harness_image()
            self.assertEqual(img, m.HARNESS_IMG)
            self.assertTrue(os.path.exists(img) and os.path.exists(img + ".src"))
            ino1 = os.stat(img).st_ino
            os.utime(os.path.join(src, "agent.py"), (time.time() + 60,) * 2)   # a newer mtime alone
            self.assertEqual(m.harness_image(), img)
            self.assertEqual(os.stat(img).st_ino, ino1)                         # is no rebuild
            with open(os.path.join(src, "agent.py"), "a") as fh:
                fh.write("VERSION = 2\n")
            m.harness_image()
            self.assertNotEqual(os.stat(img).st_ino, ino1)                      # content change is
            self.assertEqual(len(os.listdir(run)), 2, os.listdir(run))          # no temp files left
            os.unlink(os.path.join(src, "agent.py"))
            self.assertEqual(m.harness_sources(), [])                           # incomplete: no drive
            inst = {"name": "x", "template": "openrouter", "rootfs": "instances/openrouter-rootfs.ext4", "index": 9}
            self.assertTrue(m.uses_harness(inst))
            self.assertTrue(m.uses_harness({**inst, "template": "llama"}))      # same image, same agent
            self.assertFalse(m.uses_harness({**inst, "template": "claude", "rootfs": "instances/claude-rootfs.ext4"}))
            m.AGENT_SRC = ""
            self.assertIsNone(m.harness_image())
        finally:
            m.AGENT_SRC, m.HARNESS_IMG, m._paths.RUN_DIR = old

    def test_secret_policy_seeds_guest_readable_on_upgrade(self):
        """A policy file from before the two-rights model has no guest_readable:
        it is seeded from the releases once (nothing breaks on upgrade) and
        the file is rewritten with the list; a file that has the key is left alone."""
        m = self.m
        tmp = tempfile.mkdtemp(prefix="e2e-secpol2-"); oldf, olds = m.SECRET_POLICY_FILE, m._settings.load_settings
        try:
            m.SECRET_POLICY_FILE = os.path.join(tmp, "p.json")
            legacy = {"by_template": {"openrouter": ["OPENROUTER_API_KEY"]},
                      "by_instance": {"hass": ["HA_TOKEN", "OPENROUTER_API_KEY"]}}
            with open(m.SECRET_POLICY_FILE, "w") as fh:
                json.dump(legacy, fh)
            m._settings.load_settings = lambda: {}
            self.assertEqual(m.load_secret_policy()["guest_readable"], ["HA_TOKEN", "OPENROUTER_API_KEY"])
            self.assertEqual(json.load(open(m.SECRET_POLICY_FILE))["guest_readable"], ["HA_TOKEN", "OPENROUTER_API_KEY"])
            with open(m.SECRET_POLICY_FILE, "w") as fh:
                json.dump(legacy, fh)
            m._settings.load_settings = lambda: {"LLM_KEY_PROXY": "1"}       # proxy on: the LLM key stays on the host
            self.assertEqual(m.load_secret_policy()["guest_readable"], ["HA_TOKEN"])
            m.save_secret_policy({"by_template": {"openrouter": ["OPENROUTER_API_KEY"]}, "by_instance": {}, "guest_readable": []})
            self.assertEqual(m.load_secret_policy()["guest_readable"], [])          # an explicit empty list stays
        finally:
            m.SECRET_POLICY_FILE, m._settings.load_settings = oldf, olds

    def test_stale_image_detection_and_rebuild_push(self):
        """A running VM started before its base image was rebuilt is 'stale':
        the API says so, and the idle sweep pushes once per rebuild, naming
        the affected instances — silent when nothing is affected."""
        m = self.m
        tmp = tempfile.mkdtemp(prefix="e2e-stale-")
        os.makedirs(os.path.join(tmp, "instances")); os.makedirs(os.path.join(tmp, "run"))
        img = os.path.join(tmp, "instances", "openrouter-rootfs.ext4")
        old = m._paths.BASE, m._paths.RUN_DIR, m.is_running, m.load_instances, m.notify_add, dict(m._img_seen), m.HARNESS_IMG
        pushes = []
        try:
            m._paths.BASE, m._paths.RUN_DIR = tmp, os.path.join(tmp, "run")
            m.HARNESS_IMG = os.path.join(tmp, "run", "harness.ext4")    # none built here: rootfs only
            m.is_running = lambda i: i["name"] != "off"
            insts = [{"name": "old", "rootfs": "instances/openrouter-rootfs.ext4"},
                     {"name": "fresh", "rootfs": "instances/openrouter-rootfs.ext4"},
                     {"name": "off", "rootfs": "instances/openrouter-rootfs.ext4"},
                     {"name": "priv", "rootfs": "instances/priv.ext4"}]
            m.load_instances = lambda: insts
            m.notify_add = lambda *a, **k: pushes.append(a)
            for n, ts in (("old", 1000), ("fresh", 3000), ("off", 1000), ("priv", 1000)):
                pf = os.path.join(tmp, "run", n + ".pid"); open(pf, "w").write("1"); os.utime(pf, (ts, ts))
            open(img, "w").write("x"); os.utime(img, (2000, 2000))
            self.assertEqual([m.image_state(i)[0] for i in insts], [True, False, False, False])
            self.assertEqual(m.stale_instances(), ["old"])
            m._img_seen.clear()
            self.assertEqual(m.image_sweep(), [])           # first sight: baseline, no push
            self.assertEqual(m.image_sweep(), [])           # unchanged: quiet
            os.utime(img, (4000, 4000))                      # rebuilt -> both VMs older now
            self.assertEqual(m.image_sweep(), ["old", "fresh"])
            self.assertEqual(len(pushes), 1)
            self.assertIn("old, fresh", pushes[0][2])
            self.assertEqual(m.image_sweep(), [])           # once per rebuild
        finally:
            m._paths.BASE, m._paths.RUN_DIR, m.is_running, m.load_instances, m.notify_add = old[:5]
            m._img_seen.clear(); m._img_seen.update(old[5]); m.HARNESS_IMG = old[6]

    def test_guest_config_carries_host_timezone(self):
        """Guests boot in UTC; the manager hands them the host's zone name."""
        m = self.m
        self.assertTrue(m.HOST_TZ)
        self.assertEqual(m.HOST_TZ, os.environ.get("GUEST_TZ") or m._host_tz())

    def test_task_model_reaches_the_ephemeral_vm(self):
        """spawn_subagent/create_task may name a model: it travels through
        /api/task (wait) and through the queue (worker) into _run_ephemeral;
        a named instance keeps its own model."""
        m = self.m
        import mgr.store as st
        seen = []
        old_eph, old_named, old_file = m._run_ephemeral, m._run_named, st.TASKS_FILE
        try:
            m._run_ephemeral = lambda msg, model=None, timeout=600: (seen.append(("eph", msg, model)), (True, "r"))[1]
            m._run_named = lambda inst, msg, timeout=600: (seen.append(("named", inst, msg)), (True, "r"))[1]
            m._run_task_now("ephemeral", "do", "google/gemini-2.5-flash")
            m._run_task_now("ephemeral", "do")
            m._run_task_now("hass", "do", "google/gemini-2.5-flash")
            self.assertEqual(seen, [("eph", "do", "google/gemini-2.5-flash"), ("eph", "do", None),
                                    ("named", "hass", "do")])
            st.TASKS_FILE = os.path.join(tempfile.mkdtemp(prefix="e2e-taskmodel-"), "tasks.json")
            t = st.add_task("ephemeral", "queued", "", model=" x/y ")
            self.assertEqual(st.load_tasks()[0]["model"], "x/y")
            t2 = st.add_task("ephemeral", "plain", "")
            self.assertNotIn("model", next(x for x in st.load_tasks() if x["id"] == t2["id"]))
        finally:
            m._run_ephemeral, m._run_named, st.TASKS_FILE = old_eph, old_named, old_file

    def test_task_target_resolved_and_validated(self):
        """'@orchestrator' (an agent's typo) failed daily with 'instance unknown'
        and nobody was told. Targets lose a leading '@', unknown names are
        refused at creation, and an edit may move a task to another instance."""
        m = self.m
        import mgr.store as st
        old_load, old_file = m.load_instances, st.TASKS_FILE
        try:
            m.load_instances = lambda: [{"name": "orchestrator"}, {"name": "hass"}]
            self.assertEqual(m.resolve_task_target("@orchestrator"), ("orchestrator", ""))
            self.assertEqual(m.resolve_task_target(" hass "), ("hass", ""))
            self.assertEqual(m.resolve_task_target(""), ("ephemeral", ""))
            self.assertEqual(m.resolve_task_target(None), ("ephemeral", ""))
            self.assertEqual(m.resolve_task_target("@")[0], "ephemeral")
            name, err = m.resolve_task_target("orchestartor")
            self.assertEqual(name, "")
            self.assertIn("unknown", err)
            tmp = tempfile.mkdtemp(prefix="e2e-tasktarget-")
            st.TASKS_FILE = os.path.join(tmp, "tasks.json")
            t = st.add_task("orchestrator", "msft", "daily 08:00")
            msg = st.update_task(t["id"], instance="hass")
            self.assertIn("updated", msg)
            self.assertEqual(st.load_tasks()[0]["instance"], "hass")
            self.assertEqual(st.load_tasks()[0]["schedule"], "daily 08:00")   # untouched
            # hourly sweep: a task whose target died is pushed ONCE, edit clears the mark
            st.add_task("@orchestrator", "typo target", "daily 08:00")
            pushes = []
            old_notify = m.notify_add
            m.notify_add = lambda *a, **k: pushes.append(a)
            try:
                hit = m.task_target_sweep()
                self.assertEqual([h[1] for h in hit], ["@orchestrator"])
                self.assertEqual(len(pushes), 1)
                self.assertIn("@orchestrator", pushes[0][2])
                self.assertEqual(m.task_target_sweep(), [])          # not again
                bad = next(t for t in st.load_tasks() if t["instance"] == "@orchestrator")
                st.update_task(bad["id"], instance="orchestrator")   # fixed -> mark gone
                self.assertNotIn("target_warned", next(t for t in st.load_tasks() if t["id"] == bad["id"]))
            finally:
                m.notify_add = old_notify
        finally:
            m.load_instances, st.TASKS_FILE = old_load, old_file

    def test_mission_edit_and_delete_any_status(self):
        """The UI may correct or remove a mission in ANY state: goal/steps/
        status editable (positions keep their progress, reopening respects the
        active cap), delete removes done/failed/active alike, owner resolved
        from the id when the caller does not know it."""
        m = self.m
        tmp = tempfile.mkdtemp(prefix="e2e-miedit-")
        import mgr.missions as mmod
        old_file, old_notify = mmod.MISSIONS_FILE, mmod.notify_add
        try:
            mmod.MISSIONS_FILE = os.path.join(tmp, "missions.json")
            mmod.notify_add = lambda *a, **k: ("x", "ok")
            mid, _ = m.mission_start("hass", "Goal A", ["s1", "s2", "s3"])
            m.mission_update("hass", mid, step=1, status="done", result="r1")
            m.mission_finish("hass", mid, "all good")            # -> done
            # edit a DONE mission (no instance given -> owner lookup)
            self.assertEqual(m.mission_edit("", mid, goal="Goal B",
                                            steps=["s1", "s2x", "s3", "s4"]), "ok")
            mi = m.mission_list("hass")[0]
            self.assertEqual(mi["goal"], "Goal B")
            self.assertEqual([s["text"] for s in mi["steps"]], ["s1", "s2x", "s3", "s4"])
            self.assertEqual(mi["steps"][0]["status"], "done")   # progress kept by position
            self.assertEqual(mi["steps"][0]["result"], "r1")
            self.assertEqual(mi["steps"][3]["status"], "open")   # new step
            self.assertEqual(mi["status"], "done")
            # reopen -> active; the agent's mission_update works again
            self.assertEqual(m.mission_edit("hass", mid, status="active"), "ok")
            self.assertEqual(m.mission_update("hass", mid, step=4, status="doing"), "ok")
            self.assertEqual(m.mission_edit("hass", mid, status="active"), "unchanged")
            self.assertIn("unknown status", m.mission_edit("hass", mid, status="weird"))
            self.assertEqual(m.mission_edit("hass", mid, goal="  "), "goal missing")
            self.assertEqual(m.mission_edit("hass", mid, steps=[]), "steps missing")
            self.assertEqual(m.mission_edit("hass", "m-nope"), "unknown mission")
            # cap: 5 active others -> reopening a failed one is refused
            for i in range(m.MISSION_MAX_ACTIVE):
                m.mission_start("cap", f"g{i}", ["x"])
            fid, _ = m.mission_start("cap", "late", ["x"])     # None: cap reached
            self.assertIsNone(fid)
            m.mission_finish("cap", m.mission_list("cap")[0]["id"], "bad", failed=True)
            m.mission_start("cap", "fill", ["x"])                 # back at the cap
            failed_id = m.mission_list("cap")[0]["id"]
            self.assertIn("max", m.mission_edit("cap", failed_id, status="active"))
            # delete: failed, then active, then unknown
            self.assertEqual(m.mission_delete("", failed_id), "ok")
            self.assertEqual(m.mission_delete("hass", mid), "ok")
            self.assertEqual(m.mission_list("hass"), [])
            self.assertEqual(m.mission_delete("hass", mid), "unknown mission")
        finally:
            mmod.MISSIONS_FILE, mmod.notify_add = old_file, old_notify

    def test_mission_cross_instance(self):
        """Multi-owner missions: ANY agent owns missions, the steps carry the
        instance they were delegated to, and admin actions find the owner from
        the id alone (web UI/app only know the mission id)."""
        m = self.m
        tmp = tempfile.mkdtemp(prefix="e2e-mi3-")
        import mgr.missions as mmod
        old_file, old_notify = mmod.MISSIONS_FILE, mmod.notify_add
        try:
            mmod.MISSIONS_FILE = os.path.join(tmp, "missions.json")
            mmod.notify_add = lambda *a, **k: ("x", "ok")
            mid, _ = m.mission_start("jobresearcher", "cross-instance goal", ["s1", "s2"])
            self.assertTrue(mid)
            # Step 1 is executed by a DIFFERENT agent than the owner.
            self.assertEqual(m.mission_update("jobresearcher", mid, step=1, status="doing",
                                              task_id="t-x1", target="hass"), "ok")
            st1 = m.mission_list("jobresearcher")[0]["steps"][0]
            self.assertEqual(st1["target"], "hass")
            self.assertIn("@hass", m.mission_list("jobresearcher")[0]["log"][-1])
            # The advance trigger has to find the OWNER, not the executor.
            inst, mi, st = m.mission_for_task("t-x1")
            self.assertEqual((inst, mi["id"], st["n"]), ("jobresearcher", mid, 1))
            self.assertEqual(m.mission_owner(mid), "jobresearcher")
            self.assertIsNone(m.mission_owner("m-nope"))
            # Admin action without an instance resolves the owner itself.
            self.assertEqual(m.mission_admin("", mid, "pause"), "ok")
            self.assertEqual(m.mission_list("jobresearcher")[0]["status"], "paused")
            self.assertEqual(m.mission_admin("", mid, "resume"), "ok")
            self.assertEqual(m.mission_admin("", "m-nope", "pause"), "unknown mission")
        finally:
            mmod.MISSIONS_FILE, mmod.notify_add = old_file, old_notify

    def test_idle_heartbeat_skip_persists_and_does_not_reskip(self):
        """Regression (2026-09-03, 9.961 Logzeilen): ein Skip mutiert next_run
        und MUSS dirty=True melden — sonst wirft with_tasks das Weiterplanen
        weg und derselbe Heartbeat wird alle 5 s neu geskippt. Vertragstest
        direkt gegen worker_claim."""
        m = self.m
        now = int(time.time())
        hb = {"id": "hb1", "instance": "orchestrator", "status": "scheduled",
              "message": "/fresh Heartbeat: x", "schedule": "every 30m",
              "next_run": 0}
        tasks = [hb]
        skipped, throttled = [], []
        dirty, claimed = m.worker_claim(tasks, now, lambda t: True, skipped, throttled)
        self.assertTrue(dirty, "Skip mutiert next_run — MUSS dirty melden")
        self.assertIsNone(claimed)
        self.assertEqual(skipped, ["hb1"])
        self.assertGreater(hb["next_run"], now, "next_run muss vorruecken")
        # Zweiter Zyklus auf dem (nun gespeicherten) Stand: nichts mehr faellig.
        skipped2 = []
        dirty2, claimed2 = m.worker_claim(tasks, now + 5, lambda t: True, skipped2, [])
        self.assertFalse(dirty2)
        self.assertIsNone(claimed2)
        self.assertEqual(skipped2, [], "5 s spaeter darf NICHT erneut geskippt werden")
        # Nicht-idle (hb_idle False): der Task wird normal geclaimt.
        hb["next_run"] = 0
        dirty3, claimed3 = m.worker_claim(tasks, now, lambda t: False, [], [])
        self.assertTrue(dirty3)
        self.assertEqual(claimed3["id"], "hb1")
        self.assertEqual(hb["status"], "running")

    def test_mission_advance_collects_bursts_into_one_push(self):
        """Collect mode: every advance push is a full /fresh turn with ~5k fixed
        input tokens. Several tasks finishing inside the window must produce ONE
        push that lists them all — not one turn each."""
        m = self.m
        tmp = tempfile.mkdtemp(prefix="e2e-mi5-")
        import mgr.missions as mmod
        old_file, old_notify = mmod.MISSIONS_FILE, mmod.notify_add
        old_run, old_load, old_win = m._run_named, m.load_instances, m.MISSION_COLLECT_SECS
        pushes = []
        done = threading.Event()
        try:
            mmod.MISSIONS_FILE = os.path.join(tmp, "missions.json")
            mmod.notify_add = lambda *a, **k: ("x", "ok")
            m.load_instances = lambda: [{"name": "owner-a"}]
            m._run_named = lambda inst, msg: (pushes.append((inst, msg)), done.set(), (True, "ok"))[-1]
            m.MISSION_COLLECT_SECS = 0.3
            mid, _ = m.mission_start("owner-a", "burst goal", ["s1", "s2", "s3"])
            for n, tid in ((1, "t-b1"), (2, "t-b2"), (3, "t-b3")):
                m.mission_update("owner-a", mid, step=n, status="doing", task_id=tid)
                m._mission_advance_fire(tid)
            self.assertTrue(done.wait(5), "no push fired")
            time.sleep(0.4)                            # window fully drained
            self.assertEqual(len(pushes), 1, f"expected ONE push, got {len(pushes)}")
            inst, msg = pushes[0]
            self.assertEqual(inst, "owner-a")
            for tid in ("t-b1", "t-b2", "t-b3"):
                self.assertIn(tid, msg)
        finally:
            mmod.MISSIONS_FILE, mmod.notify_add = old_file, old_notify
            m._run_named, m.load_instances = old_run, old_load
            m.MISSION_COLLECT_SECS = old_win

    def test_mission_advance_fires_at_owner(self):
        """Regression guard: the push after a finished task goes to the mission's
        owner — previously it was hard-wired to the orchestrator, so a mission
        owned by any other agent would never advance. The push is debounced by
        the collect window, hence the shortened window here."""
        m = self.m
        tmp = tempfile.mkdtemp(prefix="e2e-mi4-")
        import mgr.missions as mmod
        old_file, old_notify = mmod.MISSIONS_FILE, mmod.notify_add
        old_run, old_load, old_win = m._run_named, m.load_instances, m.MISSION_COLLECT_SECS
        fired = []
        done = threading.Event()
        try:
            mmod.MISSIONS_FILE = os.path.join(tmp, "missions.json")
            mmod.notify_add = lambda *a, **k: ("x", "ok")
            m.MISSION_COLLECT_SECS = 0.2
            m.load_instances = lambda: [{"name": "jobresearcher"}, {"name": "orchestrator"}]
            m._run_named = lambda inst, msg: (fired.append(inst), done.set(), (True, "ok"))[-1]
            mid, _ = m.mission_start("jobresearcher", "owned elsewhere", ["s1"])
            m.mission_update("jobresearcher", mid, step=1, status="doing",
                             task_id="t-y1", target="hass")
            m._mission_advance_fire("t-y1")
            self.assertTrue(done.wait(5), "no advance push fired")
            self.assertEqual(fired, ["jobresearcher"])
            # Owner gone -> no push (the TTL sweep pauses the mission instead).
            fired.clear(); done.clear()
            m.load_instances = lambda: [{"name": "orchestrator"}]
            m._mission_advance_fire("t-y1")
            self.assertFalse(done.wait(0.6))
            self.assertEqual(fired, [])
        finally:
            mmod.MISSIONS_FILE, mmod.notify_add = old_file, old_notify
            m._run_named, m.load_instances = old_run, old_load
            m.MISSION_COLLECT_SECS = old_win

    def test_mission_caps(self):
        m = self.m
        tmp = tempfile.mkdtemp(prefix="e2e-mi2-")
        import mgr.missions as mmod
        old_file = mmod.MISSIONS_FILE
        try:
            mmod.MISSIONS_FILE = os.path.join(tmp, "missions.json")
            for i in range(m.MISSION_MAX_ACTIVE):
                self.assertTrue(m.mission_start("o", f"g{i}", ["s"])[0])
            self.assertIsNone(m.mission_start("o", "zuviel", ["s"])[0])
        finally:
            mmod.MISSIONS_FILE = old_file

    def test_task_store_survives_concurrent_writers(self):
        """The lock used to guard only the WRITE: worker and HTTP threads both
        did load→modify→save, and an interleaving silently dropped tasks. All
        mutations go through with_tasks() now — this hammers the store from
        four threads and demands an exact result."""
        import mgr.store as st
        tmp = tempfile.mkdtemp(prefix="e2e-race-")
        old = st.TASKS_FILE
        try:
            st.TASKS_FILE = os.path.join(tmp, "tasks.json")
            ids, errs = [], []

            def adder(n):
                try:
                    for i in range(25):
                        ids.append(st.add_task(f"inst{n}", f"job {n}-{i}")["id"])
                except Exception as e:
                    errs.append(repr(e))

            threads = [threading.Thread(target=adder, args=(n,)) for n in range(4)]
            for th in threads:
                th.start()
            # Waehrenddessen mutiert ein fuenfter Thread Status-Felder — die
            # Rolle des Workers.
            def toucher():
                for _ in range(40):
                    st.with_tasks(lambda ts: (bool(ts), [t.update(
                        {"updated": int(time.time())}) for t in ts] and True))
            tt = threading.Thread(target=toucher)
            tt.start()
            for th in threads + [tt]:
                th.join()
            self.assertEqual(errs, [])
            stored = {t["id"] for t in st.load_tasks()}
            self.assertEqual(len(ids), 100)
            self.assertEqual(stored, set(ids), "lost update: created tasks vanished")
        finally:
            st.TASKS_FILE = old

    def test_add_task_roundtrip(self):
        """Regression: the mgr/ split shipped store.py without `import uuid`,
        and for 13 days NO new task could be created (UI, app, create_task) —
        unnoticed because the pre-split scheduled tasks kept running and
        wait=true bypasses add_task. This test calls the real thing."""
        import mgr.store as st
        tmp = tempfile.mkdtemp(prefix="e2e-task-")
        old = st.TASKS_FILE
        try:
            st.TASKS_FILE = os.path.join(tmp, "tasks.json")
            t = st.add_task("someinst", "do the thing")
            self.assertTrue(t["id"] and len(t["id"]) == 12)
            self.assertEqual(t["status"], "pending")
            t2 = st.add_task("someinst", "recurring", "every 7d")
            self.assertEqual(t2["status"], "scheduled")
            self.assertGreater(t2["next_run"], int(time.time()) - 5)
            ids = {x["id"] for x in st.load_tasks()}
            self.assertEqual(ids, {t["id"], t2["id"]})
        finally:
            st.TASKS_FILE = old

    def test_tasks_file_wired(self):
        """Regression: TASKS_FILE must be set in store.configure() — otherwise
        load_tasks crashes (bug from 2026-08-20, /api/tasks returned nothing)."""
        import mgr.store as st
        self.assertTrue(st.TASKS_FILE and st.TASKS_FILE.endswith("tasks.json"))
        old = st.TASKS_FILE                      # the live file is root-only now: read a copy
        try:
            st.TASKS_FILE = os.path.join(tempfile.mkdtemp(prefix="e2e-tasks-"), "tasks.json")
            with open(st.TASKS_FILE, "w") as fh:
                fh.write("[]")
            self.assertIsInstance(st.load_tasks(), list)
        finally:
            st.TASKS_FILE = old

    def test_irohgw_allowlist_roundtrip(self):
        """iroh app-transport pairing: add/remove phone node-ids; only 64-hex
        ids are accepted; a missing gateway node-id reads as unavailable."""
        import mgr.irohgw as ig
        tmp = tempfile.mkdtemp(prefix="e2e-iroh-")
        ig.configure(tmp)
        nid = "a" * 64
        self.assertFalse(ig.status()["available"])          # no nodeid.txt yet
        ok, _ = ig.allow_add("nothex", "x")
        self.assertFalse(ok)                                 # rejected: not 64 hex
        ok, _ = ig.allow_add(nid, "Phone")
        self.assertTrue(ok)
        self.assertEqual(ig.load_allow(), [{"id": nid, "label": "Phone"}])
        ig.allow_add(nid, "Phone")                           # idempotent
        self.assertEqual(len(ig.load_allow()), 1)
        ig.allow_remove(nid)
        self.assertEqual(ig.load_allow(), [])
        # gateway node-id surfaces once the gateway writes nodeid.txt
        with open(os.path.join(tmp, "iroh-gw", "nodeid.txt"), "w") as fh:
            fh.write("b" * 64 + "\n")
        self.assertEqual(ig.gateway_node_id(), "b" * 64)
        self.assertTrue(ig.status()["available"])

    def test_reclaim_stuck_tasks(self):
        m = self.m
        import mgr.store as st
        tmp = tempfile.mkdtemp(prefix="e2e-rc-")
        old = st.TASKS_FILE
        try:
            st.TASKS_FILE = os.path.join(tmp, "tasks.json")
            st.save_tasks([{"id": "a", "status": "running", "schedule": "daily 07:00"},
                           {"id": "b", "status": "running"},          # einmalig
                           {"id": "c", "status": "done"}])
            m.reclaim_stuck_tasks()
            by = {t["id"]: t["status"] for t in st.load_tasks()}
            self.assertEqual(by["a"], "scheduled")   # geplant -> scheduled
            self.assertEqual(by["b"], "pending")      # einmalig -> pending
            self.assertEqual(by["c"], "done")         # unberuehrt
        finally:
            st.TASKS_FILE = old

    def test_leak_filter(self):
        import mgr.gateway as g
        red, n = g.redact_secrets("key sk-or-v1-abcdef0123456789xyz and ptr_ABCDEFGHIJ1234567890")
        self.assertEqual(n, 2)
        self.assertNotIn("sk-or-v1", red)
        self.assertNotIn("ptr_ABCD", red)
        # HuggingFace token is masked
        self.assertEqual(g.redact_secrets("tok hf_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345 x")[1], 1)
        self.assertEqual(g.redact_secrets("normaler Text")[1], 0)
        # git-SHA (40 hex) darf NICHT als Secret gelten
        self.assertEqual(g.redact_secrets("commit deadbeef00112233445566778899aabbccddeeff")[1], 0)

    def test_guard_rate_limit(self):
        m = self.m
        inst = {"name": "e2e-guard-r", "config": {"LLM_RATE_MIN": "2", "BUDGET_TOKENS": "0"}}
        import mgr.store  # loaded just to be safe
        r1 = m._guard_check(inst)[0]
        r2 = m._guard_check(inst)[0]
        r3, why = m._guard_check(inst)
        self.assertTrue(r1 and r2)
        self.assertFalse(r3)
        self.assertIn("rate", why)
        self.assertTrue(m._guard_check(None)[0])   # Admin/Host immer frei

    def test_usage_for_shape(self):
        m = self.m
        d = m.usage_for("orchestrator", 0)
        self.assertEqual(set(d.keys()), {"calls", "in", "out", "cost"})
        self.assertIsInstance(d["calls"], int)
        z = m.usage_for("gibtsnichtxyz", 0)     # unknown instance -> zeros
        self.assertEqual(z["calls"], 0)

    def test_prompt_store(self):
        m = self.m
        tmp = tempfile.mkdtemp(prefix="e2e-pr-")
        import mgr.rules as rmod
        old = rmod.PROMPTS_FILE
        try:
            rmod.PROMPTS_FILE = os.path.join(tmp, "prompts.json")
            self.assertEqual(m.prompt_upsert("Daily!", "Text"), "saved")   # Name normalisiert
            self.assertEqual(m.load_prompts()[0]["name"], "daily")
            self.assertEqual(m.prompt_upsert("daily", "New"), "saved")     # update
            self.assertEqual(m.load_prompts()[0]["text"], "New")
            self.assertIn("built-in", m.prompt_upsert("reset", "x"))       # reserved
            self.assertEqual(m.prompt_delete("daily"), "deleted")
            self.assertEqual(m.prompt_delete("daily"), "unknown")
        finally:
            rmod.PROMPTS_FILE = old

    def test_notify_store(self):
        m = self.m
        tmp = tempfile.mkdtemp(prefix="e2e-notif-")
        import mgr.notify as nmod
        old_file, old_sent = nmod.NOTIF_FILE, list(nmod._notif_sent)
        try:
            nmod.NOTIF_FILE = os.path.join(tmp, "notifications.json")
            nmod._notif_sent.clear()
            nid, note = m.notify_add("orchestrator", "Title", "Text")
            self.assertTrue(nid)
            lst = m.load_notifications()
            self.assertEqual(len(lst), 1)
            self.assertEqual(lst[0]["title"], "Title")
            self.assertFalse(lst[0]["read"])
            self.assertEqual(m.notif_mark_read(mark_all=True), 1)
            self.assertTrue(m.load_notifications()[0]["read"])
            self.assertIsNone(m.notify_add("x", "", "")[0])   # leer -> nichts
        finally:
            nmod.NOTIF_FILE = old_file
            nmod._notif_sent[:] = old_sent

    def test_mcp_caldav_secret_stays_on_the_host(self):
        """The caldav catalog entry takes its password from the host secret
        store, and only for an instance the policy released it to. Without the
        release the placeholder survives unsubstituted — the guest never sees a
        credential, and a missing release is visible instead of silent."""
        m = self.m
        import mgr.mcp as mcpmod
        # Self-contained: a catalog entry and a policy of its own, so the test
        # does not depend on this host's catalog (a fresh install has none).
        entry = {"name": "caldav", "command": "caldav-mcp", "args": [],
                 "env": {"CALDAV_BASE_URL": "https://cal.example.com/dav", "CALDAV_USERNAME": "me",
                         "CALDAV_PASSWORD": "${CALDAV_PASSWORD}"}}
        old = mcpmod.secret_store, mcpmod.load_mcps, m.load_mcps, m.load_secret_policy
        mcpmod.load_mcps = m.load_mcps = lambda: [entry]
        m.load_secret_policy = lambda: {"by_template": {}, "guest_readable": [],
                                        "by_instance": {"myassistant": ["CALDAV_PASSWORD"], "voicecommand": ["CALDAV_PASSWORD"]}}
        self.assertIn("caldav", [x["name"] for x in m.load_mcps()])
        self.assertEqual(m.mcp_required_secrets(["caldav"]), {"CALDAV_PASSWORD"})
        for name in ("myassistant", "voicecommand"):
            self.assertIn("CALDAV_PASSWORD",
                          m.allowed_secret_keys({"name": name, "template": "openrouter"}),
                          f"{name} has no CALDAV_PASSWORD release")
        mcpmod.secret_store = lambda: {"CALDAV_PASSWORD": "s3cret"}
        try:
            env = json.loads(m.build_mcp_config(
                ["caldav"], allowed={"CALDAV_PASSWORD"}))["mcpServers"]["caldav"]["env"]
            self.assertEqual(env["CALDAV_PASSWORD"], "s3cret")
            env = json.loads(m.build_mcp_config(
                ["caldav"], allowed=set()))["mcpServers"]["caldav"]["env"]
            self.assertEqual(env["CALDAV_PASSWORD"], "${CALDAV_PASSWORD}")
        finally:
            mcpmod.secret_store, mcpmod.load_mcps, m.load_mcps, m.load_secret_policy = old

    # ---- guest boundary (multi-tenancy S-fixes, 2026-09-06) ------------------
    def _handler(self, path, ip, method="GET", auth=None):
        """A handler object without a socket: enough of BaseHTTPRequestHandler's
        state for _auth/_do_GET to run and write their response into a buffer."""
        import email.message
        m = self.m
        h = object.__new__(m.H)
        h.path, h.command, h.request_version = path, method, "HTTP/1.1"
        h.requestline = f"{method} {path} HTTP/1.1"
        h.client_address = (ip, 40000)
        h.headers = email.message.Message()
        if auth:
            h.headers["Authorization"] = auth
        h.rfile, h.wfile = io.BytesIO(b""), io.BytesIO()
        h.close_connection = True
        return h

    def _post_handler(self, path, ip, body=b"", origin=None, ctype="application/json"):
        h = self._handler(path, ip, method="POST")
        h.rfile = io.BytesIO(body)
        h.headers["Content-Length"] = str(len(body))
        h.headers["Content-Type"] = ctype
        if origin is not None:
            h.headers["Origin"] = origin
        return h

    @staticmethod
    def _status(h):
        return int(h.wfile.getvalue().split(b" ", 2)[1] or 0)

    def test_request_body_is_capped(self):
        """A body over the cap is answered 413 without being read; a bad or
        negative Content-Length is an empty body, not a hang."""
        m = self.m
        old = m.instance_by_ip, m.PW
        try:
            m.instance_by_ip = lambda ip: None
            m.PW = ""
            h = self._post_handler("/api/settings", "10.0.0.5", b"{}")
            h.headers.replace_header("Content-Length", str(m.BODY_MAX + 1))
            h.do_POST()
            self.assertEqual(self._status(h), 413)
            self.assertTrue(h.close_connection)
            h = self._post_handler("/api/tasks", "10.0.0.5", b"")
            h.headers.replace_header("Content-Length", "-5")
            self.assertEqual(h._raw(), b"")
            h.headers.replace_header("Content-Length", "abc")
            self.assertEqual(h._raw(), b"")
            with self.assertRaises(m.BodyTooLarge):
                h.headers.replace_header("Content-Length", str(m.BODY_MAX_AUDIO + 1)); h._raw(m.BODY_MAX_AUDIO)
        finally:
            m.instance_by_ip, m.PW = old

    def test_cross_site_post_is_refused(self):
        """CSRF: a browser on another site (or a DNS-rebound name) sends its
        Origin and is refused; our own origins pass; clients without an Origin
        (app, desktop, curl) are untouched. Guests never carry one."""
        m = self.m
        old = m.instance_by_ip, m.PW, dict(m._trusted_cache)
        try:
            m.instance_by_ip = lambda ip: None
            m.PW = ""
            m._trusted_cache.update(ts=time.time() + 3600, hosts={"agents.example.com", "localhost", "192.168.1.10"})
            self.assertTrue(m.origin_allowed(""))
            self.assertTrue(m.origin_allowed("https://agents.example.com"))
            self.assertTrue(m.origin_allowed("http://192.168.1.10:8700"))
            self.assertFalse(m.origin_allowed("https://evil.example.org"))
            self.assertFalse(m.origin_allowed("null"))
            h = self._post_handler("/api/settings", "10.0.0.5", b"{}", origin="https://evil.example.org")
            h.do_POST()
            self.assertEqual(self._status(h), 403)
            self.assertIn(b"cross-site", h.wfile.getvalue())
            h = self._post_handler("/api/nonexistent", "10.0.0.5", b"{}", origin="https://agents.example.com")
            h.do_POST()
            self.assertEqual(self._status(h), 404)          # passed the guard, no such route
        finally:
            m.instance_by_ip, m.PW = old[:2]
            m._trusted_cache.update(old[2])

    def test_responses_carry_hardening_headers(self):
        m = self.m
        old = m.instance_by_ip
        try:
            m.instance_by_ip = lambda ip: None
            h = self._handler("/api/agents", "10.0.0.5")
            h._json({"ok": 1})
            head = h.wfile.getvalue().split(b"\r\n\r\n", 1)[0].lower()
            self.assertIn(b"x-content-type-options: nosniff", head)
            self.assertIn(b"x-frame-options: sameorigin", head)
        finally:
            m.instance_by_ip = old

    def test_terminal_tunnel_forwards_only_upgrade_headers(self):
        """The browser's Authorization (Traefik's BasicAuth passes it on) and
        cookies must never reach a VM's webterm."""
        m = self.m
        items = [("Host", "agents.example.com"), ("Authorization", "Basic abc"), ("Cookie", "s=1"),
                 ("Upgrade", "websocket"), ("Connection", "Upgrade"), ("Sec-WebSocket-Key", "k"),
                 ("Sec-WebSocket-Version", "13"), ("X-Forwarded-For", "1.2.3.4")]
        kept = dict(m.ws_forward_headers(items))
        self.assertEqual(set(kept), {"Host", "Upgrade", "Connection", "Sec-WebSocket-Key", "Sec-WebSocket-Version"})

    def test_chat_log_only_from_signal_guests_and_marked_in_inbox(self):
        """Only a Signal-transport VM may file user turns; the inbox marks them
        as relayed by that agent so the orchestrator knows who spoke."""
        m = self.m
        web = {"name": "web1", "template": "openrouter", "index": 3, "config": {"TRANSPORT": "web"}}
        old = m.instance_by_ip, m.load_chats, m._inbox_wm, m.INBOX_WM_FILE
        try:
            m.instance_by_ip = lambda ip: web if ip == "172.30.3.2" else None
            h = self._post_handler("/api/chat-log", "172.30.3.2", b'{"sender":"x","user":"hi"}')
            h.do_POST()
            self.assertEqual(self._status(h), 403)
            m.load_chats = lambda: [
                {"id": "sig-hass-4915", "instance": "hass", "title": "Signal", "updatedAt": 5000,
                 "messages": [{"user": True, "text": "Licht aus"}]},
                {"id": "web-1", "instance": "orchestrator", "title": "Web", "updatedAt": 5000,
                 "messages": [{"user": True, "text": "Plan"}]}]
            m._inbox_wm = lambda: 0
            items = {i["id"]: i for i in m.inbox_since(peek=True)}
            self.assertEqual(items["sig-hass-4915"]["via"], "signal:hass")
            self.assertTrue(items["sig-hass-4915"]["text"].startswith("[Signal message relayed by agent 'hass'] Licht aus"))
            self.assertNotIn("via", items["web-1"])
            self.assertEqual(items["web-1"]["text"], "Plan")
        finally:
            m.instance_by_ip, m.load_chats, m._inbox_wm, m.INBOX_WM_FILE = old

    def test_mount_validation_and_guest_mount_list(self):
        """A host folder never exposes the manager tree, the agent sources or
        ~/.ssh; a guest path is never a system directory. The guest fetches
        its own list by IP from /api/mounts instead of the shared workspace."""
        m = self.m
        tmp = tempfile.mkdtemp(prefix="e2e-mounts-")
        share = os.path.join(tmp, "share"); os.makedirs(share)
        old = m.BROWSE_ROOTS, m.instance_by_ip, m.mount_specs, m.load_instances
        try:
            m.BROWSE_ROOTS = (tmp,)
            self.assertEqual(m.mount_error(share, "/home/node/data"), "")
            self.assertIn("not a directory", m.mount_error(os.path.join(tmp, "nope"), "/x"))
            self.assertIn("must be under", m.mount_error("/srv", "/x"))
            m.BROWSE_ROOTS = ("/",)
            self.assertIn("would expose", m.mount_error(m._paths.BASE, "/x"))
            self.assertIn("would expose", m.mount_error(os.path.dirname(m._paths.BASE), "/x"))   # contains it
            for bad in ("/bin", "/usr/local", "/etc/x", "/app", "/harness", "/config", "/memory", "/", "rel", "/a/../etc"):
                self.assertTrue(m.mount_error(share, bad), bad)
            m.BROWSE_ROOTS = (tmp,)
            self.assertIn("error:", m.set_mounts.__doc__ or "error:")     # documented below via the route
            inst = {"name": "vm1", "index": 4, "template": "openrouter", "rootfs": "instances/openrouter-rootfs.ext4", "mounts": []}
            m.load_instances = lambda: [inst]
            m.mount_specs = lambda i: [{"sub": m.FCMNT_ROOT + "/vm1/0", "guest": "/home/node/data", "ro": True}]
            m.instance_by_ip = lambda ip: inst if ip == "172.30.4.2" else None
            h = self._handler("/api/mounts", "172.30.4.2"); h._do_GET()
            self.assertIn(m.FCMNT_ROOT.encode() + b"/vm1/0|/home/node/data|ro\n", h.wfile.getvalue())
            h = self._handler("/api/mounts?instance=nope", "10.0.0.5"); h._do_GET()
            self.assertEqual(self._status(h), 404)
        finally:
            m.BROWSE_ROOTS, m.instance_by_ip, m.mount_specs, m.load_instances = old

    def test_set_mounts_refuses_bad_folders(self):
        m = self.m
        tmp = tempfile.mkdtemp(prefix="e2e-setm-")
        old = m.load_instances, m.BROWSE_ROOTS, m._paths.INST_DIR
        try:
            m._paths.INST_DIR = tmp
            m.BROWSE_ROOTS = (tmp,)
            inst = {"name": "vm1", "index": 4, "template": "openrouter", "rootfs": "instances/openrouter-rootfs.ext4", "mounts": []}
            m.load_instances = lambda: [inst]
            m.mount_specs = m.mount_specs
            r = m.set_mounts("vm1", [{"host": tmp, "guest": "/bin"}])
            self.assertTrue(r.startswith("error:"), r)
            self.assertFalse(os.path.exists(os.path.join(tmp, "vm1.json")))     # nothing saved
        finally:
            m.load_instances, m.BROWSE_ROOTS, m._paths.INST_DIR = old

    def test_js_json_and_download_name_and_rate(self):
        m = self.m
        self.assertNotIn("</script>", m.js_json({"d": "</script><img src=x>"}))
        self.assertEqual(json.loads(m.js_json({"d": "</script>"}))["d"], "</script>")
        self.assertEqual(m.download_name('a"b\r\nc.txt'), "a_b_c.txt")
        self.assertEqual(m.download_name(""), "file")
        key = ("t", "x")
        self.assertTrue(all(m.rate_ok(key, 3, 60) for _ in range(3)))
        self.assertFalse(m.rate_ok(key, 3, 60))

    def test_usage_report_accepted_for_local_models_only_when_proxied(self):
        """Key proxy on: the agent's figures are ignored — except a local model
        (LLAMA_ENDPOINT) called directly, whose report is flagged `direct`."""
        m = self.m
        old = m._settings.load_settings
        try:
            m._settings.load_settings = lambda: {"LLM_KEY_PROXY": "1"}
            llama = {"name": "u", "config": {"LLAMA_ENDPOINT": "http://10.0.0.5:8080/"}}
            self.assertTrue(m.usage_report_accepted(llama, {"direct": True}))
            self.assertFalse(m.usage_report_accepted(llama, {}))                       # old agent: not flagged
            self.assertFalse(m.usage_report_accepted({"name": "o", "config": {}}, {"direct": True}))  # proxied instance
            self.assertTrue(m.usage_report_accepted({"name": "c", "template": "claude", "config": {}}, {"direct": True}))  # claude = direct
            self.assertFalse(m.usage_report_accepted({"name": "c", "template": "claude", "config": {}}, {}))               # old bridge: not flagged
            m._settings.load_settings = lambda: {"LLM_KEY_PROXY": ""}
            self.assertTrue(m.usage_report_accepted({"name": "o", "config": {}}, {}))
        finally:
            m._settings.load_settings = old

    def test_hindsight_retains_user_turn_only(self):
        """A-1: the passive chat capture keeps the USER turn, never the agent's
        own reply (no memory poisoning), and HINDSIGHT_RETAIN=0 turns retention
        off for that instance; explicit notes obey the same switch."""
        m = self.m
        seen = []
        old = (m.load_instances, m.instance_by_ip, m.save_chats, m.load_chats, m._memfs.timeline_add,
               m._hindsight.retain_async)
        try:
            m._hindsight.retain_async = lambda inst, text, tags=(): seen.append((inst, text, tuple(tags)))
            m._memfs.timeline_add = lambda *a, **k: None
            m.load_chats = lambda: []; m.save_chats = lambda c: 1
            m.load_instances = lambda: [{"name": "a", "config": {}}, {"name": "voice", "config": {"HINDSIGHT_RETAIN": "0"}}]
            self.assertTrue(m.hindsight_retains("a")); self.assertFalse(m.hindsight_retains("voice"))
            m.chat_log_append("a", "", "mach das Radio an", "Das Radio ist an.", kind="voice")
            self.assertEqual(len(seen), 1)
            inst, text, tags = seen[0]
            self.assertEqual(inst, "a"); self.assertEqual(text, "mach das Radio an")   # user only
            self.assertNotIn("Das Radio ist an", text)                                  # NOT the agent reply
            self.assertIn("user", tags)
            seen.clear()
            m.chat_log_append("voice", "", "hallo", "hi", kind="voice")                 # retention off
            self.assertEqual(seen, [])
        finally:
            (m.load_instances, m.instance_by_ip, m.save_chats, m.load_chats, m._memfs.timeline_add,
             m._hindsight.retain_async) = old

    def test_hindsight_second_memory(self):
        """Off without HINDSIGHT_URL (every call a no-op); on: retain sends the
        text to the instance's bank, recall hits merge into the memory search
        behind the semantic ones, reflect answers, and a dead server never
        raises."""
        import mgr.hindsight as hs
        m = self.m
        calls = []
        old = hs._settings, hs._call, m.sem_search, m._settings.load_settings
        try:
            hs.configure(lambda: {}, log=lambda *a, **k: None)
            self.assertFalse(hs.enabled()); self.assertFalse(hs.retain("vm1", "x")); self.assertEqual(hs.recall("vm1", "q"), [])
            self.assertIn("off", hs.reflect("vm1", "q"))
            hs.configure(lambda: {"HINDSIGHT_URL": "http://127.0.0.1:1/"}, log=lambda *a, **k: None)
            def fake(method, path, body=None, timeout=20):
                calls.append((method, path, body))
                if path.endswith("/recall"):
                    return {"results": [{"text": "Ulrich bikes to work", "score": 0.9}, {"content": "likes radio"}, {"text": ""}]}
                if path.endswith("/reflect"):
                    return {"text": "He commutes by bike."}
                return {"success": True}
            hs._call = fake
            self.assertTrue(hs.retain("vm 1", "User: hi\n\nAssistant: hello", ("chat",)))
            self.assertEqual(calls[-1][1], "/v1/default/banks/vm-1/memories")
            self.assertEqual(calls[-1][2]["items"][0]["tags"], ["chat"]); self.assertTrue(calls[-1][2]["async"])
            hits = hs.recall("vm1", "commute")
            self.assertEqual([h["text"] for h in hits], ["Ulrich bikes to work", "likes radio"])
            self.assertEqual(hits[0]["source"], "hindsight")
            self.assertEqual(hs.reflect("vm1", "how does he commute?"), "He commutes by bike.")
            self.assertEqual(hs.bank("a/b c"), "a-b-c")
            # merged into the manager's memory search, semantic first, no duplicates
            m._settings.load_settings = lambda: {"HINDSIGHT_URL": "http://127.0.0.1:1/"}
            m._hindsight.configure(m._settings.load_settings, log=lambda *a, **k: None); m._hindsight._call = fake
            m.sem_search = lambda inst, q, k=5: [{"score": 0.5, "text": "likes radio"}]
            h = self._post_handler("/api/memory-search", "10.0.0.9", json.dumps({"instance": "vm1", "query": "commute"}).encode())
            m.instance_by_ip = lambda ip: None
            h._do_POST()
            d = json.loads(h.wfile.getvalue().split(b"\r\n\r\n", 1)[1])
            self.assertEqual([x["text"] for x in d["hits"]], ["likes radio", "Ulrich bikes to work"])
            def dead(method, path, body=None, timeout=20):
                raise OSError("connection refused")
            hs._call = dead
            self.assertFalse(hs.retain("vm1", "x", wait=True)); self.assertEqual(hs.recall("vm1", "q"), [])
            self.assertIn("error", hs.reflect("vm1", "q")); self.assertFalse(hs.health()[0])
        finally:
            hs._settings, hs._call, m.sem_search, m._settings.load_settings = old

    def test_internet_off_is_an_explicit_reject(self):
        """internet=False must not depend on the FORWARD policy: the tap gets
        its own chain that rejects everything outside the pool, jumped to
        first. With internet on, the chain ends in the usual ACCEPT."""
        m = self.m
        calls = []
        class R:
            def __init__(self, rc): self.returncode = rc
        def fake_sh(*a, **k):
            calls.append(a)
            return R(1 if "-C" in a else 0)
        old = m.sh, m.ensure_antispoof
        try:
            m.sh = fake_sh; m.ensure_antispoof = lambda inst: None
            inst = {"name": "vm1", "index": 3, "config": {}}
            m.apply_internet(inst, False)
            chain = m._fc_chain(inst)
            self.assertIn(("iptables", "-A", chain, "!", "-d", m.POOL, "-j", "REJECT"), calls)
            self.assertIn(("iptables", "-I", "FORWARD", "1", "-i", m.net_of(inst)["tap"], "-j", chain), calls)
            self.assertFalse(any("ACCEPT" in a and chain in a for a in calls))
            calls.clear()
            m.apply_internet(inst, True)
            self.assertIn(("iptables", "-A", chain, "!", "-d", m.POOL, "-j", "ACCEPT"), calls)
            self.assertFalse(any(a[-1] == "REJECT" and "!" in a for a in calls))
        finally:
            m.sh, m.ensure_antispoof = old

    def test_sandbox_through_the_task_route_and_the_queue(self):
        """A guest's spawn_subagent/create_task with a sandbox: the route turns
        it into {cfg, internet} and hands it to the run (wait) or stores it on
        the queued task; a sandbox on a named target or one that widens the
        caller's rights is refused; without a sandbox the old call shape."""
        m = self.m
        import mgr.store as st
        seen = []
        old = (m.instance_by_ip, m.load_instances, m._run_task_now, m.history_add, m.guest_may_target,
               m._run_ephemeral, st.TASKS_FILE)
        try:
            caller = {"name": "orch", "config": {}}
            m.instance_by_ip = lambda ip: caller if ip == "172.30.1.2" else None
            m.load_instances = lambda: [caller, {"name": "hass"}]
            m.guest_may_target = lambda inst, target: True
            m.history_add = lambda *a, **k: None
            m._run_task_now = lambda target, msg, model=None, timeout=600, sandbox=None: (seen.append((target, msg, sandbox)), (True, "ok"))[1]
            def post(body):
                h = self._post_handler("/api/task", "172.30.1.2", json.dumps(body).encode()); h._do_POST()
                return json.loads(h.wfile.getvalue().split(b"\r\n\r\n", 1)[1])
            d = post({"message": "x", "target": "ephemeral", "wait": True, "sandbox": {"tools": "bash", "egress": "none"}})
            self.assertEqual(d.get("result"), "ok")
            self.assertEqual(seen[-1], ("ephemeral", "x", {"cfg": {"AGENT_TOOLS": "bash"}, "internet": False}))
            d = post({"message": "x", "target": "ephemeral", "wait": True})
            self.assertEqual(seen[-1][2], None)                                                  # no cage: as before
            self.assertIn("ephemeral targets only", post({"message": "x", "target": "hass", "wait": True, "sandbox": {"tools": "bash"}})["error"])
            caller["config"] = {"AGENT_TOOLS": "read_file"}
            self.assertIn("does not hold", post({"message": "x", "target": "ephemeral", "wait": True, "sandbox": {"tools": "bash"}})["error"])
            caller["config"] = {}
            st.TASKS_FILE = os.path.join(tempfile.mkdtemp(prefix="e2e-sbq-"), "tasks.json")
            d = post({"message": "later", "target": "ephemeral", "sandbox": {"skill": "nope"}})
            self.assertIn("unknown", d["error"])                                                 # refused at creation, not at 08:00
            d = post({"message": "later", "target": "ephemeral", "sandbox": {"egress": "api.example.com"}})
            t = next(x for x in st.load_tasks() if x["id"] == d["id"])
            self.assertEqual(t["sandbox"], {"cfg": {"EGRESS_ALLOW": "api.example.com"}, "internet": True})
            # the runner keeps the old call shape without a sandbox (tests and callers monkeypatch it)
            calls = []
            m._run_ephemeral = lambda *a, **k: (calls.append((a, k)), (True, "r"))[1]
            m._run_task_now = old[2]
            m._run_task_now("ephemeral", "do", None, sandbox={"cfg": {}, "internet": False})
            m._run_task_now("ephemeral", "do")
            self.assertEqual(len(calls[0][0]), 4); self.assertEqual(len(calls[1][0]), 3)
        finally:
            (m.instance_by_ip, m.load_instances, m._run_task_now, m.history_add, m.guest_may_target,
             m._run_ephemeral, st.TASKS_FILE) = old

    def test_egress_allowlist_rules(self):
        """EGRESS_ALLOW: the chain accepts the resolved addresses and rejects
        everything else outside the pool; a host that does not resolve is
        skipped without opening the net."""
        m = self.m
        import socket as _socket
        calls = []
        class R:
            def __init__(self, rc): self.returncode = rc
        old = m.sh, m.ensure_antispoof, m.socket.getaddrinfo, m._mcp_endpoints, m._llama_endpoint
        try:
            m.sh = lambda *a, **k: (calls.append(a), R(1 if "-C" in a else 0))[1]
            m.ensure_antispoof = lambda inst: None
            m._mcp_endpoints = lambda inst: []; m._llama_endpoint = lambda inst: None
            def gai(host, *a, **k):
                if host == "api.example.com":
                    return [(2, 1, 6, "", ("93.184.216.34", 0))]
                raise OSError("no such host")
            m.socket.getaddrinfo = gai
            inst = {"name": "vm1", "index": 3, "config": {"EGRESS_ALLOW": "api.example.com, typo.invalid"}}
            m.apply_internet(inst, True)
            chain = m._fc_chain(inst)
            self.assertIn(("iptables", "-A", chain, "-d", "93.184.216.34", "-j", "ACCEPT"), calls)
            self.assertIn(("iptables", "-A", chain, "!", "-d", m.POOL, "-j", "REJECT"), calls)
            self.assertNotIn(("iptables", "-A", chain, "!", "-d", m.POOL, "-j", "ACCEPT"), calls)
            self.assertFalse(any("typo.invalid" in a for a in calls))
        finally:
            m.sh, m.ensure_antispoof, m.socket.getaddrinfo, m._mcp_endpoints, m._llama_endpoint = old

    def test_notification_text_lands_in_the_task_chat(self):
        """A guest's notification is also appended to the instance's task chat
        — the place a click on the notification opens; an admin notification
        touches no chat."""
        m = self.m
        seen = []
        old = m.instance_by_ip, m.notify_add, m.chat_log_append, m.audit_append
        try:
            m.instance_by_ip = lambda ip: {"name": "orch"} if ip == "172.30.1.2" else None
            m.notify_add = lambda inst, title, body, link="": ("id1", "")
            m.chat_log_append = lambda inst, sender, u, r, kind="signal": seen.append((inst, u, r, kind)) or 1
            m.audit_append = lambda *a, **k: None
            h = self._post_handler("/api/notify", "172.30.1.2", json.dumps({"title": "Saddler weekly", "message": "Failures 67 (was 20)"}).encode())
            h._do_POST()
            self.assertEqual(seen, [("orch", "", "🔔 Saddler weekly\n\nFailures 67 (was 20)", "task")])
            seen.clear()
            m.notify_add = lambda inst, title, body, link="": ("id2", "")
            fake_key = "sk-" + "or-" + "v1-" + "de" * 32       # assembled at runtime so no literal key sits in the repo (GitHub push protection)
            h = self._post_handler("/api/notify", "172.30.1.2", json.dumps({"title": "t", "message": "key " + fake_key}).encode())
            h._do_POST()
            self.assertNotIn(fake_key, seen[0][2])                               # secret redacted in the chat copy too
            self.assertIn("[SECRET", seen[0][2])
            h = self._post_handler("/api/notify", "10.0.0.9", json.dumps({"title": "t", "message": "m"}).encode())
            h._do_POST()
            self.assertEqual(len(seen), 1)
        finally:
            m.instance_by_ip, m.notify_add, m.chat_log_append, m.audit_append = old

    def test_tool_allowlist_enforced_at_the_host(self):
        """A-2: a restricted instance is refused a capability it did not list;
        an instance with no allowlist keeps everything; admin is never gated."""
        m = self.m
        self.assertTrue(m.tool_allowed({"config": {}}, "send_signal"))
        self.assertTrue(m.tool_allowed({"config": {"AGENT_TOOLS": "bash,send_signal"}}, "send_signal"))
        self.assertFalse(m.tool_allowed({"config": {"AGENT_TOOLS": "bash,read_file"}}, "send_signal"))
        self.assertFalse(m.tool_allowed({"config": {"AGENT_TOOLS": "bash"}}, "ha_control"))
        seen = []
        old = m.instance_by_ip, m.notify_add, m.audit_append, m.chat_log_append
        try:
            m.notify_add = lambda *a, **k: seen.append(a) or ("id", "")
            m.audit_append = lambda *a, **k: None; m.chat_log_append = lambda *a, **k: 1
            m.instance_by_ip = lambda ip: {"name": "r", "config": {"AGENT_TOOLS": "bash"}} if ip == "172.30.1.2" else None
            h = self._post_handler("/api/notify", "172.30.1.2", json.dumps({"title": "t", "message": "m"}).encode())
            h._do_POST()
            self.assertEqual(self._status(h), 403); self.assertEqual(seen, [])
            m.instance_by_ip = lambda ip: {"name": "o", "config": {}}
            h = self._post_handler("/api/notify", "172.30.1.2", json.dumps({"title": "t", "message": "m"}).encode())
            h._do_POST()
            self.assertEqual(self._status(h), 200); self.assertEqual(len(seen), 1)
        finally:
            m.instance_by_ip, m.notify_add, m.audit_append, m.chat_log_append = old

    def test_persona_carries_tools_and_model(self):
        """Feature 3: a persona keeps an optional recommended tool subset and
        model across save; unknown tools are dropped; empty stays absent."""
        m = self.m
        import mgr.store as st
        old = m.load_personas, m.save_personas
        saved = []
        try:
            m.load_personas = lambda: []
            m.save_personas = lambda items: saved.append(items)
            m.upsert_persona("rev", "You review.", tools="read_file, bash, nope_tool", model=" google/gemini-2.5-flash ")
            ent = saved[-1][0]
            self.assertEqual(ent["name"], "rev")
            self.assertEqual(set(ent["tools"]), {"read_file", "bash"})     # unknown dropped
            self.assertEqual(ent["model"], "google/gemini-2.5-flash")
            saved.clear()
            m.upsert_persona("plain", "hi")
            self.assertNotIn("tools", saved[-1][0]); self.assertNotIn("model", saved[-1][0])
            # regression: a web save (name+prompt only, tools/model = None) must
            # NOT wipe an existing persona's tools/model.
            saved.clear()
            m.load_personas = lambda: [{"name": "rev", "prompt": "old", "tools": ["bash"], "model": "m/x"}]
            m.upsert_persona("rev", "new prompt")                    # tools=None, model=None
            ent = saved[-1][0]
            self.assertEqual(ent["prompt"], "new prompt")
            self.assertEqual(ent["tools"], ["bash"]); self.assertEqual(ent["model"], "m/x")   # kept
            saved.clear()
            m.upsert_persona("rev", "p", tools=[])                   # explicit empty -> clear
            self.assertNotIn("tools", saved[-1][0])
        finally:
            m.load_personas, m.save_personas = old

    def test_defense_baseline_prepended(self):
        """Feature 2: every instance's system prompt carries the prompt-defense
        baseline (untrusted-content rule) by default."""
        ag = _load("agent_defense", AGENT_PATH, {"OPENROUTER_API_KEY": "dummy"})
        self.assertIn("untrusted", ag.SYSTEM.lower())
        self.assertIn("outrank", ag.SYSTEM.lower())

    def test_sandbox_config_persona(self):
        """Feature 4: spawn_subagent(persona=) bakes the persona prompt, uses its
        recommended tools when none are requested, sets its model, and refuses an
        unknown persona."""
        m = self.m
        old = m.load_personas, m.load_skills
        try:
            m.load_personas = lambda: [{"name": "assistant", "prompt": "You are helpful."},
                                       {"name": "code-reviewer", "prompt": "You review code.",
                                        "tools": ["bash", "read_file"], "model": "google/gemini-2.5-flash"}]
            m.load_skills = lambda: []
            caller = {"name": "orch", "config": {}}
            cfg, net, err = m.sandbox_config(caller, {"persona": "code-reviewer"})
            self.assertEqual(err, "")
            self.assertEqual(cfg["AGENT_TOOLS"], "bash,read_file")          # persona's tools
            self.assertEqual(cfg["AGENT_SYSTEM"], "You review code.")       # persona's prompt
            self.assertEqual(cfg["OPENROUTER_MODEL"], "google/gemini-2.5-flash")
            self.assertIn("unknown", m.sandbox_config(caller, {"persona": "ghost"})[2])
            # an explicit tools list still overrides the persona's, and must stay a subset
            cfg2, _, err2 = m.sandbox_config(caller, {"persona": "code-reviewer", "tools": "read_file"})
            self.assertEqual((cfg2["AGENT_TOOLS"], err2), ("read_file", ""))
            # an explicit model must win over the persona's model in the VM config
            seen = []
            old2 = m.create_instance, m.load_instances, m.wait_web, m._chat_post, m.stop, m.delete_instance
            try:
                def create(name, tpl, cfg=None, mounts=None, internet=True):
                    seen.append(dict(cfg or {}))
                    m.load_instances = lambda: [{"name": name, "template": "openrouter"}]
                    return "ok"
                m.create_instance = create; m.wait_web = lambda i, timeout=120: True
                m._chat_post = lambda i, msg, timeout=600: "r"; m.stop = lambda i: None; m.delete_instance = lambda n: None
                sb = {"cfg": {"OPENROUTER_MODEL": "persona/model", "AGENT_SYSTEM": "You review."}, "internet": True}
                m._run_ephemeral_vm("do", "explicit/model", 60, sb)
                self.assertEqual(seen[-1]["OPENROUTER_MODEL"], "explicit/model")
            finally:
                m.create_instance, m.load_instances, m.wait_web, m._chat_post, m.stop, m.delete_instance = old2
        finally:
            m.load_personas, m.load_skills = old

    def test_sandbox_config_only_narrows(self):
        """A sandboxed sub-agent runs in an ephemeral VM with a NARROWER policy:
        a subset of the caller's tools (never the spawning/secret ones), an
        egress allowlist inside the caller's own, or no network, and one
        skill baked into the system prompt with the file/web tools only."""
        m = self.m
        old = m.load_skills, m.load_personas
        try:
            m.load_skills = lambda: [{"name": "pdf-digest", "description": "d", "content": "Read the PDF, summarise."}]
            m.load_personas = lambda: [{"name": "assistant", "prompt": "You are helpful."}]
            caller = {"name": "orch", "config": {}}
            self.assertEqual(m.sandbox_config(caller, None), ({}, True, ""))
            cfg, net, err = m.sandbox_config(caller, {"tools": "bash, read_file,spawn_subagent"})
            self.assertEqual((cfg["AGENT_TOOLS"], net, err), ("bash,read_file", True, ""))     # spawn never inherited
            self.assertIn("unknown tools", m.sandbox_config(caller, {"tools": ["bash", "nope"]})[2])
            narrow = {"name": "n", "config": {"AGENT_TOOLS": "bash,read_file", "EGRESS_ALLOW": "api.example.com,cdn.example.com"}}
            self.assertIn("does not hold", m.sandbox_config(narrow, {"tools": ["bash", "web_search"]})[2])
            self.assertNotIn("notify", m.sandbox_config(caller, {"tools": "bash,notify"})[0]["AGENT_TOOLS"])   # notify never inherited
            self.assertIn("outside the caller", m.sandbox_config(narrow, {"egress": "api.example.com,evil.example.org"})[2])
            cfg, net, err = m.sandbox_config(narrow, {"egress": ["API.example.com"]})
            self.assertEqual((cfg["EGRESS_ALLOW"], net, err), ("api.example.com", True, ""))
            self.assertEqual(m.sandbox_config(caller, {"egress": "none"})[1], False)
            self.assertIn("list hosts", m.sandbox_config(caller, {"egress": [" "]})[2])
            cfg, net, err = m.sandbox_config(caller, {"skill": "pdf-digest"})
            self.assertEqual(cfg["AGENT_TOOLS"], ",".join(sorted(m.SANDBOX_DEFAULT_TOOLS)))
            self.assertIn("[Skill: pdf-digest]", cfg["AGENT_SYSTEM"]); self.assertTrue(cfg["AGENT_SYSTEM"].startswith("You are helpful."))
            self.assertIn("unknown", m.sandbox_config(caller, {"skill": "ghost"})[2])
            self.assertIn("object", m.sandbox_config(caller, "bash")[2])
        finally:
            m.load_skills, m.load_personas = old

    def test_sandbox_reaches_the_ephemeral_vm(self):
        """The sandbox travels from the task route into the VM's creation:
        the narrowed tools land in its config and 'egress none' creates it
        without internet; without a sandbox nothing changes."""
        m = self.m
        seen = []
        old = m.create_instance, m.load_instances, m.wait_web, m._chat_post, m.stop, m.delete_instance
        try:
            m.create_instance = lambda name, tpl, cfg=None, mounts=None, internet=True: seen.append((tpl, dict(cfg or {}), internet)) or "ok"
            m.load_instances = lambda: [{"name": n, "template": "openrouter"} for n in ["x"]] if False else [{"name": seen[-1] and "task-x", "template": "openrouter"}]
            m.load_instances = lambda: [{"name": next((k for k in ["any"]), ""), "template": "openrouter"}]
            m.wait_web = lambda inst, timeout=120: True
            m._chat_post = lambda inst, message, timeout=600: "child says hi"
            m.stop = lambda inst: None; m.delete_instance = lambda name: None
            # load_instances must return the instance the run just created: match on prefix
            m.load_instances = lambda: [{"name": "task-" + "".join(c for c in "0" * 6), "template": "openrouter"}]
            real_create = m.create_instance
            def create(name, tpl, cfg=None, mounts=None, internet=True):
                m.load_instances = lambda: [{"name": name, "template": "openrouter", "config": dict(cfg or {}), "internet": internet}]
                return real_create(name, tpl, cfg, mounts, internet)
            m.create_instance = create
            ok, res = m._run_ephemeral_vm("do", None, 60, {"cfg": {"AGENT_TOOLS": "bash", "EGRESS_ALLOW": ""}, "internet": False})
            self.assertEqual((ok, res), (True, "child says hi"))
            self.assertEqual(seen[-1][1]["AGENT_TOOLS"], "bash"); self.assertFalse(seen[-1][2]); self.assertEqual(seen[-1][1]["NO_SPAWN"], "1")
            ok, res = m._run_ephemeral_vm("do", "google/gemini-2.5-flash", 60)
            self.assertNotIn("AGENT_TOOLS", seen[-1][1]); self.assertTrue(seen[-1][2]); self.assertEqual(seen[-1][1]["OPENROUTER_MODEL"], "google/gemini-2.5-flash")
        finally:
            m.create_instance, m.load_instances, m.wait_web, m._chat_post, m.stop, m.delete_instance = old

    def test_proxy_books_upstream_usage(self):
        m = self.m
        seen = []
        old = m.usage_add
        try:
            m.usage_add = lambda *a, **kw: seen.append(a)
            inst = {"name": "vm1"}
            m._proxy_usage(inst, "openrouter", b'{"model":"x/y","usage":{"prompt_tokens":10,"completion_tokens":5,"cost":0.001}}')
            m._proxy_usage(inst, "openrouter", b'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":2}}\n')
            m._proxy_usage(inst, "openrouter", b'data: [DONE]\n')
            m._proxy_usage(None, "openrouter", b'{"usage":{"prompt_tokens":10}}')
            self.assertEqual(seen, [("vm1", "x/y", 10, 5, 0.001), ("vm1", "openrouter", 1, 2, None)])
        finally:
            m.usage_add = old

    def test_nfs_exports_are_per_instance_and_squashed_to_the_guest_user(self):
        """No pool-wide root export: the workspace agent/<name> and each host
        folder are exported to that VM's address only, by absolute path, with
        every write squashed to the guest user; the config disk tells the guest
        where its workspace is; the old root export is retired once."""
        import types
        m = self.m
        tmp = tempfile.mkdtemp(prefix="e2e-nfs-")
        calls = []
        old = (m.AGENT_ROOT, m.FCMNT_ROOT, m.EXPORTS_D, m.AGENT_EXPORTS, m.sh, m.mount_specs,
               m.GUEST_UID, m.GUEST_GID, m.ensure_guest_user)
        try:
            m.AGENT_ROOT = os.path.join(tmp, "agent"); m.FCMNT_ROOT = os.path.join(m.AGENT_ROOT, ".fcmnt")
            m.EXPORTS_D = os.path.join(tmp, "exports.d"); m.AGENT_EXPORTS = os.path.join(m.EXPORTS_D, "agent.exports")
            os.makedirs(m.EXPORTS_D)
            with open(m.AGENT_EXPORTS, "w") as fh:
                fh.write(f"{m.AGENT_ROOT} {m.POOL}(rw,fsid=0,crossmnt)\n")
            m.sh = lambda *a, check=True: (calls.append(a), types.SimpleNamespace(returncode=0, stdout="", stderr=""))[1]
            m.GUEST_UID, m.GUEST_GID = 4242, 4243
            m.ensure_guest_user = lambda: True
            share = os.path.join(tmp, "share"); os.makedirs(share)
            inst = {"name": "vm1", "index": 7, "template": "openrouter", "rootfs": "instances/openrouter-rootfs.ext4",
                    "config": {}, "mounts": [{"host": share, "guest": "/home/node/data", "readonly": True}]}
            m.mount_specs = lambda i: [{"idx": 0, "host": share, "guest": "/home/node/data", "ro": True,
                                        "target": os.path.join(m.FCMNT_ROOT, "vm1", "0"),
                                        "sub": os.path.join(m.FCMNT_ROOT, "vm1", "0"), "fsid": 4000 + 7 * 16}]
            m.setup_mounts(inst)
            ex = open(os.path.join(m.EXPORTS_D, "fc-vm1.exports")).read()
            ws = os.path.join(m.AGENT_ROOT, "vm1")
            self.assertIn(f"{ws} 172.30.7.2(rw,sync,no_subtree_check,all_squash,anonuid=4242,anongid=4243,fsid={4000 + 7 * 16 + 14})", ex)
            self.assertIn(f"{m.FCMNT_ROOT}/vm1/0 172.30.7.2(ro,", ex)
            self.assertNotIn(m.POOL, ex)                                   # nothing for the whole pool
            self.assertTrue(os.path.isdir(ws))
            self.assertFalse(os.path.exists(m.AGENT_EXPORTS))              # root export retired …
            self.assertTrue(os.path.exists(m.AGENT_EXPORTS + ".bak"))      # … with a backup
            self.assertFalse(m.retire_root_export())                       # idempotent
            self.assertEqual(m.guest_env(inst)["AGENT_EXPORT"], ws)
            self.assertEqual(m.guest_env(inst)["MEMORY_DIR"], "/memory")
            self.assertEqual(m.guest_env(inst)["GUEST_DNS"], m.GUEST_DNS)
            # real mount_specs: absolute host paths as NFS subpaths, fsid slots 0..13 for folders
            m.mount_specs = old[5]
            sp = m.mount_specs({**inst, "index": 3, "template": "claude", "rootfs": "instances/claude-rootfs.ext4"})
            self.assertEqual(sp[0]["sub"], os.path.join(m.FCMNT_ROOT, "vm1", "0"))
            self.assertEqual(sp[0]["fsid"], 4000 + 3 * 16)
            self.assertEqual(m.workspace_fsid({"index": 3}), 4000 + 3 * 16 + 14)
            self.assertTrue(any(c[:2] == ("exportfs", "-ra") for c in calls))
        finally:
            (m.AGENT_ROOT, m.FCMNT_ROOT, m.EXPORTS_D, m.AGENT_EXPORTS, m.sh, m.mount_specs,
             m.GUEST_UID, m.GUEST_GID, m.ensure_guest_user) = old

    def test_guest_user_and_writability_hint(self):
        """Without root the squash user is not created (exports fall back to
        uid 1000); a rw share the guest user cannot write is flagged."""
        m = self.m
        tmp = tempfile.mkdtemp(prefix="e2e-gw-")
        old = m.GUEST_UID, m.GUEST_GID, m.GUEST_USER
        try:
            if os.geteuid() != 0:
                m.GUEST_USER = "kaim56-e2e-nonexistent"
                self.assertFalse(m.ensure_guest_user())
            m.GUEST_UID, m.GUEST_GID = 4242, 4243
            os.chmod(tmp, 0o755)
            self.assertFalse(m.guest_can_write(tmp))          # owned by us, not the guest user
            os.chmod(tmp, 0o777)
            self.assertTrue(m.guest_can_write(tmp))
            m.GUEST_UID = os.getuid(); os.chmod(tmp, 0o700)
            self.assertTrue(m.guest_can_write(tmp))           # owner = guest user
        finally:
            m.GUEST_UID, m.GUEST_GID, m.GUEST_USER = old

    def test_login_lockout(self):
        """Ten wrong passwords in a row lock the client for a while — the right
        password included; a success clears the counter. Behind the local proxy
        the client is the first X-Forwarded-For hop, not the proxy."""
        m = self.m
        old = m.instance_by_ip, m.PW, m.USER
        try:
            m.instance_by_ip = lambda ip: None
            m.PW, m.USER = "s3cret", "admin"
            m._auth_fails.clear()
            good = "Basic " + base64.b64encode(b"admin:s3cret").decode()
            bad = "Basic " + base64.b64encode(b"admin:nope").decode()
            for i in range(m.AUTH_FAILS_MAX):
                h = self._handler("/api/agents", "10.0.0.9", auth=bad)
                self.assertFalse(h._auth()); self.assertEqual(self._status(h), 401)
            h = self._handler("/api/agents", "10.0.0.9", auth=good)
            self.assertFalse(h._auth()); self.assertEqual(self._status(h), 429)     # locked
            h = self._handler("/api/agents", "10.0.0.10", auth=good)
            self.assertTrue(h._auth())                                                # another client
            m._auth_fails.clear()
            h = self._handler("/api/agents", "10.0.0.9", auth=good)
            self.assertTrue(h._auth())
            self.assertEqual(m.auth_client_key("127.0.0.1", "203.0.113.5, 10.0.0.1"), "203.0.113.5")
            self.assertEqual(m.auth_client_key("172.17.0.3", "203.0.113.5"), "203.0.113.5")
            self.assertEqual(m.auth_client_key("192.168.1.20", "203.0.113.5"), "192.168.1.20")   # LAN client: XFF ignored
        finally:
            m.instance_by_ip, m.PW, m.USER = old
            m._auth_fails.clear()

    def test_instance_json_is_operator_readable(self):
        """save_instance leaves the JSON at 0640 whatever the umask; the tests
        and the operator read it, and harden_files relaxes old ones."""
        m = self.m
        tmp = tempfile.mkdtemp(prefix="e2e-instjson-")
        old = m._paths.INST_DIR, os.umask(0o077)
        try:
            m._paths.INST_DIR = tmp
            m.save_instance({"name": "x", "template": "openrouter"})
            self.assertEqual(os.stat(os.path.join(tmp, "x.json")).st_mode & 0o777, 0o640)
            self.assertEqual(m.load_instances()[0]["name"], "x")
            os.chmod(os.path.join(tmp, "x.json"), 0o600)
            os.makedirs(os.path.join(tmp, "instances")); os.rename(os.path.join(tmp, "x.json"), os.path.join(tmp, "instances", "x.json"))
            m.harden_files(tmp)
            self.assertEqual(os.stat(os.path.join(tmp, "instances", "x.json")).st_mode & 0o777, 0o640)
        finally:
            m._paths.INST_DIR = old[0]; os.umask(old[1])

    def test_harden_files_makes_state_private(self):
        m = self.m
        tmp = tempfile.mkdtemp(prefix="e2e-harden-")
        os.makedirs(os.path.join(tmp, "audit")); os.makedirs(os.path.join(tmp, "templates"))
        for f in ("chats.json", "missions.json", "history.db", "audit/hass.jsonl", "templates/x.json", "run.log"):
            with open(os.path.join(tmp, f), "w") as fh:
                fh.write("{}")
            os.chmod(os.path.join(tmp, f), 0o644)
        n = m.harden_files(tmp)
        self.assertEqual(n, 4)
        for f in ("chats.json", "missions.json", "history.db", "audit/hass.jsonl"):
            self.assertEqual(os.stat(os.path.join(tmp, f)).st_mode & 0o777, 0o600, f)
        self.assertEqual(os.stat(os.path.join(tmp, "audit")).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(os.path.join(tmp, "templates", "x.json")).st_mode & 0o777, 0o644)   # untouched
        self.assertEqual(os.stat(os.path.join(tmp, "run.log")).st_mode & 0o777, 0o644)

    def test_config_disk_content_is_readable_by_the_agent(self):
        """The manager runs with umask 077; the config disk is read by uid
        1000 in the VM, so its tree must be made world-readable explicitly
        (a 0700 plugins folder killed the agent at start once)."""
        m = self.m
        if not shutil.which("mkfs.ext4", path="/usr/sbin:/sbin:" + os.environ.get("PATH", "")):
            self.skipTest("mkfs.ext4 not available")
        tmp = tempfile.mkdtemp(prefix="e2e-cfgdisk-")
        old = m._paths.RUN_DIR, None, os.umask(0o077)
        try:
            m._paths.RUN_DIR = tmp
            inst = {"name": "vm1", "index": 5, "template": "openrouter", "rootfs": "instances/openrouter-rootfs.ext4", "config": {}}
            m.make_config_disk(inst)
            d = os.path.join(tmp, "vm1.cfgdir")
            self.assertEqual(os.stat(d).st_mode & 0o777, 0o755)
            self.assertEqual(os.stat(os.path.join(d, "config.env")).st_mode & 0o777, 0o644)
            for root, dirs, files in os.walk(d):
                for x in dirs:
                    self.assertEqual(os.stat(os.path.join(root, x)).st_mode & 0o777, 0o755, x)
        finally:
            m._paths.RUN_DIR = old[0]
            os.umask(old[2])

    def test_notify_route_rejects_empty(self):
        """Empty notification -> 429, id null. Offline on purpose: as an HTTP
        test it ran against the live manager on every suite run and left one
        'admin/notify empty' line in the live audit each time (138 of them
        looked like a misbehaving admin instance)."""
        m = self.m
        old = m.instance_by_ip, m.audit_append, m.PW
        try:
            m.instance_by_ip = lambda ip: None
            m.audit_append = lambda *a, **k: None
            m.PW = ""
            h = self._post_handler("/api/notify", "10.0.0.5", b'{"title": "", "message": ""}')
            h.do_POST()
            self.assertEqual(self._status(h), 429)
            self.assertIsNone(json.loads(h.wfile.getvalue().split(b"\r\n\r\n", 1)[1]).get("id"))
        finally:
            m.instance_by_ip, m.audit_append, m.PW = old

    def test_task_runs_carry_a_deadline_and_the_worker_timeout(self):
        """The bridge call carries deadline = now + timeout - margin; the worker
        gives a task TASK_TIMEOUT, a waiting guest keeps 600 s; a timeout reads
        as a timeout, not as a stack trace."""
        m = self.m
        sent = []
        old = m.urllib.request.urlopen, m.net_of, m.load_instances, m.is_running, m._run_named

        class _R:
            def __init__(self, body): self.body = body
            def read(self): return self.body
        try:
            m.net_of = lambda inst: {"guest": "172.30.9.2"}
            def fake_open(req, timeout=None):
                sent.append((json.loads(req.data.decode()), timeout))
                if timeout == 5:
                    raise TimeoutError("timed out")
                return _R(b'{"reply": "done"}')
            m.urllib.request.urlopen = fake_open
            inst = {"name": "vm1", "index": 9}
            self.assertEqual(m._chat_post(inst, "hi", timeout=1800), "done")
            body, to = sent[-1]
            self.assertEqual(to, 1800)
            self.assertAlmostEqual(body["deadline"], time.time() + 1770, delta=5)
            m.load_instances = lambda: [inst]
            m.is_running = lambda i: True
            ok, res = m._run_named("vm1", "hi", timeout=5)
            self.assertFalse(ok); self.assertIn("no answer within 5 s", res)
            got = []
            m._run_named = lambda instance, message, timeout=600: (got.append(timeout), (True, "x"))[1]
            m._run_task_now("vm1", "hi", None, timeout=m.TASK_TIMEOUT)
            m._run_task_now("vm1", "hi")
            self.assertEqual(got, [m.TASK_TIMEOUT, 600])
            self.assertGreaterEqual(m.TASK_TIMEOUT, 1800)
        finally:
            m.urllib.request.urlopen, m.net_of, m.load_instances, m.is_running, m._run_named = old

    def test_trace_stitches_turn_llm_and_tool_spans(self):
        """One turn = one span tree. The agent sends a start marker, its LLM
        calls (usage with turn/step/ms), its tool calls (audit with turn/ms)
        and an end marker; GET /api/trace/<inst>?turn= returns them stitched
        and ordered, the audit output keeps its shape (plus ms), an old DB
        gains the span columns without a migration error."""
        import mgr.store as st
        import sqlite3
        m = self.m
        tmp = tempfile.mkdtemp(prefix="e2e-trace-")
        old = st.HISTORY_DB, m.AUDIT_DIR, m.instance_by_ip, m._settings.load_settings, list(st._migrated)
        try:
            st.HISTORY_DB = os.path.join(tmp, "history.db")
            m.AUDIT_DIR = os.path.join(tmp, "audit")
            with sqlite3.connect(st.HISTORY_DB) as c:        # a DB from before the span columns
                c.execute("CREATE TABLE llm_usage(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, "
                          "instance TEXT, model TEXT, prompt_tokens INTEGER, completion_tokens INTEGER, cost REAL)")
                c.execute("INSERT INTO llm_usage(ts,instance,model,prompt_tokens,completion_tokens,cost) "
                          "VALUES(1,'vm1','old',1,1,0)")
            st._migrated[0] = False
            inst = {"name": "vm1", "index": 3, "template": "openrouter", "config": {}}
            m.instance_by_ip = lambda ip: inst if ip == "172.30.3.2" else None
            m._settings.load_settings = lambda: {}                    # proxy off: the agent reports usage
            def post(path, body):
                h = self._post_handler(path, "172.30.3.2", json.dumps(body).encode()); h.do_POST()
                return self._status(h)
            self.assertEqual(post("/api/trace", {"turn": "t1", "event": "start", "kind": "task"}), 204)
            self.assertEqual(post("/api/usage", {"model": "x/y", "prompt_tokens": 100, "completion_tokens": 20,
                                                 "turn": "t1", "step": 1, "ms": 900}), 204)
            self.assertEqual(post("/api/audit", {"tool": "web_search", "target": "cronn", "ok": True,
                                                 "result": "1. cronn", "turn": "t1", "ms": 340}), 204)
            self.assertEqual(post("/api/audit", {"tool": "http_fetch", "target": "https://x", "ok": False,
                                                 "err": "HTTP 403", "turn": "t1", "ms": 1200}), 204)
            self.assertEqual(post("/api/usage", {"model": "x/y", "prompt_tokens": 0, "completion_tokens": 0,
                                                 "turn": "t1", "step": 2, "ms": 50, "ok": False, "err": "HTTP 429"}), 204)
            self.assertEqual(post("/api/trace", {"turn": "t1", "event": "end", "kind": "task",
                                                 "steps": 2, "ms": 2600, "outcome": "ok"}), 204)
            # an admin, not a guest, may not file markers; a guest gets no trace read
            h = self._post_handler("/api/trace", "10.0.0.5", b'{"turn":"t2","event":"start"}'); h.do_POST()
            self.assertEqual(self._status(h), 403)
            h = self._handler("/api/trace/vm1?turn=t1", "172.30.3.2"); h._do_GET()
            self.assertEqual(self._status(h), 403)
            h = self._handler("/api/trace/vm1?turn=t1", "10.0.0.5"); h._do_GET()
            tr = json.loads(h.wfile.getvalue().split(b"\r\n\r\n", 1)[1])
            self.assertEqual(tr["turn"]["kind"], "task")
            self.assertEqual((tr["turn"]["steps"], tr["turn"]["ms"], tr["turn"]["outcome"]), (2, 2600, "ok"))
            self.assertEqual((tr["turn"]["llm_calls"], tr["turn"]["llm_failed"], tr["turn"]["in"]), (2, 1, 100))
            self.assertEqual([x["step"] for x in tr["llm"]], [1, 2])
            self.assertEqual([x["ms"] for x in tr["llm"]], [900, 50])
            self.assertEqual([x["ok"] for x in tr["llm"]], [True, False])
            self.assertEqual([x["tool"] for x in tr["tools"]], ["web_search", "http_fetch"])
            self.assertEqual([x["ms"] for x in tr["tools"]], [340, 1200])
            self.assertTrue(all(x["ms"] > 0 for x in tr["llm"] + tr["tools"]))
            h = self._handler("/api/trace/vm1", "10.0.0.5"); h._do_GET()
            lst = json.loads(h.wfile.getvalue().split(b"\r\n\r\n", 1)[1])["turns"]
            self.assertEqual([t["turn"] for t in lst], ["t1"])
            # the audit reader is unchanged apart from ms
            recs = m.audit_read("vm1")
            self.assertEqual(set(recs[0]) - {"ms"}, {"ts", "tool", "target", "ok", "err", "turn"})
            # an end without a start still yields a full row; old rows have no turn
            st.turn_end("vm1", "t9", ms=10, steps=1, outcome="error")
            self.assertEqual(st.turns_read("vm1")[0]["turn"], "t9")
            self.assertEqual(st.usage_for("vm1")["calls"], 3)          # the old row still counts
            self.assertEqual(st.turns_prune(30), 0)
        finally:
            st.HISTORY_DB, m.AUDIT_DIR, m.instance_by_ip, m._settings.load_settings = old[:4]
            st._migrated[0] = old[4][0]

    def test_instance_proxy_forwards_the_turn_header(self):
        """The bridge names a turn in X-Kaim-Turn; the manager's /i/<name>/
        proxy passes it to the app on the streamed and on the JSON-unpacked
        chat path, so the app can fetch the trace."""
        m = self.m
        import email.message

        class _R:
            def __init__(self, body, ct, turn):
                self.status, self.body, self.pos = 200, body, 0
                self.headers = email.message.Message()
                self.headers["Content-Type"] = ct
                if turn:
                    self.headers["X-Kaim-Turn"] = turn
            def read(self, n=None):
                if n is None:
                    out, self.pos = self.body[self.pos:], len(self.body); return out
                out = self.body[self.pos:self.pos + n]; self.pos += len(out); return out
        old = m.urllib.request.urlopen, m.load_instances, m.is_running, m.net_of, m.instance_by_ip, m.PW
        try:
            inst = {"name": "vm1", "index": 4, "config": {"TRANSPORT": "web"}}
            m.load_instances = lambda: [inst]
            m.is_running = lambda i: True
            m.net_of = lambda i: {"guest": "172.30.4.2"}
            m.instance_by_ip = lambda ip: None
            m.PW = ""
            m.urllib.request.urlopen = lambda req, timeout=None: _R(b"Hallo", "text/plain; charset=utf-8", "t0ken001")
            h = self._post_handler("/i/vm1/api/chat/stream", "10.0.0.5", b'{"message":"hi"}')
            h._proxy("POST")
            raw = h.wfile.getvalue()
            self.assertIn(b"X-Kaim-Turn: t0ken001", raw.split(b"\r\n\r\n", 1)[0])
            self.assertTrue(raw.endswith(b"Hallo"))
            m.urllib.request.urlopen = lambda req, timeout=None: _R(b'{"reply": "Hi", "turn": "t0ken002"}', "application/json", "t0ken002")
            h = self._post_handler("/i/vm1/api/chat", "10.0.0.5", b'{"message":"hi"}')
            h._proxy("POST")
            raw = h.wfile.getvalue()
            self.assertIn(b"X-Kaim-Turn: t0ken002", raw.split(b"\r\n\r\n", 1)[0])
            self.assertTrue(raw.endswith(b"Hi"))                                  # unpacked for the app
            m.urllib.request.urlopen = lambda req, timeout=None: _R(b"<p>x</p>", "text/html", "")
            h = self._handler("/i/vm1/", "10.0.0.5"); h._proxy("GET")
            self.assertNotIn(b"X-Kaim-Turn", h.wfile.getvalue())                # nothing to forward
        finally:
            m.urllib.request.urlopen, m.load_instances, m.is_running, m.net_of, m.instance_by_ip, m.PW = old

    def test_task_run_now(self):
        """The Tasks tab's play button: a scheduled task runs at the next tick
        and keeps its schedule, a finished one-off is pending again, a running
        one is left alone, an unknown id says so."""
        import mgr.store as st
        m = self.m
        tmp = tempfile.mkdtemp(prefix="e2e-runnow-")
        old = st.TASKS_FILE, m.instance_by_ip, m.PW
        try:
            st.TASKS_FILE = os.path.join(tmp, "tasks.json")
            later = int(time.time()) + 86400
            with open(st.TASKS_FILE, "w") as fh:
                json.dump([{"id": "s1", "instance": "vm1", "message": "daily", "schedule": "daily 07:00",
                            "status": "scheduled", "next_run": later, "target_warned": 5},
                           {"id": "o1", "instance": "vm1", "message": "once", "schedule": "",
                            "status": "error", "result": "error: x"},
                           {"id": "r1", "instance": "vm1", "message": "busy", "schedule": "", "status": "running"}], fh)
            self.assertIn("queued", m.run_task_now("s1"))
            self.assertIn("queued", m.run_task_now("o1"))
            self.assertIn("running already", m.run_task_now("r1"))
            self.assertEqual(m.run_task_now("nope"), "unknown")
            ts = {t["id"]: t for t in st.load_tasks()}
            self.assertEqual(ts["s1"]["status"], "scheduled")
            self.assertLessEqual(ts["s1"]["next_run"], int(time.time()))
            self.assertEqual(ts["s1"]["schedule"], "daily 07:00")
            self.assertNotIn("target_warned", ts["s1"])
            self.assertEqual(ts["o1"]["status"], "pending")
            self.assertEqual(ts["r1"]["status"], "running")
            # the route
            m.instance_by_ip = lambda ip: None; m.PW = ""
            h = self._post_handler("/api/tasks/o1/run", "10.0.0.5", b"{}"); h.do_POST()
            self.assertIn(b"queued", h.wfile.getvalue())
        finally:
            st.TASKS_FILE, m.instance_by_ip, m.PW = old

    def test_skill_proposals_wait_for_approval(self):
        """An agent's skill proposal is linted, stored, announced, and enters
        the catalog only when approved in the Skills tab; a newer proposal
        with the same name replaces the pending one; admins cannot file."""
        m = self.m
        tmp = tempfile.mkdtemp(prefix="e2e-skprop-")
        old = m.SKILL_PROPOSALS_FILE, m.SKILLS_FILE, m.notify_add, m.instance_by_ip, m.PW
        notes = []
        try:
            m.SKILL_PROPOSALS_FILE = os.path.join(tmp, "p.json"); m.SKILLS_FILE = os.path.join(tmp, "s.json")
            m.notify_add = lambda *a, **k: notes.append(a)
            vm = {"name": "vm1", "index": 3, "template": "openrouter", "config": {}}
            m.instance_by_ip = lambda ip: vm if ip == "172.30.3.2" else None
            good = {"name": "Job-Search-NRW", "description": "Daily job search for a region",
                    "content": "# Purpose\nSearch job boards.\n\n## Steps\n1. web_search with the region\n2. http_fetch each hit\n3. notify a summary\n\n## Pitfalls\nIndeed blocks fetches.",
                    "turn": "t1"}
            h = self._post_handler("/api/skill-proposals", "172.30.3.2", json.dumps(good).encode()); h.do_POST()
            self.assertEqual(self._status(h), 200)
            self.assertEqual(len(notes), 1); self.assertIn("job-search-nrw", notes[0][1])
            bad = dict(good, content="too short"); h = self._post_handler("/api/skill-proposals", "172.30.3.2", json.dumps(bad).encode()); h.do_POST()
            self.assertEqual(self._status(h), 400)
            leak = dict(good, content=good["content"] + "\nuse Bearer abcdefghijklmnopqrstuvwxyz1234"); h = self._post_handler("/api/skill-proposals", "172.30.3.2", json.dumps(leak).encode()); h.do_POST()
            self.assertEqual(self._status(h), 400)
            again = dict(good, description="Daily job search, second try"); h = self._post_handler("/api/skill-proposals", "172.30.3.2", json.dumps(again).encode()); h.do_POST()
            self.assertEqual(self._status(h), 200)
            pending = [p for p in m.load_proposals() if p["status"] == "proposed"]
            self.assertEqual([p["description"] for p in pending], ["Daily job search, second try"])   # replaced, not doubled
            self.assertFalse(pending[0]["update"])
            h = self._post_handler("/api/skill-proposals", "10.0.0.5", json.dumps(good).encode()); h.do_POST()
            self.assertEqual(self._status(h), 403)                                     # guests only
            self.assertEqual([x["name"] for x in m.load_skills()], [])                 # nothing in the catalog yet
            m.PW = ""
            h = self._post_handler(f"/api/skill-proposals/{pending[0]['id']}/approve", "10.0.0.5", b"{}"); h.do_POST()
            self.assertIn(b"saved", h.wfile.getvalue())
            self.assertEqual([x["name"] for x in m.load_skills()], ["job-search-nrw"])
            self.assertEqual(m.load_proposals()[0]["status"], "approved")
            self.assertEqual(m.proposal_decide("nope", True), "unknown")
            # a proposal for an existing skill is flagged as an update
            pid, why = m.proposal_add("vm1", "job-search-nrw", "Better", good["content"])
            self.assertTrue(pid); self.assertTrue(next(p for p in m.load_proposals() if p["id"] == pid)["update"])
            self.assertIn("discarded", m.proposal_decide(pid, False))
        finally:
            m.SKILL_PROPOSALS_FILE, m.SKILLS_FILE, m.notify_add, m.instance_by_ip, m.PW = old

    def test_sessions_fulltext_search_scoped_per_guest(self):
        """FTS5 over chats and task runs: exact words find the session, guests
        see their own instance only, the orchestrator everything, the index
        follows chats.json by mtime."""
        import mgr.store as st
        m = self.m
        tmp = tempfile.mkdtemp(prefix="e2e-fts-")
        old = st.HISTORY_DB, m.load_chats, m.CHATS_FILE, m.instance_by_ip, dict(st._fts_state)
        try:
            st.HISTORY_DB = os.path.join(tmp, "history.db"); st._fts_state["mtime"] = None
            m.CHATS_FILE = os.path.join(tmp, "chats.json"); open(m.CHATS_FILE, "w").write("[]")
            chats = [{"id": "c1", "instance": "vm1", "title": "Jobs", "updatedAt": 1788000000000,
                      "messages": [{"user": True, "text": "Was war mit Vaillant in Remscheid?"},
                                   {"user": False, "text": "Vaillant sucht einen Senior Project Manager Security."}]},
                     {"id": "c2", "instance": "vm2", "title": "Garten", "updatedAt": 1788000000000,
                      "messages": [{"user": True, "text": "Gartenhaus Licht an"}]}]
            m.load_chats = lambda: chats
            st.history_add("vm2", "MSFT Kurs holen", "MSFT 512.30 — alert sent", True)
            hits = m.sessions_search("Vaillant")
            self.assertEqual({(h["instance"], h["kind"]) for h in hits}, {("vm1", "chat")})
            self.assertIn("[Vaillant]", hits[0]["snippet"])
            self.assertEqual([h["kind"] for h in m.sessions_search("MSFT alert")], ["task"])
            self.assertEqual(m.sessions_search("Vaillant", instance="vm2"), [])
            self.assertEqual(m.sessions_search('"; DROP TABLE x; --'), [])           # syntax cannot break it
            # guest scoping through the route
            vm1 = {"name": "vm1", "index": 3, "config": {}}; orch = {"name": m.ORCH_INSTANCE, "index": 1, "config": {}}
            m.instance_by_ip = lambda ip: {"172.30.3.2": vm1, "172.30.1.2": orch}.get(ip)
            h = self._post_handler("/api/sessions-search", "172.30.3.2", b'{"q": "Gartenhaus"}'); h.do_POST()
            self.assertEqual(json.loads(h.wfile.getvalue().split(b"\r\n\r\n", 1)[1])["hits"], [])     # vm2's chat
            h = self._post_handler("/api/sessions-search", "172.30.1.2", b'{"q": "Gartenhaus"}'); h.do_POST()
            self.assertEqual(len(json.loads(h.wfile.getvalue().split(b"\r\n\r\n", 1)[1])["hits"]), 1)
            # a changed chats.json is picked up
            chats[1]["messages"].append({"user": False, "text": "Licht im Gartenhaus ist an."})
            os.utime(m.CHATS_FILE, (1, 1))
            self.assertEqual(len(m.sessions_search("Gartenhaus")), 2)
        finally:
            st.HISTORY_DB, m.load_chats, m.CHATS_FILE, m.instance_by_ip = old[:4]
            st._fts_state.clear(); st._fts_state.update(old[4])

    def test_session_panel_data_and_log(self):
        """The chat's session panel: runtime, uptime, login state, platform
        rows, the instance's MCP servers with their secret state; the log
        route returns the console tail; both admin-only, unknown instance 404;
        nothing secret in the answer."""
        import mgr.store as st
        m = self.m
        tmp = tempfile.mkdtemp(prefix="e2e-session-")
        old = (m.load_instances, m.is_running, m.pidfile, m._settings.load_settings, m.secret_store, m.load_secret_policy,
               m.load_mcps, m.load_skills, m._paths.RUN_DIR, m.instance_by_ip, m.PW, st.HISTORY_DB, m.image_state)
        try:
            inst = {"name": "vm1", "index": 3, "template": "openrouter", "rootfs": "instances/openrouter-rootfs.ext4",
                    "config": {"OPENROUTER_MODEL": "x/y", "MCP_SERVERS": "caldav,homeassistant"}}
            m.load_instances = lambda: [inst]
            m.is_running = lambda i: True
            m.image_state = lambda i: (False, 0, 0)
            m._paths.RUN_DIR = tmp; st.HISTORY_DB = os.path.join(tmp, "history.db")
            pf = os.path.join(tmp, "vm1.pid"); open(pf, "w").write("1"); os.utime(pf, (time.time() - 7500,) * 2)
            m.pidfile = lambda i: pf
            with open(os.path.join(tmp, "vm1.log"), "w") as fh:
                fh.write("[init] harness from /dev/vdc\nagent ready\n")
            m._settings.load_settings = lambda: {"LLM_KEY_PROXY": "1", "BRAVE_API_KEY": "b"}
            m.secret_store = lambda: {"CALDAV_PASSWORD": "s3cret", "HA_TOKEN": "t"}
            m.load_secret_policy = lambda: {"by_template": {}, "by_instance": {"vm1": ["HA_TOKEN"]}, "guest_readable": []}
            m.load_mcps = lambda: [{"name": "caldav", "command": "c", "env": {"CALDAV_PASSWORD": "${CALDAV_PASSWORD}"}},
                                   {"name": "homeassistant", "command": "h", "args": ["Bearer ${HA_TOKEN}"]}]
            m.load_skills = lambda: [{"name": "a"}, {"name": "b"}]
            m.instance_by_ip = lambda ip: None; m.PW = ""
            d = m.session_info(inst)
            self.assertEqual((d["runtime"], d["login"], d["model"]), ("openrouter-agent", "key proxy", "x/y"))
            self.assertTrue(7400 < d["uptime"] < 7700)
            self.assertEqual({x["name"]: x["ready"] for x in d["mcps"]}, {"caldav": False, "homeassistant": True})
            self.assertEqual(d["need_secret"], 1)
            self.assertIn("2 in catalog", [p["state"] for p in d["platform"] if p["name"] == "Skills"][0])
            self.assertNotIn("s3cret", json.dumps(d))
            h = self._handler("/api/session/vm1", "10.0.0.5"); h._do_GET()
            body = json.loads(h.wfile.getvalue().split(b"\r\n\r\n", 1)[1])
            self.assertEqual(body["name"], "vm1")
            h = self._handler("/api/session/vm1/log", "10.0.0.5"); h._do_GET()
            self.assertTrue(h.wfile.getvalue().endswith(b"agent ready\n"))
            h = self._handler("/api/session/nope", "10.0.0.5"); h._do_GET()
            self.assertEqual(self._status(h), 404)
            m.instance_by_ip = lambda ip: inst
            h = self._handler("/api/session/vm1", "172.30.3.2"); h._do_GET()
            self.assertEqual(self._status(h), 403)                                  # guests: no
            # a claude instance reports its host credential, not a key
            m.instance_by_ip = lambda ip: None
            cl = {**inst, "template": "claude", "config": {}}
            self.assertRegex(m.session_info(cl)["login"], r"^(ok · valid \d+h \d+m|expired on the host.*|missing \(log in on the host\))$")
            self.assertNotIn("commands", m.session_info(cl))                       # the / picker lists them
        finally:
            (m.load_instances, m.is_running, m.pidfile, m._settings.load_settings, m.secret_store, m.load_secret_policy,
             m.load_mcps, m.load_skills, m._paths.RUN_DIR, m.instance_by_ip, m.PW, st.HISTORY_DB, m.image_state) = old

    def test_chat_page_carries_panel_and_search(self):
        """The rendered chat page has the session panel, the search bar and
        the two header toggles from the design; the agent list is embedded."""
        import chatui
        page = chatui.render([{"name": "vm1", "running": True}], "vm1", "")
        for needle in ('id=panel', 'id=searchbar', 'id=searchBtn', 'id=panelBtn', "'/api/session/'",
                       'function searchApply', 'function panelLoad', '"vm1"',
                       "convs.filter(x=>x.agent===agent)"):                    # dropdown opens that instance's chat
            self.assertIn(needle, page, needle)
        self.assertNotIn("cell('Commands'", page)                              # the / picker lists them

    def test_settings_for_ui_masks_every_credential_shaped_value(self):
        """Not only the schema's key params: any setting named like a
        credential is replaced by the placeholder before the page embeds it;
        the placeholder never overwrites the stored value on save."""
        m = self.m
        old = m._settings.load_settings
        try:
            m._settings.load_settings = lambda: {"OPENROUTER_API_KEY": "sk-or-x", "HF_TOKEN": "hf_abc", "MY_PASSWORD": "p",
                                       "BRAVE_API_KEY": "b", "SIGNAL_NUMBER": "+49", "TTS_VOICE": "de-thorsten-high", "EMPTY_KEY": ""}
            ui = m._settings.settings_for_ui()
            self.assertEqual({k: v for k, v in ui.items() if v == m._settings.SETTINGS_KEEP},
                             {k: m._settings.SETTINGS_KEEP for k in ("OPENROUTER_API_KEY", "HF_TOKEN", "MY_PASSWORD", "BRAVE_API_KEY")})
            self.assertEqual((ui["SIGNAL_NUMBER"], ui["TTS_VOICE"], ui["EMPTY_KEY"]), ("+49", "de-thorsten-high", ""))
            self.assertNotIn("hf_abc", json.dumps(ui))
        finally:
            m._settings.load_settings = old

    def test_secret_store_edit_from_the_ui(self):
        """Values are added, replaced and deleted in the store file through
        admin routes: names validated, other lines and comments kept, the
        file stays 0600, values never come back through /api/secret-keys."""
        m = self.m
        tmp = tempfile.mkdtemp(prefix="e2e-secstore-")
        old = m.SECRETS_FILE, m._settings.load_settings, m.instance_by_ip, m.PW
        try:
            m.SECRETS_FILE = os.path.join(tmp, "secrets.env")
            with open(m.SECRETS_FILE, "w") as fh:
                fh.write("# my secrets\nHA_TOKEN=old\nMRMUSIC_TOKEN=m\n")
            os.chmod(m.SECRETS_FILE, 0o600)
            m._settings.load_settings = lambda: {"OPENROUTER_API_KEY": "sk"}
            self.assertEqual(m.secret_set("CALDAV_PASSWORD", "p4ss"), "CALDAV_PASSWORD added")
            self.assertEqual(m.secret_set("HA_TOKEN", "new"), "HA_TOKEN replaced")
            self.assertIn("invalid name", m.secret_set("bad-name", "x"))
            self.assertIn("one non-empty line", m.secret_set("X_KEY", "a\nb"))
            self.assertIn("one non-empty line", m.secret_set("X_KEY", "  "))
            txt = open(m.SECRETS_FILE).read()
            self.assertEqual(txt, "# my secrets\nHA_TOKEN=new\nMRMUSIC_TOKEN=m\nCALDAV_PASSWORD=p4ss\n")
            self.assertEqual(os.stat(m.SECRETS_FILE).st_mode & 0o777, 0o600)
            self.assertEqual(m.secret_delete("MRMUSIC_TOKEN"), "MRMUSIC_TOKEN deleted")
            self.assertEqual(m.secret_delete("MRMUSIC_TOKEN"), "MRMUSIC_TOKEN not in the store")
            self.assertEqual(set(m.load_secrets_file()), {"HA_TOKEN", "CALDAV_PASSWORD"})
            m.instance_by_ip = lambda ip: None; m.PW = ""
            h = self._handler("/api/secret-keys", "10.0.0.5"); h._do_GET()
            body = json.loads(h.wfile.getvalue().split(b"\r\n\r\n", 1)[1])
            self.assertEqual(body["sources"], {"CALDAV_PASSWORD": "store", "HA_TOKEN": "store", "OPENROUTER_API_KEY": "settings"})
            self.assertNotIn("p4ss", h.wfile.getvalue().decode()); self.assertNotIn("new", body["keys"])
            h = self._post_handler("/api/secret-store", "10.0.0.5", b'{"name": "NEW_KEY", "value": "v"}'); h.do_POST()
            self.assertIn(b"NEW_KEY added", h.wfile.getvalue())
            h = self._post_handler("/api/secret-store/NEW_KEY/delete", "10.0.0.5", b"{}"); h.do_POST()
            self.assertIn(b"NEW_KEY deleted", h.wfile.getvalue())
            guest = {"name": "vm1", "index": 3, "config": {}}
            m.instance_by_ip = lambda ip: guest
            h = self._post_handler("/api/secret-store", "172.30.3.2", b'{"name": "X_KEY", "value": "v"}'); h.do_POST()
            self.assertEqual(self._status(h), 403)                                       # guests: never
        finally:
            m.SECRETS_FILE, m._settings.load_settings, m.instance_by_ip, m.PW = old

    def test_guest_get_denylist_covers_ui_proxy_and_terminal(self):
        """GET /i/<other>/term opened the shell of every other VM — only POST
        was gated. The denylist names the admin UI, chat, katfs and /i/."""
        m = self.m
        for p in ("/", "/chat", "/chat?i=x", "/katfs", "/katfs/", "/katfs/x.txt",
                  "/i/orchestrator/", "/i/orchestrator/term", "/i/x/term/ws?y=1"):
            self.assertTrue(m.guest_get_blocked(p), p)
        for p in ("/api/agents", "/api/memory/self", "/api/skills?meta=1",
                  "/api/inbox", "/chatx", "/logo.svg"):
            self.assertFalse(m.guest_get_blocked(p), p)

    def test_guest_gets_403_on_denied_paths_and_skips_basic_auth(self):
        """Guests carry no credentials (identity = source IP), so they pass
        _auth even with MANAGER_PASS set; an admin without credentials gets
        401. Denied GET paths answer 403 before any handler runs."""
        m = self.m
        guest = {"name": "hass", "index": 7, "config": {}}
        old_ibi, old_pw = m.instance_by_ip, m.PW
        try:
            m.instance_by_ip = lambda ip: guest if ip == "172.30.7.2" else None
            m.PW = "secret"
            for p in ("/i/orchestrator/term", "/", "/chat", "/katfs/", "/api/inbox",
                      "/no/such/page"):
                h = self._handler(p, "172.30.7.2")
                self.assertTrue(h._auth(), p)
                h._do_GET()
                self.assertIn(b" 403 ", h.wfile.getvalue().split(b"\r\n", 1)[0], p)
            h = self._handler("/", "192.168.1.5")
            self.assertFalse(h._auth())
            self.assertIn(b" 401 ", h.wfile.getvalue().split(b"\r\n", 1)[0])
            h = self._handler("/", "192.168.1.5", auth="Basic " + base64.b64encode(
                f"{m.USER}:secret".encode()).decode())
            self.assertTrue(h._auth())
        finally:
            m.instance_by_ip, m.PW = old_ibi, old_pw

    def test_guest_task_target_policy(self):
        """A guest may task itself or an ephemeral VM; other instances only via
        DELEGATE_TARGETS in its config ('*' = all); the orchestrator: all."""
        m = self.m
        me = {"name": "jobresearcher", "config": {}}
        for t in ("ephemeral", "", None, "jobresearcher"):
            self.assertTrue(m.guest_may_target(me, t), repr(t))
        for t in ("orchestrator", "hass", "claudy"):
            self.assertFalse(m.guest_may_target(me, t), t)
        me["config"]["DELEGATE_TARGETS"] = "hass, claudy"
        self.assertTrue(m.guest_may_target(me, "hass"))
        self.assertTrue(m.guest_may_target(me, "claudy"))
        self.assertFalse(m.guest_may_target(me, "orchestrator"))
        me["config"]["DELEGATE_TARGETS"] = "*"
        self.assertTrue(m.guest_may_target(me, "orchestrator"))
        orch = {"name": m.ORCH_INSTANCE, "config": {}}
        self.assertTrue(m.guest_may_target(orch, "anything"))

    def test_history_search_scoped_to_instance(self):
        """recall_tasks from a guest returns only runs it created or executed."""
        import mgr.store as st
        tmp = tempfile.mkdtemp(prefix="e2e-hist-")
        old = st.HISTORY_DB
        try:
            st.HISTORY_DB = os.path.join(tmp, "h.db")
            st.history_add("hass", "light on", "done", True, origin="worker")
            st.history_add("orchestrator", "ping", "pong", True, origin="jobresearcher")
            st.history_add("claudy", "review", "ok", True, origin="orchestrator")
            self.assertEqual(len(st.history_search()), 3)
            self.assertEqual([r["target"] for r in st.history_search(instance="hass")], ["hass"])
            self.assertEqual([r["target"] for r in st.history_search(instance="jobresearcher")],
                             ["orchestrator"])
            self.assertEqual(st.history_search(instance="remote"), [])
            self.assertEqual(len(st.history_search("light", instance="hass")), 1)
            self.assertEqual(st.history_search("light", instance="orchestrator"), [])
        finally:
            st.HISTORY_DB = old

    def test_hitl_status_bound_to_requesting_instance(self):
        """Approval ids are 8 hex chars — a guest may only poll its own."""
        m = self.m
        import mgr.signal as sigmod
        old_send = sigmod.signal_send
        try:
            sigmod.signal_send = lambda text, to=None: (True, "sent")
            hid = m.hitl_create("hass", "bash", "rm x")
            self.assertEqual(m.hitl_status(hid), "pending")            # admin
            self.assertEqual(m.hitl_status(hid, "hass"), "pending")    # owner
            self.assertEqual(m.hitl_status(hid, "uncensored"), "unknown")
        finally:
            sigmod.signal_send = old_send

    def test_guest_input_rules_only_manager_and_nfs(self):
        """Guest -> host is limited to :8700 and NFS; the DROP is inserted first
        so the ACCEPTs land above it. Per tap, a source other than the guest's
        own /30 address is dropped (the IP is the guest's identity)."""
        import types
        m = self.m
        calls = []
        old_sh = m.sh
        try:
            # every -C fails -> "rule missing" -> everything gets inserted
            m.sh = lambda *a, check=True: (calls.append(a), types.SimpleNamespace(returncode=1))[1]
            m.ensure_guest_input_rules()
            ins = [c for c in calls if c[1] == "-I"]
            self.assertEqual(ins[0][2:], ("INPUT", "1", "-i", "fc+", "-j", "DROP"))
            ports = {c[c.index("--dport") + 1] for c in ins if "--dport" in c}
            self.assertEqual(ports, {str(m.LISTEN[1]), "2049"})
            self.assertTrue(any("ESTABLISHED,RELATED" in c for c in ins))
            self.assertTrue(all(c[2] == "INPUT" and ("fc+" in c or m.POOL in c) for c in ins))
            # a pool source on the LAN interface is forged: dropped before any by-IP check
            self.assertIn(("iptables", "-I", "INPUT", "1", "-i", m.HOSTIF, "-s", m.POOL, "-j", "DROP"), ins)
            # an ACCEPT that already exists (maybe below the DROP) is removed and re-inserted on top
            calls.clear()
            state = {"nfs": 1}      # one stray copy of the NFS rule
            def sh2(*a, check=True):
                calls.append(a)
                if a[1] == "-D" and "2049" in a and state["nfs"] > 0:
                    state["nfs"] -= 1; return types.SimpleNamespace(returncode=0)
                return types.SimpleNamespace(returncode=1)
            m.sh = sh2
            m.ensure_guest_input_rules()
            nfs = [c for c in calls if "2049" in c]
            self.assertEqual([c[1] for c in nfs], ["-D", "-D", "-I"])      # delete until gone, then insert at 1
            self.assertEqual(nfs[-1][2:4], ("INPUT", "1"))
            calls.clear()
            m.ensure_antispoof({"name": "hass", "index": 7})
            spec = ("-i", "fc7", "!", "-s", "172.30.7.2", "-j", "DROP")
            self.assertEqual({c[2] for c in calls if c[1] == "-I"}, {"INPUT", "FORWARD"})
            for c in calls:
                if c[1] == "-I":
                    self.assertEqual(c[4:], spec)
        finally:
            m.sh = old_sh


# ===========================================================================
# HTTP: against the running manager
# ===========================================================================
@unittest.skipUnless(_manager_up(), "manager on 127.0.0.1:8700 not reachable")
class ManagerHTTP(unittest.TestCase):
    def test_root_page(self):
        st, _ = _http("/")
        self.assertEqual(st, 200)

    def test_agents_have_backend_and_model(self):
        st, txt = _http("/api/agents")
        self.assertEqual(st, 200)
        agents = json.loads(txt).get("agents", [])
        self.assertTrue(agents)
        known = {"openrouter", "orcarouter", "anthropic", "pi", "prime", "llama", "claude"}
        for a in agents:
            self.assertIn("backend", a, f"{a.get('name')} without a backend field")
            self.assertIn("model", a)
            self.assertIn(a["backend"], known, f"unbekanntes backend {a['backend']}")

    def test_orchestrator_reports_orcarouter(self):
        """The most recently fixed bug: the orcarouter agent must not appear as
        'openrouter, no model'. Only checked WHEN the orchestrator runs via
        ORCAROUTER_MODEL."""
        st, txt = _http("/api/agents")
        orch = next((a for a in json.loads(txt)["agents"] if a["name"] == "orchestrator"), None)
        if not orch:
            self.skipTest("no orchestrator")
        if orch["backend"] == "orcarouter":
            self.assertTrue(orch["model"], "orcarouter backend but empty model (the old bug)")

    def test_hitl_status_route(self):
        st, txt = _http("/api/hitl/deadbeef")
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(txt).get("status"), "unknown")

    def test_katfs_status_route(self):
        st, txt = _http("/api/katfs/status")
        self.assertEqual(st, 200)
        self.assertIn("up", json.loads(txt))

    def test_missions_get(self):
        st, txt = _http("/api/missions?instance=orchestrator")
        self.assertEqual(st, 200)
        self.assertIn("missions", json.loads(txt))

    def test_usage_by_model_folds_task_vms(self):
        """The Resources chart's data: one row per instance AND model; the
        one-shot task VMs are summed as "tasks"; `since` cuts by time."""
        import mgr.store as st
        old = st.HISTORY_DB, st._migrated[0]
        tmp = tempfile.mkdtemp(prefix="e2e-ubm-")
        try:
            st.HISTORY_DB = os.path.join(tmp, "history.db"); st._migrated[0] = False
            now = int(time.time())
            for inst, model, pt, ct, cost, ts in (("orch", "a/x", 100, 10, 0.01, now), ("orch", "b/y", 50, 5, 0.02, now),
                                                  ("task-0a1b2c", "a/x", 20, 2, 0.001, now), ("task-3d4e5f", "a/x", 30, 3, 0.002, now),
                                                  ("orch", "a/x", 1000, 1, 1.0, now - 100000)):
                with st._hist_lock, st._hist_conn() as c:
                    c.execute("INSERT INTO llm_usage(ts,instance,model,prompt_tokens,completion_tokens,cost) VALUES(?,?,?,?,?,?)",
                              (ts, inst, model, pt, ct, cost))
            rows = {(r["instance"], r["model"]): r for r in st.usage_by_model(now - 3600)}
            self.assertEqual(set(rows), {("orch", "a/x"), ("orch", "b/y"), ("tasks", "a/x")})
            self.assertEqual((rows[("tasks", "a/x")]["in"], rows[("tasks", "a/x")]["calls"], rows[("tasks", "a/x")]["cost"]), (50, 2, 0.003))
            self.assertEqual(rows[("orch", "a/x")]["in"], 100)                   # the old row is outside the window
            self.assertEqual(st.usage_by_model(0)[0], {**rows[("orch", "a/x")], "in": 1100, "out": 11, "calls": 2, "cost": 1.01})
            st_, txt = _http("/api/usage-by-model?since=0")
            self.assertEqual(st_, 200); self.assertIn("rows", json.loads(txt))
        finally:
            st.HISTORY_DB, st._migrated[0] = old

    def test_usage_by_instance(self):
        st, txt = _http("/api/usage/orchestrator?since=0")
        self.assertEqual(st, 200)
        d = json.loads(txt)
        for k in ("calls", "in", "out", "cost"):
            self.assertIn(k, d)

    def test_claude_bridge_reports_usage_and_traces(self):
        """The claude bridge, on a real turn, logs one summary line and posts a
        /api/usage and a /api/trace start+end so Logs/Resources/Traces show it."""
        import io, types
        wb = _load("web_bridge_obs", os.path.join(os.path.dirname(os.path.dirname(AGENT_PATH)), "claude", "web_bridge.py")
                   if os.path.exists(os.path.join(os.path.dirname(os.path.dirname(AGENT_PATH)), "claude", "web_bridge.py"))
                   else "/home/ulrich/claude-signal-firecracker/web_bridge.py")
        posts = []; logs = []
        old = wb._post, wb._log, wb.subprocess.run, wb._sync_credentials
        try:
            wb._post = lambda path, payload: posts.append((path, payload))
            wb._log = lambda *m: logs.append(" ".join(str(x) for x in m))
            wb._sync_credentials = lambda force=False: False
            fake = json.dumps({"result": "done", "session_id": "s1", "usage": {"input_tokens": 1200, "output_tokens": 80},
                               "total_cost_usd": 0.0, "num_turns": 3})
            wb.subprocess.run = lambda *a, **k: types.SimpleNamespace(stdout=fake, stderr="")
            out = wb._claude_run("hello", None, True)
            self.assertEqual(out, "done")
            paths = [p for p, _ in posts]
            self.assertIn("/api/usage", paths); self.assertEqual(paths.count("/api/trace"), 2)
            usage = next(pl for pp, pl in posts if pp == "/api/usage")
            self.assertEqual((usage["prompt_tokens"], usage["completion_tokens"], usage["direct"]), (1200, 80, True))
            self.assertTrue(usage["ok"])
            self.assertTrue(any("tokens 1200/80" in x for x in logs))
            self.assertTrue(wb._last_turn[0])                       # turn id set for the X-Kaim-Turn header
        finally:
            wb._post, wb._log, wb.subprocess.run, wb._sync_credentials = old

    def test_claude_bridge_syncs_the_host_login(self):
        """The claude VM works from a copy of the host's OAuth login that goes
        stale once the host rotates its refresh token. The bridge pulls the
        host's credential before a turn when it is newer, and once by force
        when Claude answers with the auth failure."""
        import io
        wb = _load("web_bridge_e2e", os.path.join(os.path.dirname(os.path.dirname(AGENT_PATH)), "claude", "web_bridge.py")
                   if os.path.exists(os.path.join(os.path.dirname(os.path.dirname(AGENT_PATH)), "claude", "web_bridge.py"))
                   else "/home/ulrich/claude-signal-firecracker/web_bridge.py")
        tmp = tempfile.mkdtemp(prefix="e2e-cred-")
        wb.CRED_PATH = os.path.join(tmp, ".claude", ".credentials.json")
        host = {"claudeAiOauth": {"accessToken": "new", "refreshToken": "r2", "expiresAt": 2000}}
        old_open = urllib.request.urlopen                 # the bridge shares the global module: restore it
        wb.urllib.request.urlopen = lambda url, timeout=None: io.BytesIO(json.dumps(host).encode())
        self.addCleanup(setattr, urllib.request, "urlopen", old_open)
        self.assertTrue(wb._sync_credentials())                                   # no copy yet -> written
        self.assertEqual(json.load(open(wb.CRED_PATH))["claudeAiOauth"]["accessToken"], "new")
        self.assertEqual(oct(os.stat(wb.CRED_PATH).st_mode & 0o777), "0o600")
        self.assertFalse(wb._sync_credentials())                                  # same age -> untouched
        host["claudeAiOauth"] = {"accessToken": "newer", "expiresAt": 3000}
        self.assertTrue(wb._sync_credentials())
        host["claudeAiOauth"] = {"accessToken": "older", "expiresAt": 1000}
        self.assertFalse(wb._sync_credentials()); self.assertTrue(wb._sync_credentials(force=True))
        self.assertEqual(json.load(open(wb.CRED_PATH))["claudeAiOauth"]["accessToken"], "older")
        # the turn: auth failure -> forced sync -> one retry
        calls = []
        wb._claude_run = lambda msg, resume, keep: calls.append(msg) or ("Failed to authenticate: OAuth session expired" if len(calls) == 1 else "hi")
        self.assertEqual(wb._claude_once("hello", None, True), "hi")
        self.assertEqual(len(calls), 2)
        wb.urllib.request.urlopen = lambda url, timeout=None: (_ for _ in ()).throw(OSError("no manager"))
        self.assertFalse(wb._sync_credentials(force=True))                         # no manager: keep the copy, no crash

    def test_version_and_update_check(self):
        """The installed version comes from VERSION (dev without it), the newest
        release from GitHub through a cached check whose failure is remembered,
        and "available" only when both parse and the release is newer."""
        m = _load("manager_e2e_ver", MANAGER_PATH)
        tmp = tempfile.mkdtemp(prefix="e2e-ver-")
        old = m._about.VERSION_FILE, m._about._fetch_latest_release, dict(m._about._update), os.environ.get("UPDATE_CHECK")
        try:
            os.environ.pop("UPDATE_CHECK", None)
            m._about.VERSION_FILE = os.path.join(tmp, "VERSION")
            self.assertEqual(m._about.installed_version(), "dev")
            open(m._about.VERSION_FILE, "w").write("v1.0.0-4-gabc\n")
            self.assertEqual(m._about.installed_version(), "v1.0.0-4-gabc")
            m._about._fetch_latest_release = lambda: {"latest": "v1.0.1", "url": "https://x/r", "notes": "n"}
            m._about._update.update(ts=0.0, latest="", url="", notes="", error="")
            u = m._about.update_check()
            self.assertEqual((u["latest"], u["error"]), ("v1.0.1", ""))
            self.assertTrue(m._about.update_available("v1.0.0-4-gabc", "v1.0.1"))
            self.assertTrue(m._about.update_available("1.0.0", "v1.0.1"))
            self.assertFalse(m._about.update_available("v1.0.1", "v1.0.1"))
            self.assertFalse(m._about.update_available("dev", "v1.0.1"))
            self.assertFalse(m._about.update_available("v1.0.1", ""))
            def boom():
                raise OSError("offline")
            m._about._fetch_latest_release = boom
            self.assertEqual(m._about.update_check()["latest"], "v1.0.1")            # cached, not refetched
            e = m._about.update_check(force=True)
            self.assertEqual(e["latest"], "v1.0.1"); self.assertIn("offline", e["error"])  # kept, error noted
            st = m._about.update_status()
            for k in ("unit", "updating", "log"):
                self.assertIn(k, st)
            st_, txt = _http("/api/version")
            self.assertEqual(st_, 200)
            d = json.loads(txt)
            for k in ("installed", "latest", "available", "unit", "updating", "log"):
                self.assertIn(k, d)
        finally:
            m._about.VERSION_FILE, m._about._fetch_latest_release = old[0], old[1]
            m._about._update.clear(); m._about._update.update(old[2])
            if old[3] is not None:
                os.environ["UPDATE_CHECK"] = old[3]

    def test_voice_health_route(self):
        st, txt = _http("/api/voice-health")
        self.assertEqual(st, 200)
        d = json.loads(txt)
        self.assertIn("ready", d)
        if d.get("voices"):
            self.assertIn("de-thorsten-medium", d["voices"])

    def test_notifications_get(self):
        st, txt = _http("/api/notifications")
        self.assertEqual(st, 200)
        d = json.loads(txt)
        self.assertIn("notifications", d)
        self.assertIn("unread", d)
        self.assertIsInstance(d["unread"], int)

    def test_memory_key_with_space_survives_the_url(self):
        """Store takes the key via JSON body, recall via URL path — a key like
        "jobsuche Firmen" could be stored but never retrieved (live bug). The
        path segments are URL-decoded now, and a slash inside a key stays one
        key."""
        import urllib.parse as up
        st, _ = _http("/api/memory/e2e-memtest", "POST",
                      {"key": "jobsuche Firmen", "value": "Ford, Bayer"})
        self.assertEqual(st, 200)
        st, txt = _http("/api/memory/e2e-memtest/" + up.quote("jobsuche Firmen", safe=""))
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(txt).get("value"), "Ford, Bayer")
        # Slash inside a key: everything after the instance is ONE key.
        st, _ = _http("/api/memory/e2e-memtest", "POST",
                      {"key": "a/b c", "value": "x"})
        self.assertEqual(st, 200)
        st, txt = _http("/api/memory/e2e-memtest/" + up.quote("a/b c", safe=""))
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(txt).get("value"), "x")
        # Clean up after ourselves: value null deletes — the production memory
        # store is not a place for test residue (bit the saddler once).
        for k in ("jobsuche Firmen"[:0] or "jobsuche Firmen", "a/b c"):
            _http("/api/memory/e2e-memtest", "POST", {"key": k, "value": None})
        st, txt = _http("/api/memory/e2e-memtest")
        self.assertEqual(json.loads(txt), {}, "test residue left in memory store")

    def test_orcarouter_template_registered(self):
        st, txt = _http("/")
        self.assertEqual(st, 200)
        self.assertIn("orcarouter", txt)

    def test_stopped_instance_api_error_is_plain_text(self):
        """The app pours the body of an API answer straight into the chat bubble.
        For a stopped/unknown instance that must be plain text — HTML showed up
        there as a raw "<p>Instance … is not running</p>"."""
        req = urllib.request.Request(MANAGER_URL + "/i/e2e-gibtsnicht/api/chat",
                                     data=b'{"message":"hi"}', method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=8)
            self.fail("expected 503")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            self.assertEqual(e.code, 503)
            self.assertTrue(e.headers.get("Content-Type", "").startswith("text/plain"))
            self.assertNotIn("<", body)
            self.assertIn("not running", body)

    def test_stopped_instance_page_stays_html(self):
        """The browser path keeps its markup."""
        st, txt = _http("/i/e2e-gibtsnicht/")
        self.assertEqual(st, 503)
        self.assertIn("<p>", txt)


# ===========================================================================
# LIVE: round-trip to the agent VM (free /goal path only)
# ===========================================================================
@unittest.skipUnless(_orchestrator_running(), "orchestrator VM not running")
class LiveAgent(unittest.TestCase):
    @staticmethod
    def _reply(txt):
        # The manager proxy returns the reply as plain text; falls back to JSON
        # {"reply": ...} in case that ever changes.
        txt = txt.strip()
        if txt.startswith("{"):
            try:
                return json.loads(txt).get("reply", txt)
            except Exception:
                return txt
        return txt

    def test_goal_command_roundtrip(self):
        """Proves the new agent code is live in the VM — without a model call,
        so without token cost."""
        st, txt = _http("/i/orchestrator/api/chat", "POST",
                        {"message": "/goal show"}, timeout=30)
        self.assertEqual(st, 200)
        self.assertIn("goal", self._reply(txt))

    def test_tools_registry_after_rebuild(self):
        """Smoke test for a rootfs rebuild, no model call: '/tools' on an
        instance that was STARTED AFTER the current image was built must list
        its built-ins and every assigned MCP with the full 'server__tool'
        name. Instances still on an older image are skipped — a stale one
        would answer through the model (cost, no signal)."""
        img = os.path.join(FC_DIR, "instances", "openrouter-rootfs.ext4")
        if not os.path.exists(img):
            self.skipTest("no openrouter rootfs here")
        built = os.path.getmtime(img)
        st, txt = _http("/api/agents")
        fresh = []
        for a in json.loads(txt).get("agents", []):
            pf = os.path.join(FC_DIR, "run", a["name"] + ".pid")
            if a.get("running") and a.get("template") == "openrouter" \
                    and os.path.exists(pf) and os.path.getmtime(pf) > built:
                fresh.append(a)
        if not fresh:
            self.skipTest("no openrouter instance started on the current image")
        for a in fresh[:2]:
            # A freshly (re)started VM answers 502/503 through the proxy until
            # the agent bridge is up (MCP init takes ~20 s) — wait, don't fail.
            for _ in range(40):
                st, txt = _http(f"/i/{a['name']}/api/chat", "POST", {"message": "/tools"}, timeout=30)
                if st == 200:
                    break
                time.sleep(3)
            self.assertEqual(st, 200, f"{a['name']}: bridge not up after 120 s ({txt[:80]})")
            rep = self._reply(txt)
            self.assertIn("built-in (", rep, a["name"])
            for mcp in a.get("mcps", []):
                self.assertIn(mcp + "__", rep, f"{a['name']}: MCP '{mcp}' not registered")

    def test_reasoning_command_roundtrip(self):
        st, txt = _http("/i/orchestrator/api/chat", "POST",
                        {"message": "/reasoning"}, timeout=30)
        self.assertEqual(st, 200)
        # /reasoning without an argument shows the status -> some text comes back
        self.assertTrue(self._reply(txt))


if __name__ == "__main__":
    # Short environment report, then unittest.
    print(f"manager reachable: {_manager_up()} | orchestrator running: {_orchestrator_running()}",
          file=sys.stderr)
    unittest.main(verbosity=2)
