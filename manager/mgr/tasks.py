# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""Background jobs: the task queue and its worker, ephemeral sandbox VMs, the orchestrator heartbeat and the mission advance.

Part of the mgr package: no import from manager.py. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import json
import os
import socket
import threading
import time
import urllib.request
import uuid

from mgr import chats as _chats
from mgr import guestchat as _guestchat
from mgr import guests as _guests
from mgr import instances as _instances
from mgr import memfs as _memfs
from mgr import missions as _missions
from mgr import notify as _notify
from mgr import settings as _settings
from mgr import signal as _signal_mod
from mgr import store as _store
from mgr import util as _util
from mgr import vm as _vm


TASK_TIMEOUT = int(os.environ.get("TASK_TIMEOUT", "1800"))    # worker-run tasks: 30 min


def _chat_post(inst, message, timeout=600):
    """Non-streaming chat call to an instance's bridge. The agent gets the
    deadline along and stops its tool loop in time — a run that outlives the
    caller answers into the void (a 12-step job search once did)."""
    url = f"http://{_instances.net_of(inst)['guest']}:{_instances.WEB_GUEST_PORT}/api/chat"
    data = json.dumps({"message": message, "deadline": time.time() + timeout - 30,
                       "kind": "task"}).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    body = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
    try:
        return json.loads(body).get("reply", body)
    except ValueError:
        return body


def _run_named(instance, message, timeout=600):
    """Run a task on an EXISTING instance (its tools/MCP/secrets live there).
    Starts it if needed and waits until the bridge is up."""
    inst = next((i for i in _instances.load_instances() if i["name"] == instance), None)
    if not inst:
        return (False, f"instance '{instance}' unknown")
    if not _instances.is_running(inst):
        if not _guestchat.wait_web(inst, timeout=120):
            return (False, f"instance '{instance}' not ready")
        inst = next((i for i in _instances.load_instances() if i["name"] == instance), None)
    try:
        return (True, _chat_post(inst, message, timeout=timeout))
    except (TimeoutError, socket.timeout):
        return (False, f"error: no answer within {timeout} s — the agent did not finish in time")
    except Exception as e:
        return (False, f"error: {e!r}")


EPHEMERAL_MAX = int(os.environ.get("EPHEMERAL_MAX", "2"))
_ephemeral_slots = threading.BoundedSemaphore(EPHEMERAL_MAX)


# A sandboxed run: an ephemeral VM with a NARROWER policy than its caller —
# fewer tools, an egress allowlist or no network at all, and optionally one
# skill baked into its system prompt. The cage is the Firecracker VM as
# always; what changes is what the agent inside may do. A caller can only
# narrow: no tool it does not hold itself, no host outside its own allowlist.
def _run_ephemeral(message, model=None, timeout=600, sandbox=None):
    """Run a task in a FRESH, isolated VM that is deleted afterwards — at most
    EPHEMERAL_MAX at a time: every one is a full VM (RAM, tap, disk), and any
    guest may ask for one, so the rest queue instead of exhausting the host."""
    if not _ephemeral_slots.acquire(timeout=600):
        return (False, f"ephemeral VM slots busy ({EPHEMERAL_MAX} at a time) — try again later")
    try:
        return _run_ephemeral_vm(message, model, timeout, sandbox)
    finally:
        _ephemeral_slots.release()


def _run_ephemeral_vm(message, model=None, timeout=600, sandbox=None):
    name = "task-" + uuid.uuid4().hex[:6]
    cfg = {"TRANSPORT": "web", "NO_SPAWN": "1"}
    internet = True
    if sandbox:                                   # {"cfg": {...}, "internet": bool} from sandbox_config
        cfg.update(sandbox.get("cfg") or {})
        internet = bool(sandbox.get("internet", True))
    if model:
        cfg["OPENROUTER_MODEL"] = model           # an explicit model wins over a persona's model
        print(f"[ephemeral] {name}: sandbox tools={cfg.get('AGENT_TOOLS') or 'all'} "
              f"egress={cfg.get('EGRESS_ALLOW') or ('none' if not internet else 'any')}"
              f"{' skill' if 'AGENT_SYSTEM' in cfg else ''}", flush=True)
    msg = _instances.create_instance(name, "openrouter", cfg, internet=internet)
    inst = next((i for i in _instances.load_instances() if i["name"] == name), None)
    if not inst:
        return (False, f"ephemeral VM failed: {msg}")
    try:
        if not _guestchat.wait_web(inst, timeout=120):
            return (False, "ephemeral VM not ready")
        return (True, _chat_post(inst, message, timeout=timeout))
    except Exception as e:
        return (False, f"error: {e!r}")
    finally:
        try:
            _vm.stop(inst)
            _instances.delete_instance(name)
        except Exception as e:
            # a leaked ephemeral VM keeps its tap, its disk and its RAM
            print(f"[quiet] ephemeral cleanup of {name} failed: {e!r}", flush=True)


