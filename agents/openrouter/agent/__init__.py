#!/usr/bin/env python3
# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""OpenRouter agent with tool-calling — model-agnostic, runs inside the microVM.

Tools: bash, read_file, write_file, list_dir, http_fetch  + optional MCP servers
(stdio), fetched from the manager at runtime. Transports: signal | web (via TRANSPORT). Stdlib only.
"""

# ---- package modules ----
from . import loop as _loop  # noqa: E402,F401  (tests reach it as m._x)
from . import tools as _tools  # noqa: E402,F401  (tests reach it as m._x)
from . import context as _context  # noqa: E402,F401  (tests reach it as m._x)
from . import tools_manager as _tools_manager  # noqa: E402,F401  (tests reach it as m._x)
from . import offload as _offload  # noqa: E402,F401  (tests reach it as m._x)
from . import learn as _learn  # noqa: E402,F401  (tests reach it as m._x)
from . import llm as _llm  # noqa: E402,F401  (tests reach it as m._x)
from . import mcp as _mcp  # noqa: E402,F401  (tests reach it as m._x)
from . import tools_local as _tools_local  # noqa: E402,F401  (tests reach it as m._x)
from . import observe as _observe  # noqa: E402,F401  (tests reach it as m._x)
from . import mgrclient as _mgrclient  # noqa: E402,F401  (tests reach it as m._x)
from . import config as _config  # noqa: E402,F401  (tests reach it as m._x)

# ---- end of agent ----
