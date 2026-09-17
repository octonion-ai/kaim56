# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""The LLM transport: retries and timeouts, the wire format (system folding for local models, strict alternation), one chat completion and the streamed variant.

Part of the openrouter agent package (runs inside the VM): no import from the package root. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import json
import os
import socket
import time
import urllib.error

from . import config as _config
from . import mgrclient as _mgrclient
from . import observe as _observe


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
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "600" if _config.LLAMA_ENDPOINT else "120"))
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
    return not (_config.LLAMA_ENDPOINT and _is_timeout(e))
_RETRY_CODES = {408, 409, 429, 500, 502, 503, 504}


def _retry_sleep(attempt):
    # 0.5s, 1s, 2s, 4s … capped at 8s.
    time.sleep(min(8.0, 0.5 * (2 ** attempt)))



# --- OpenRouter chat --------------------------------------------------------
FOLD_SYSTEM = os.environ.get("LLM_FOLD_SYSTEM", "1" if _config.LLAMA_ENDPOINT else "0") not in ("0", "false", "False", "")


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
    out = [first]
    for m in rest:
        # A-5: strict user/assistant alternation. Two adjacent messages of the
        # same role break local chat templates (llama.cpp/Qwen: "2 or more
        # assistant messages at the end"). Merge adjacent plain-text user OR
        # assistant messages; never touch one carrying tool_calls, and never a
        # tool message (it must follow its tool_calls assistant).
        prev = out[-1] if out else None
        if (prev is not None and prev.get("role") == m.get("role") in ("user", "assistant")
                and not prev.get("tool_calls") and not m.get("tool_calls")
                and isinstance(prev.get("content"), str) and isinstance(m.get("content"), str)):
            out[-1] = {**prev, "content": (prev["content"] + "\n\n" + m["content"]).strip()}
        else:
            out.append(m)
    return out


def or_chat(messages, tools, model=None):
    messages = _wire_messages(messages)
    _b = {"model": model or _config.OR_MODEL, "messages": messages, "usage": {"include": True}}
    if tools:                       # do NOT send an empty tools list (400)
        _b["tools"] = tools
        _b["tool_choice"] = "auto"
    if _config._reasoning:
        _b["reasoning"] = {"effort": _config._reasoning}
    body = json.dumps(_b).encode()
    last = ""
    _observe._turn_step[0] += 1
    t0 = time.monotonic()
    for attempt in range(LLM_RETRIES + 1):
        req = urllib.request.Request(_mgrclient._llm_url(), data=body, method="POST",
                                     headers=_mgrclient._llm_headers())
        try:
            r = urllib.request.urlopen(req, timeout=LLM_TIMEOUT)
            d = json.loads(r.read().decode())
            _mgrclient.report_usage(d.get("usage"), ms=int((time.monotonic() - t0) * 1000))
            return d["choices"][0]["message"]
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", "replace")[:400]
            if e.code == 400 and "image" in err_body.lower() \
                    and _strip_history_images(messages):
                _b["messages"] = messages
                body = json.dumps(_b).encode()
                continue           # images gone -> the turn gets another chance
            last = f"⚠️ {_config.LLM_NAME} HTTP {e.code}: {err_body[:300]}"
            if _config.LLAMA_ENDPOINT and e.code == 503:
                last = _LOADING_MSG
            elif e.code in _RETRY_CODES and attempt < LLM_RETRIES:
                _retry_sleep(attempt); continue
            _mgrclient.report_usage({}, ms=int((time.monotonic() - t0) * 1000), ok=False, err=last)
            return {"role": "assistant", "content": last}
        except Exception as e:
            last = f"⚠️ {_config.LLM_NAME} error: {e!r}"
            if _config.LLAMA_ENDPOINT and _conn_dropped(e):
                last = _llama_dropped_msg(messages)
            elif _retry_after(e, attempt):
                _retry_sleep(attempt); continue
            _mgrclient.report_usage({}, ms=int((time.monotonic() - t0) * 1000), ok=False, err=last)
            return {"role": "assistant", "content": last}
    return {"role": "assistant", "content": last}



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
        b = {"model": _config.OR_MODEL, "messages": _wire_messages(messages), "stream": True, "usage": {"include": True}}
        if _config.LLAMA_ENDPOINT:
            b["stream_options"] = {"include_usage": True}   # llama.cpp: token counts in the last chunk
        if use_tools and tools:
            b["tools"] = tools
            b["tool_choice"] = "auto"
        if _config._reasoning:
            b["reasoning"] = {"effort": _config._reasoning}
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
    _observe._turn_step[0] += 1
    _t0 = time.monotonic()
    for attempt in range(LLM_RETRIES + 1):
        req = urllib.request.Request(_mgrclient._llm_url(), data=body, method="POST",
                                     headers=_mgrclient._llm_headers())
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
            m = f"⚠️ {_config.LLM_NAME} HTTP {e.code}: {err_body[:300]}"
            if _config.LLAMA_ENDPOINT and e.code == 503:
                m = _LOADING_MSG
            elif e.code in _RETRY_CODES and attempt < LLM_RETRIES:
                _retry_sleep(attempt); continue
            _mgrclient.report_usage({}, ms=int((time.monotonic() - _t0) * 1000), ok=False, err=m)
            on_token(m); return {"role": "assistant", "content": m}
        except Exception as e:
            m = f"⚠️ {_config.LLM_NAME} error: {e!r}"
            if _config.LLAMA_ENDPOINT and _conn_dropped(e):
                m = _llama_dropped_msg(messages)
            elif _retry_after(e, attempt):
                _retry_sleep(attempt); continue
            _mgrclient.report_usage({}, ms=int((time.monotonic() - _t0) * 1000), ok=False, err=m)
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
                _mgrclient.report_usage(chunk["usage"], ms=int((time.monotonic() - _t0) * 1000))
            try:
                delta = chunk["choices"][0]["delta"]
            except (KeyError, IndexError):
                continue
            # OpenRouter calls it "reasoning", llama.cpp (Qwen3 et al.) "reasoning_content"
            rzn = delta.get("reasoning") or delta.get("reasoning_content")
            if rzn:
                if not reasoning_open:
                    on_token(_config.THINK_START); reasoning_open = True
                reasoning_txt += rzn
                on_token(rzn)
            c = delta.get("content")
            if c:
                if reasoning_open:
                    on_token(_config.THINK_END); reasoning_open = False
                content += c
                on_token(c)
            _tcs = delta.get("tool_calls") or []
            if _tcs and reasoning_open:
                on_token(_config.THINK_END); reasoning_open = False
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
            on_token(_config.THINK_END)
    except Exception as e:
        # aborted mid-stream: keep what was already streamed, report the rest.
        m = f"⚠️ {_config.LLM_NAME} stream aborted: {e!r}"
        on_token(m)
        content += ("\n" + m)
    if _config.LLAMA_ENDPOINT and not content and not reasoning_txt and not tcs and not got_usage:
        # llama.cpp answered 200 and then died (an image did that): the stream
        # ends cleanly with nothing in it — not an empty reply, a dropped one.
        m = _llama_dropped_msg(messages)
        _mgrclient.report_usage({}, ms=int((time.monotonic() - _t0) * 1000), ok=False, err=m)
        on_token(m)
        content = m
    # Some reasoning models emit EVERYTHING as thinking and leave content empty
    # -> instead of an empty answer, keep the thinking (otherwise "_(empty reply)_").
    msg = {"role": "assistant", "content": content or reasoning_txt or None}
    if tcs:
        msg["tool_calls"] = [tcs[i] for i in sorted(tcs)]
    return msg