def resolve_task_target(target):
    """('name', '') or ('', error). Strips a leading '@' — an agent once wrote
    '@orchestrator' and the task then failed every morning for days with
    'instance unknown' while nobody was told — and refuses unknown names at
    creation time instead of at 08:00 the next day."""
    t = (target or "").strip().lstrip("@").strip() or "ephemeral"
    if t == "ephemeral" or any(i.get("name") == t for i in _instances.load_instances()):
        return t, ""
    return "", f"instance '{t}' unknown (targets: ephemeral or an existing instance name)"


def unknown_target_tasks(tasks, names):
    """Scheduled/pending tasks whose instance does not exist (and is not
    'ephemeral') and that were not reported yet — the ones that would fail at
    their next run without anyone hearing about it."""
    out = []
    for t in tasks:
        inst = str(t.get("instance") or "")
        if t.get("status") in ("scheduled", "pending") and inst != "ephemeral" \
                and inst not in names and not t.get("target_warned"):
            out.append(t)
    return out


def task_target_sweep():
    """Hourly (idle worker): push once per task with a dead target, then mark
    it so the push does not repeat. Editing the task clears the mark."""
    names = {i.get("name") for i in _instances.load_instances()}
    hit = []

    def mut(tasks):
        for t in unknown_target_tasks(tasks, names):
            t["target_warned"] = int(time.time())
            hit.append((t.get("id"), t.get("instance"), str(t.get("message", ""))[:120]))
        return bool(hit), None
    _store.with_tasks(mut)
    for tid, inst, msg in hit:
        try:
            _notify.notify_add("task", f"Task target unknown: {tid}",
                       f"instance '{inst}' does not exist — {msg}", link="tasks")
        except Exception as e:
            _util._wlog(f"{tid}: target-sweep notify: {e!r}")
    return hit


def _run_task_now(instance, message, model=None, timeout=600, sandbox=None):
    """Run a task — on a named instance (routing to the capability) or in an
    ephemeral VM (target == 'ephemeral'). `model` applies to the ephemeral VM
    only — a named instance keeps its own configuration. `timeout` is the
    caller's patience; the worker allows TASK_TIMEOUT, a waiting guest 600 s."""
    if instance == "ephemeral":
        if sandbox:
            return _run_ephemeral(message, (model or "").strip()[:120] or None, timeout, sandbox)
        return _run_ephemeral(message, (model or "").strip()[:120] or None, timeout)
    return _run_named(instance, message, timeout)


# ---- Instant trigger for the orchestrator ----------------------------------
# New user message (Signal/app/web) -> the orchestrator runs debounced within
# seconds instead of only at the next 2-h heartbeat. Coalesces bursts, one run
# at a time; if new messages arrived during the run, it fires again right away.
# Fires only if the inbox really has something new (peek).
ORCH_HEARTBEAT_MSG = (
    "/fresh "   # stateless: own throwaway context, no bloat, no wiping out a
                # running app chat (shared _history).
    "Heartbeat (instant trigger): 1) read_inbox — new user messages. "
    "2) For each one that needs action: recall_tasks (no duplicates), then "
    "list_agents and create_task to the CAPABLE instance (e.g. hass for "
    "HomeAssistant) or ephemeral. 3) Messages starting with [Signal] came in "
    "via Signal: send the reply or confirmation back with send_signal "
    "(briefly) — WITHOUT specifying a number/recipient, it goes to the user "
    "automatically; do NOT invent a number. 4) Check missions: is a step stuck "
    "on doing even though its task finished long ago (recall_tasks)? Then "
    "mission_update and kick off the next step. Keep it short. Nothing to do? "
    "Report: nothing to do.")
