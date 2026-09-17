#!/usr/bin/env python3
# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""The openrouter agent: a tool-calling loop against an OpenAI-compatible
backend, running inside the microVM. Stdlib only. Transports: signal | web
(run_agent.py). One concern per module:

  config         environment, backend selection (OpenRouter / OrcaRouter /
                 llama.cpp), limits, the system prompt, /model /steps /reasoning
  mgrclient      the manager client: base URL, GET/POST, the LLM key or the
                 key proxy, the LLM URL and headers, the usage report
  observe        audit lines per tool call and the trace of a turn
  tools_local    tools inside the VM: shell, files, office, fetch, pdf, search
  tools_manager  tools that go through the manager: sub-agents, tasks,
                 missions, oracle, Home Assistant, notify, Signal, inbox,
                 skills, memory, playbooks, secrets, katfs
  tools          the tool table (BUILTIN), allowlist, schema, execution with
                 audit, denylist and HITL, plugins, init()
  mcp            MCP stdio servers and the manager's MCP hub as tools
  offload        large tool outputs to disk, previews, offload_read
  llm            chat completion and streaming, retries and timeouts
  context        history, per-turn injections, summarization, branches
  learn          skill proposals from successful turns
  loop           run / run_stream, the tool loop, steering, the goal loop

run_agent.py addresses the modules directly (agent.loop.run, agent.tools.init,
agent.config.OR_MODEL, …). Inside the package a sibling is used as
``_name.func`` (``from . import name as _name``), never as an imported name,
so a test can replace one definition in one place. The tests reach every
module as ``agent._name``; that is why they are all imported here.
"""
from . import config as _config              # noqa: F401
from . import context as _context            # noqa: F401
from . import learn as _learn                # noqa: F401
from . import llm as _llm                    # noqa: F401
from . import loop as _loop                  # noqa: F401
from . import mcp as _mcp                    # noqa: F401
from . import mgrclient as _mgrclient        # noqa: F401
from . import observe as _observe            # noqa: F401
from . import offload as _offload            # noqa: F401
from . import tools as _tools                # noqa: F401
from . import tools_local as _tools_local    # noqa: F401
from . import tools_manager as _tools_manager  # noqa: F401
