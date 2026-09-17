# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""Where the manager keeps its files: the base directory and the paths derived from it.

Part of the mgr package: no import from manager.py. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import os

# The manager directory: mgr/ lives inside it, so this is location-independent
# for the live tree and the repo alike (manager.py derives the same value).
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BIN = os.path.join(BASE, "bin", "firecracker")
KERNEL = os.path.join(BASE, "bin", "vmlinux")
INST_DIR = os.path.join(BASE, "instances")
TEMPLATE_DIR = os.path.join(BASE, "templates")
RUN_DIR = os.path.join(BASE, "run")
SETTINGS_FILE = os.path.join(BASE, "settings.json")

# The operator's home: the manager tree ($HOME/firecracker) sits next to the
# operator's files, so the parent of BASE is the home; no user is named here.
HOME_DIR = os.path.dirname(BASE)