MISSION_ADVANCE_MSG = (
    "/fresh Mission progress (instant trigger after task completion): the "
    "following tasks are done:\n{done}\n"
    "For EACH of them: 1) recall_tasks for the result. 2) mission_update: set "
    "the step to done/failed, record the result briefly. 3) Kick off the NEXT "
    "open step (create_task to the capable instance or ephemeral, note the "
    "task-id on the step via mission_update). 4) No open step left? "
    "mission_finish with a short summary. Blocked? notify the user. Keep it short.")


# Collect mode for the advance push (idea from OpenClaw's queue modes): every
# push is a full /fresh turn and costs its fixed ~5k input tokens before any
# work happens. When several tasks finish close together — exactly what the
# cross-instance missions produce — one push handling all of them does the same
# work for one fixed cost. Completions are therefore collected per OWNER for a
# short window and flushed as a single message.
MISSION_COLLECT_SECS = float(os.environ.get("MISSION_COLLECT_SECS", "8"))
_madv_lock = threading.Lock()
_madv_pending = {}        # owner -> [ "task 'id' (mission 'mid', goal, step N)" ]
_madv_timer = {}          # owner -> threading.Timer


def _mission_advance_flush(inst):
    with _madv_lock:
        _madv_timer.pop(inst, None)
        lines = _madv_pending.pop(inst, [])
    if not lines:
        return
    if not any(i.get("name") == inst for i in _instances.load_instances()):
        return          # owner deleted -> nothing to push to (TTL sweep pauses it)
    msg = MISSION_ADVANCE_MSG.format(done="\n".join("- " + x for x in lines))
    try:
        _run_named(inst, msg)
    except Exception as e:
        print("mission-advance:", repr(e), flush=True)


def _mission_advance_fire(task_id):
    """After task completion: if the task belongs to a mission step, note it for
    the mission's OWNER and (re)arm that owner's collect window. The owner is
    whichever agent planned the mission — the step itself may have run on a
    completely different instance."""
    inst, m, st = _missions.mission_for_task(task_id)
    if not m or not inst:
        return
    line = f"task '{task_id}' (mission '{m['id']}', {m['goal'][:80]}, step {st['n']})"
    with _madv_lock:
        _madv_pending.setdefault(inst, []).append(line)
        t = _madv_timer.get(inst)
        if t:
            t.cancel()
        t = threading.Timer(MISSION_COLLECT_SECS, _mission_advance_flush, args=(inst,))
        t.daemon = True
        _madv_timer[inst] = t
        t.start()


_orch_lock = threading.Lock()
_orch_timer = [None]
_orch_running = [False]
_orch_dirty = [False]


def orchestrator_ping():
    if not any(i.get("name") == _guests.ORCH_INSTANCE for i in _instances.load_instances()):
        return
    try:
        if not _chats.inbox_since(peek=True):   # only fire if there is really something new
            return
    except Exception:
        return
    with _orch_lock:
        if _orch_timer[0]:
            _orch_timer[0].cancel()
        t = threading.Timer(8.0, _orch_fire)
        t.daemon = True
        _orch_timer[0] = t
        t.start()


# Supply the signal module with its cross-references (all now defined).
_signal_mod.load_settings = _settings.load_settings
_signal_mod.chat_log_append = _chats.chat_log_append
_signal_mod.orchestrator_ping = orchestrator_ping

def _orch_fire():
    with _orch_lock:
        if _orch_running[0]:
            _orch_dirty[0] = True
            return
        _orch_running[0] = True
    try:
        _run_named(_guests.ORCH_INSTANCE, ORCH_HEARTBEAT_MSG)
    except Exception as e:
        print("orch-trigger:", repr(e), flush=True)
    finally:
        with _orch_lock:
            _orch_running[0] = False
            rerun = _orch_dirty[0]
            _orch_dirty[0] = False
        if rerun:
            orchestrator_ping()


_mi_sweep_ts = [0.0]


def reclaim_stuck_tasks():
    """Reset orphaned 'running' tasks at startup. Exactly ONE worker runs — what
    is still 'running' at startup belongs to a crashed run (e.g. the store bug
    on Aug 20) and would otherwise never fire again."""
    def mut(tasks):
        n = 0
        for t in tasks:
            if t.get("status") == "running":
                t["status"] = "scheduled" if t.get("schedule") else "pending"
                n += 1
        return bool(n), n
    n = _store.with_tasks(mut)
    if n:
        print(f"[worker] {n} orphaned 'running' task(s) reset", flush=True)


