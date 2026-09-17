# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""The agent loop: a turn (run / run_stream) with its time budget, the tool loop, steering (interrupting a running turn), auto reset, and the goal loop.

Part of the openrouter agent package (runs inside the VM): no import from the package root. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import json
import os
import re
import threading
import time
import uuid

from . import config as _config
from . import context as _context
from . import learn as _learn
from . import llm as _llm
from . import observe as _observe
from . import tools as _tools


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
        m = _llm.or_chat([{"role": "system", "content": JUDGE_PROMPT},
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
        hist.append({"role": "user", "content":
                     f"Your last answer does not yet meet the goal: {_goal}. "
                     f"Critique: {fb}. Improve the answer accordingly."})
        answer = _tool_loop(hist)
    return answer


# --- steering: interrupt the running agent -----------------------------------
# While a turn is running (tool loop), the user can push in additional messages
# (run_agent: POST /api/steer). They are fed in between two tool
# steps as a user message — the agent changes course instead of
# stubbornly running to the end.
_steer_lock = threading.Lock()
_steer_q = []
_busy = [False]
# Auto-reset: a context that idled for AUTO_RESET_MIN minutes starts over at
# the next turn (0 = never). For a voice instance every "radio on" otherwise
# pays for the whole day's history on each of its two model calls.
AUTO_RESET_MIN = int(os.environ.get("AUTO_RESET_MIN", "0") or 0)
_last_turn = [0.0]


def _auto_reset():
    """Called at the start of a turn, before the injections: drops the
    conversation if the last turn is older than AUTO_RESET_MIN minutes.
    Returns True when it did (the turn then starts on a fresh context)."""
    now = time.time()
    last, _last_turn[0] = _last_turn[0], now
    if AUTO_RESET_MIN <= 0 or not last or now - last < AUTO_RESET_MIN * 60 or len(_context._history) <= 1:
        return False
    del _context._history[1:]
    _config.log(f"auto-reset: context idle for {int((now - last) // 60)} min (> {AUTO_RESET_MIN}), starting fresh")
    return True


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


DEADLINE_MARGIN = 45           # seconds before the deadline reserved for the final answer
_DEADLINE_NOTE = ("[TimeBudget] The time budget for this turn is exhausted. Answer NOW with "
                  "what you have: a partial result is fine, say what is still missing. "
                  "No more tools.")


def _out_of_time():
    return bool(_context._deadline[0]) and time.time() > _context._deadline[0] - DEADLINE_MARGIN


def _tool_loop(hist):
    """Tool loop on an arbitrary message list. `hist` is either
    the persistent _history (conversation) or a throwaway list (heartbeat)."""
    for _ in _config._step_iter():
        _drain_steer(hist)
        if _out_of_time():
            hist.append({"role": "system", "content": _DEADLINE_NOTE})
            msg = _llm.or_chat(hist, [])                       # tools off: the final answer
            hist.append(msg)
            return (msg.get("content") or "(empty answer)") + "\n\n⏱️ (time budget exhausted — partial result)"
        msg = _llm.or_chat(hist, _tools.TOOLS)
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
            out = "(time budget exhausted — not executed)" if _out_of_time() else _tools.exec_tool(fn["name"], args)
            _config.log("tool", fn["name"], "->", "(redacted)" if fn["name"] == "get_secret" else out[:80].replace("\n", " "))
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
    _context._deadline[0] = float(deadline or 0)
    ts = _turn_steps(user_message)
    if ts:
        saved, _config.MAX_STEPS = _config.MAX_STEPS, ts[0]      # config's module global, per turn
        try:
            return run(ts[1], kind=kind, turn=turn)
        finally:
            _config.MAX_STEPS = saved
    _observe._turn_id[0] = turn or uuid.uuid4().hex[:8]
    user_message = _context._expand_prompt(user_message)
    if user_message.strip() == "/reset":
        del _context._history[1:]
        globals()["_goal"] = None      # a stale goal would drive the goal loop on every later turn
        return "🔄 Context reset."
    if user_message.startswith("/reasoning"):
        return _config._set_reasoning(user_message)
    if user_message.startswith("/goal"):
        return _set_goal(user_message)
    if user_message.strip() == "/tools":
        return _tools._tools_report()
    if user_message.startswith("/model"):
        return _config._set_model(user_message)
    if user_message.startswith("/steps"):
        return _config._set_steps(user_message)
    if user_message.startswith(("/aside", "/branch")):
        return _context._branch_open(user_message)
    if user_message.startswith("/back"):
        return _context._branch_close(user_message)
    # /fresh: run statelessly in a throwaway context — the conversation
    # _history stays untouched (otherwise a heartbeat would wipe out a running
    # app chat, because both share the same _history). For the
    # orchestrator heartbeat: look, delegate, discard.
    if user_message.startswith("/fresh"):
        m = user_message[len("/fresh"):].strip()
        hist = [{"role": "system", "content": _config.SYSTEM},
                {"role": "system", "content": _context._now_line()},
                {"role": "user", "content": m}]
        _observe._trace_begin("fresh")
        out = "⚠️ (no answer)"
        try:
            out = _tool_loop(hist)
            return out
        finally:
            _observe._trace_end(_learn._outcome_of(out))
            _learn._maybe_learn(hist, m, _learn._outcome_of(out))
    _auto_reset()
    _context._trim_history()
    _context._inject_playbooks()
    _context._inject_missions()
    _context._inject_memory_index()
    _context._inject_now()
    _context._recall(user_message)
    _context._history.append({"role": "user", "content": user_message})
    _busy[0] = True
    _observe._trace_begin(kind)
    out = "⚠️ (no answer)"
    try:
        out = _run_goal(_context._history, user_message) if _goal else _tool_loop(_context._history)
        return out
    finally:
        _busy[0] = False
        _observe._trace_end(_learn._outcome_of(out))
        _learn._maybe_learn(_context._history, user_message, _learn._outcome_of(out))


def run_stream(user_message, on_token, image=None, deadline=0.0, kind="stream", turn=None):
    _context._deadline[0] = float(deadline or 0)
    _observe._turn_id[0] = turn or uuid.uuid4().hex[:8]
    """Like run(), but streams the answer tokens via on_token. Tool rounds
    produce no text; the final answer is streamed.
    image: optional base64 JPEG -> sent as vision content to OpenRouter."""
    user_message = _context._expand_prompt(user_message)
    if user_message.strip() == "/reset":
        del _context._history[1:]
        globals()["_goal"] = None      # a stale goal would drive the goal loop on every later turn
        on_token("🔄 Context reset.")
        return
    if user_message.startswith("/reasoning"):
        on_token(_config._set_reasoning(user_message))
        return
    if user_message.startswith("/goal"):
        on_token(_set_goal(user_message))
        return
    if user_message.strip() == "/tools":
        on_token(_tools._tools_report())
        return
    if user_message.startswith("/model"):
        on_token(_config._set_model(user_message))
        return
    if user_message.startswith("/steps"):
        on_token(_config._set_steps(user_message))
        return
    if user_message.startswith(("/aside", "/branch")):
        on_token(_context._branch_open(user_message))
        return
    if user_message.startswith("/back"):
        on_token(_context._branch_close(user_message))
        return
    # /fresh: stateless as in run(), conversation untouched. Heartbeats
    # need no streaming — emit the answer once.
    if user_message.startswith("/fresh"):
        m = user_message[len("/fresh"):].strip()
        on_token(_tool_loop([{"role": "system", "content": _config.SYSTEM},
                             {"role": "user", "content": m}]))
        return
    _auto_reset()
    _context._trim_history()
    _context._inject_playbooks()
    _context._inject_missions()
    _context._inject_memory_index()
    _context._inject_now()
    _context._recall(user_message)
    if image:
        content = [
            {"type": "text", "text": user_message or "What is in the image?"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}},
        ]
    else:
        content = user_message
    _context._history.append({"role": "user", "content": content})
    _busy[0] = True
    _observe._trace_begin(kind)
    outcome = "error"
    try:
        if _goal:
            # With an active goal the answer is refined against the judge (not
            # streamed) and then emitted as a whole.
            ans = _run_goal(_context._history, user_message)
            on_token(ans)
            outcome = _learn._outcome_of(ans)
            return
        for _ in _config._step_iter():
            _drain_steer(_context._history, on_token)
            if _out_of_time():
                _context._history.append({"role": "system", "content": _DEADLINE_NOTE})
                _context._history.append(_llm.or_chat_stream(_context._history, [], on_token))
                on_token("\n\n⏱️ (time budget exhausted — partial result)")
                outcome = "deadline"
                return
            msg = _llm.or_chat_stream(_context._history, _tools.TOOLS, on_token)
            _context._history.append(msg)
            tcs = msg.get("tool_calls")
            if not tcs:
                if _drain_steer(_context._history, on_token):
                    continue
                outcome = _learn._outcome_of(msg.get("content"))
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
                    while not ev.wait(_config.HEARTBEAT_SEC):
                        try:
                            on_token(" \u00b7")
                        except Exception:
                            return
                _hb = threading.Thread(target=_heartbeat, daemon=True)
                _hb.start()
                try:
                    out = _tools.exec_tool(fn["name"], args)
                finally:
                    _hb_stop.set()
                    _hb.join(timeout=1)
                on_token("\n")
                _config.log("tool", fn["name"], "->", "(redacted)" if fn["name"] == "get_secret" else out[:80].replace("\n", " "))
                _context._history.append({"role": "tool", "tool_call_id": tc["id"], "content": out})
        on_token("\n(max tool steps reached)")
        outcome = "max_steps"
    finally:
        _busy[0] = False
        _observe._trace_end(outcome)
        _learn._maybe_learn(_context._history, user_message, outcome)
