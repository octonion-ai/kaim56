# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""The kAIm56 manager as a package, one concern per module (the map is in
manager.py's docstring). manager.py is the systemd entry point and the
composition root: it imports every module, wires the few injected
cross-references and starts the server.

Dependency direction: a module NEVER imports manager (no cycles). Siblings
are used as modules — ``from mgr import x as _x`` and ``_x.func()`` — never
as imported names, so a test can replace one definition in one place and
every caller sees it.
"""