def worker_claim(tasks, now, hb_idle, skipped_hb, throttled):
    """One claim pass over the task store (runs INSIDE with_tasks): skip idle
    heartbeats, throttle loops, claim the first runnable task. Module-level so
    the dirty contract is testable — the bug this guards against: a skip
    mutates next_run, and returning dirty=False threw that mutation away, so
    the same heartbeat was re-skipped every 5 s (9,961 log lines)."""
    def due(t):
        if t.get("status") == "running":
            return False
        if t.get("schedule"):
            return t.get("next_run", 0) <= now
        return t.get("status") == "pending"

    for t in tasks:
        if not due(t):
            continue
        if hb_idle(t):
            t["next_run"] = _store._next_run(t["schedule"], now)
            t["result"] = "skipped: inbox empty, no active mission"
            t["updated"] = now
            skipped_hb.append(t["id"])
            continue
        # Frequency cap: more than 6 runs/h of the same task is ALWAYS a
        # defect (loop bug Aug 20) — pause it for an hour.
        runs = [x for x in t.get("recent_runs", []) if now - x < 3600]
        if len(runs) >= 6:
            t["recent_runs"] = runs
            t["next_run"] = now + 3600
            throttled.append((t["id"], str(t.get("message", ""))[:120]))
            continue
        t["recent_runs"] = runs + [now]
        t["status"] = "running"
        t["updated"] = now
        return True, dict(t)
    # Skips und Drosselungen VERAENDERN Tasks (next_run!) — ohne dirty=True
    # verfiele das Weiterplanen beim naechsten Zyklus.
    return bool(throttled) or bool(skipped_hb), None


def _task_worker():
    """Processes due/pending tasks sequentially in the background."""
    reclaim_stuck_tasks()
    while True:
        ran = False
        try:
            now = int(time.time())

            # The CLAIM happens on a fresh load inside the store lock
            # (worker_claim above) — this closes the lost-update window
            # between worker and HTTP threads.
            throttled = []

            def heartbeat_idle(t):
                """The 30-min heartbeat is a full /fresh turn whose usual
                outcome is "nothing to do": ~1.560 of the orchestrator's 1.774
                audit entries in 14 days were its idle ritual. When the inbox
                holds nothing new AND no mission is active, the manager can
                answer that question itself — without waking the model. Costs
                are no argument anymore (hy3 is ~free), but the audit noise
                drowns the saddler and the free tier is a cluster risk."""
                # Only the SCHEDULED heartbeat: a one-off with the same text
                # (mission advance, manual poke) must run — and skipping a
                # pending one-off would just re-skip it every worker cycle.
                if not t.get("schedule") \
                        or not str(t.get("message", "")).startswith("/fresh Heartbeat"):
                    return False
                try:
                    if _chats.inbox_since(peek=True):
                        return False
                    if any(m.get("status") == "active"
                           for lst in _missions.load_missions().values() for m in lst):
                        return False
                except Exception:
                    return False          # in doubt: run it
                return True

            skipped_hb = []
            t = _store.with_tasks(lambda ts: worker_claim(ts, now, heartbeat_idle,
                                                   skipped_hb, throttled))
            for tid in skipped_hb:
                _util._wlog(f"{tid}: heartbeat skipped (idle — no inbox, no mission)")
            for tid, tmsg in throttled:
                _util._wlog(f"{tid}: >6 runs/h — paused for 1 h (loop protection)")
                try:
                    _notify.notify_add("guardrail", f"Task loop throttled: {tid}",
                               tmsg + " — ran >6x/h, paused 1 h.", link="tasks")
                except Exception:
                    pass
            if t is not None:
                sched = bool(t.get("schedule"))
                # From here on EVERYTHING is guarded individually: an error
                # anywhere must never leave the task as a "running" orphan
                # (bug Aug 20: exception in the follow-up -> outer except ->
                # the task never fired again and the chat entry was missing).
                try:
                    ok, res = _run_task_now(t["instance"], t["message"], t.get("model"), timeout=TASK_TIMEOUT,
                                            sandbox=t.get("sandbox"))
                except Exception as e:
                    ok, res = False, f"worker-exception (run): {e!r}"
                    _util._wlog(f"{t['id']}: {res}")
                def done_mut(fresh, _tid=t["id"], _ok=ok, _res=res, _sched=sched):
                    tt = next((x for x in fresh if x["id"] == _tid), None)
                    if tt is None:
                        return False, None
                    tt["updated"] = int(time.time())
                    tt["result"] = _res
                    if _sched:
                        tt["status"] = "scheduled"
                        tt["next_run"] = _store._next_run(tt["schedule"], int(time.time()))
                    else:
                        tt["status"] = "done" if _ok else "error"
                    return True, None
                try:
                    _store.with_tasks(done_mut)
                except Exception as e:
                    _util._wlog(f"{t['id']}: status update failed: {e!r}")
                try:
                    _chats.chat_log_append(t.get("instance", "task"), "task",
                                    t.get("message", ""), res, kind="task")
                except Exception as e:
                    _util._wlog(f"{t['id']}: chat_log_append: {e!r}")
                try:
                    _store.history_add(t.get("instance", ""), t.get("message", ""), res, ok,
                                t.get("schedule", ""), origin="worker")
                except Exception as e:
                    _util._wlog(f"{t['id']}: history_add: {e!r}")
                try:
                    _mission_advance_fire(t["id"])
                except Exception as e:
                    _util._wlog(f"{t['id']}: mission-advance: {e!r}")
                # A scheduled task that fails would otherwise fail again
                # tomorrow, silently — the result only sits in the Tasks tab.
                # One push per DISTINCT failure text (not one per day).
                if sched and not ok and res != t.get("result"):
                    try:
                        _notify.notify_add(t.get("instance") or "task",
                                   f"Scheduled task failed: {t['id']}",
                                   (str(t.get("message", ""))[:120] + " — " + str(res))[:900],
                                   link="tasks")
                    except Exception as e:
                        _util._wlog(f"{t['id']}: failure notify: {e!r}")
                ran = True
        except Exception as e:
            _util._wlog(f"worker-loop: {e!r}")
        if not ran:
            time.sleep(5)
            # Orphan watch: if a task hangs on "running" for more than 30 min,
            # its run is lost (the timeout is 10 min) -> reset it.
            try:
                cut = int(time.time()) - 1800

                def orphan_mut(tasks2):
                    hit = []
                    for t2 in tasks2:
                        if t2.get("status") == "running" and t2.get("updated", 0) < cut:
                            t2["status"] = "scheduled" if t2.get("schedule") else "pending"
                            hit.append(t2.get("id"))
                    return bool(hit), hit
                for tid in _store.with_tasks(orphan_mut):
                    _util._wlog(f"{tid}: running orphan reset")
            except Exception as e:
                _util._wlog(f"orphan-watch: {e!r}")
            # TTL sweep while idle, at most once per hour.
            now = time.time()
            if now - _mi_sweep_ts[0] > 3600:
                _mi_sweep_ts[0] = now
                try:
                    _missions.mission_ttl_sweep()
                except Exception as e:
                    _util._wlog(f"mission-ttl-sweep failed: {e!r}")
                try:
                    task_target_sweep()
                except Exception as e:
                    _util._wlog(f"task-target-sweep failed: {e!r}")
                try:
                    _memfs.sweep([i["name"] for i in _instances.load_instances() if _vm.uses_harness(i)])
                except Exception as e:
                    _util._wlog(f"memfs-sweep failed: {e!r}")
                try:
                    _store.turns_prune(30)          # traces older than the weekly digest's reach
                except Exception as e:
                    _util._wlog(f"turns-prune failed: {e!r}")
            try:
                _vm.image_sweep()          # one stat per base image, every idle cycle
            except Exception as e:
                _util._wlog(f"image-sweep failed: {e!r}")



def run_task_now(tid):
    """Queue a task for the worker's next tick: a scheduled one keeps its
    schedule (only this run is pulled forward), a one-off is set pending
    again, whatever its last outcome. A running one is left alone."""
    out = {"msg": "unknown"}

    def mut(tasks):
        t = next((x for x in tasks if x["id"] == tid), None)
        if t is None:
            return False, None
        if t.get("status") == "running":
            out["msg"] = f"task {tid} is running already"
            return False, None
        if t.get("schedule"):
            t["next_run"] = int(time.time())
        else:
            t["status"] = "pending"
        t.pop("target_warned", None)
        out["msg"] = f"task {tid} queued — runs within a minute"
        return True, None
    _store.with_tasks(mut)
    return out["msg"]
