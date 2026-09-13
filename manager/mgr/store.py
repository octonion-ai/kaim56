# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""store: all data storage in one place — SQLite (task history, LLM usage,
semantic vectors), the flat agent memory (memory.json) and the embedding
client. Part of the mgr package; only needs BASE.
"""
import json
import os
import re
import sqlite3
import threading
import time
import uuid
import urllib.request

HISTORY_DB = None
MEMORY_FILE = None
TASKS_FILE = None
_hist_lock = threading.Lock()
_mem_lock = threading.Lock()
EMBED_URL = "http://127.0.0.1:" + os.environ.get("EMBED_PORT", "8772")


def configure(base: str) -> None:
    global HISTORY_DB, MEMORY_FILE, TASKS_FILE
    HISTORY_DB = os.path.join(base, "history.db")
    MEMORY_FILE = os.path.join(base, "memory.json")
    TASKS_FILE = os.path.join(base, "tasks.json")


# ---- History (SQLite, stdlib): queryable long-term memory of tasks ----------
# Every executed task lands here; agents query it via recall_tasks
# ("have we done this already?" -> no duplicates, base knowledge).


def _hist_conn():
    c = sqlite3.connect(HISTORY_DB, timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS task_runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER, target TEXT, task TEXT, result TEXT, ok INTEGER,
        schedule TEXT, origin TEXT)""")
    # Usage per LLM call. The agents report it after every call to
    # /api/usage; the manager identifies the instance by source IP. cost is
    # what the provider bills for that call (OpenRouter reports it with
    # "usage":{"include":true}) — 0.0 if it reports nothing.
    c.execute("""CREATE TABLE IF NOT EXISTS llm_usage(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER, instance TEXT, model TEXT,
        prompt_tokens INTEGER, completion_tokens INTEGER, cost REAL)""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_usage_inst_ts ON llm_usage(instance, ts)")
    _migrate_spans(c)
    # Traces: one row per agent turn; its LLM calls are the llm_usage rows and
    # its tool calls the audit lines with the same turn id.
    c.execute("""CREATE TABLE IF NOT EXISTS turns(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        instance TEXT, turn TEXT, kind TEXT,
        ts_start INTEGER, ts_end INTEGER, ms INTEGER, steps INTEGER, outcome TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_turns_inst_ts ON turns(instance, ts_start)")
    # Semantic long-term memory: per memory a text + embedding vector
    # (as JSON). Search loads an instance's vectors and computes cosine in
    # memory — at a personal scale (hundreds) that is enough without
    # Vector DB. Vectors are normalised, cosine = dot product.
    c.execute("""CREATE TABLE IF NOT EXISTS semantic_memory(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER, instance TEXT, mkey TEXT, text TEXT, vec TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_sem_inst ON semantic_memory(instance)")
    return c


_SPAN_COLS = (("turn", "TEXT"), ("ms", "INTEGER"), ("step", "INTEGER"),
              ("ok", "INTEGER DEFAULT 1"), ("err", "TEXT"))
_migrated = [False]


def _migrate_spans(c):
    """llm_usage grew span columns (turn, ms, step, ok, err); an existing DB
    gets them added once. Checked once per process, not per connection."""
    if _migrated[0]:
        return
    have = {r[1] for r in c.execute("PRAGMA table_info(llm_usage)")}
    for col, typ in _SPAN_COLS:
        if col not in have:
            try:
                c.execute(f"ALTER TABLE llm_usage ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass
    _migrated[0] = True


def usage_add(instance, model, prompt_tokens, completion_tokens, cost,
              turn="", ms=None, step=None, ok=True, err=""):
    try:
        with _hist_lock, _hist_conn() as c:
            c.execute("INSERT INTO llm_usage(ts,instance,model,prompt_tokens,"
                      "completion_tokens,cost,turn,ms,step,ok,err) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (int(time.time()), str(instance)[:80], str(model)[:120],
                       int(prompt_tokens or 0), int(completion_tokens or 0),
                       float(cost or 0.0), str(turn or "")[:16],
                       None if ms is None else int(ms), None if step is None else int(step),
                       1 if ok else 0, str(err or "")[:400]))
        return "ok"
    except Exception as e:
        return f"error: {e!r}"


# ---- turns (traces) ---------------------------------------------------------
def turn_start(instance, turn, kind="chat", ts=None):
    try:
        with _hist_lock, _hist_conn() as c:
            c.execute("INSERT INTO turns(instance,turn,kind,ts_start) VALUES(?,?,?,?)",
                      (str(instance)[:80], str(turn)[:16], str(kind or "chat")[:16],
                       int(ts or time.time())))
        return "ok"
    except Exception as e:
        return f"error: {e!r}"


def turn_end(instance, turn, ms=None, steps=None, outcome="ok", kind="chat"):
    """Close the turn's row; a turn whose start was lost gets a full row."""
    try:
        now = int(time.time())
        with _hist_lock, _hist_conn() as c:
            cur = c.execute(
                "UPDATE turns SET ts_end=?, ms=?, steps=?, outcome=? WHERE id = "
                "(SELECT id FROM turns WHERE instance=? AND turn=? ORDER BY id DESC LIMIT 1)",
                (now, None if ms is None else int(ms), None if steps is None else int(steps),
                 str(outcome or "ok")[:16], str(instance)[:80], str(turn)[:16]))
            if cur.rowcount == 0:
                c.execute("INSERT INTO turns(instance,turn,kind,ts_start,ts_end,ms,steps,outcome) "
                          "VALUES(?,?,?,?,?,?,?,?)",
                          (str(instance)[:80], str(turn)[:16], str(kind or "chat")[:16],
                           now - int((ms or 0) / 1000), now, None if ms is None else int(ms),
                           None if steps is None else int(steps), str(outcome or "ok")[:16]))
        return "ok"
    except Exception as e:
        return f"error: {e!r}"


_TURN_COLS = ("id", "instance", "turn", "kind", "ts_start", "ts_end", "ms", "steps", "outcome")


def _turn_rows(c, where, params):
    rows = [dict(zip(_TURN_COLS, r)) for r in c.execute(
        "SELECT id,instance,turn,kind,ts_start,ts_end,ms,steps,outcome FROM turns "
        f"WHERE {where} ORDER BY ts_start DESC, id DESC", params)]
    for t in rows:                      # the LLM side of the turn, summed
        n, pt, ct, cost, bad = c.execute(
            "SELECT COUNT(*), SUM(prompt_tokens), SUM(completion_tokens), SUM(cost), "
            "SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) FROM llm_usage WHERE instance=? AND turn=?",
            (t["instance"], t["turn"])).fetchone()
        t.update({"llm_calls": n or 0, "in": pt or 0, "out": ct or 0,
                  "cost": round(cost or 0.0, 4), "llm_failed": bad or 0})
    return rows


def turns_read(instance, limit=50, since=0):
    try:
        with _hist_lock, _hist_conn() as c:
            return _turn_rows(c, "instance=? AND ts_start>=?", (instance, int(since or 0)))[:limit]
    except Exception:
        return []


def turn_trace(instance, turn):
    """{turn, llm} for one turn: the turn row (None if unknown) and its LLM
    spans in order. The tool spans come from the audit (manager side)."""
    try:
        with _hist_lock, _hist_conn() as c:
            rows = _turn_rows(c, "instance=? AND turn=?", (instance, turn))
            llm = [dict(zip(("ts", "model", "in", "out", "cost", "step", "ms", "ok", "err"), r))
                   for r in c.execute(
                       "SELECT ts, model, prompt_tokens, completion_tokens, cost, step, ms, ok, err "
                       "FROM llm_usage WHERE instance=? AND turn=? ORDER BY ts, id", (instance, turn))]
        for r in llm:
            r["ok"] = bool(r["ok"] is None or r["ok"])
        return {"turn": rows[0] if rows else None, "llm": llm}
    except Exception:
        return {"turn": None, "llm": []}


# ---- sessions: full-text search over chats and task runs (FTS5) ------------
# Exact search next to the semantic one: "where did we talk about the
# Vaillant job?" The index is rebuilt from chats.json whenever that file
# changed (it is small) and from task_runs in the same pass.
_fts_state = {"mtime": None}


def _fts_ok(c):
    try:
        c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5("
                  "instance UNINDEXED, kind UNINDEXED, ref UNINDEXED, ts UNINDEXED, title, text)")
        return True
    except sqlite3.OperationalError:
        return False


def sessions_refresh(chats, chats_mtime):
    """Rebuild the index when chats.json changed. `chats` is the loaded store
    (a list of conversations), `chats_mtime` its file mtime (any hashable)."""
    if _fts_state["mtime"] == chats_mtime:
        return False
    try:
        with _hist_lock, _hist_conn() as c:
            if not _fts_ok(c):
                return False
            c.execute("DELETE FROM sessions_fts")
            rows = []
            for conv in chats or []:
                if not isinstance(conv, dict):
                    continue
                cid, title = str(conv.get("id", ""))[:80], str(conv.get("title", ""))[:120]
                inst, ts = str(conv.get("instance", ""))[:80], int(conv.get("updatedAt", 0) or 0) // 1000
                for i, m in enumerate(conv.get("messages", []) or []):
                    txt = m.get("text") if isinstance(m, dict) else None
                    if isinstance(txt, str) and txt.strip():
                        rows.append((inst, "chat", f"{cid}#{i}", ts, title, txt[:8000]))
            for rid, ts, target, task, result in c.execute(
                    "SELECT id, ts, target, task, result FROM task_runs ORDER BY id DESC LIMIT 5000"):
                rows.append((target or "", "task", str(rid), int(ts or 0), (task or "")[:120],
                             f"{task or ''}\n{result or ''}"[:8000]))
            c.executemany("INSERT INTO sessions_fts(instance,kind,ref,ts,title,text) VALUES(?,?,?,?,?,?)", rows)
        _fts_state["mtime"] = chats_mtime
        return True
    except Exception as e:
        print("sessions_refresh:", repr(e), flush=True)
        return False


def _fts_query(q):
    """Every word becomes a quoted term (implicit AND): FTS5's own syntax
    (quotes, NEAR, columns) would otherwise turn user input into errors."""
    words = [w.replace('"', "") for w in str(q or "").split()]
    return " ".join(f'"{w}"' for w in words if w)


def sessions_query(query, instance=None, limit=10):
    """Hits: instance, kind (chat|task), ref, ts, title, snippet. Best first."""
    mq = _fts_query(query)
    if not mq:
        return []
    try:
        with _hist_lock, _hist_conn() as c:
            if not _fts_ok(c):
                return []
            sql = ("SELECT instance, kind, ref, ts, title, "
                   "snippet(sessions_fts, 5, '[', ']', ' … ', 28) FROM sessions_fts WHERE sessions_fts MATCH ?")
            args = [mq]
            if instance:
                sql += " AND instance = ?"
                args.append(str(instance))
            sql += " ORDER BY bm25(sessions_fts) LIMIT ?"
            args.append(int(limit))
            return [dict(zip(("instance", "kind", "ref", "ts", "title", "snippet"), r))
                    for r in c.execute(sql, args)]
    except Exception as e:
        print("sessions_query:", repr(e), flush=True)
        return []


def turns_prune(days=30):
    try:
        with _hist_lock, _hist_conn() as c:
            return c.execute("DELETE FROM turns WHERE ts_start < ?",
                             (int(time.time()) - days * 86400,)).rowcount
    except Exception:
        return 0


def usage_summary():
    """Usage per instance: today (local midnight) and total."""
    midnight = int(time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)))
    out = {}
    try:
        with _hist_lock, _hist_conn() as c:
            for since, key in ((0, "total"), (midnight, "today")):
                for inst, calls, pt, ct, cost in c.execute(
                        "SELECT instance, COUNT(*), SUM(prompt_tokens), "
                        "SUM(completion_tokens), SUM(cost) FROM llm_usage "
                        "WHERE ts >= ? GROUP BY instance", (since,)):
                    out.setdefault(inst, {})[key] = {
                        "calls": calls, "in": pt or 0, "out": ct or 0,
                        "cost": round(cost or 0.0, 4)}
    except Exception:
        return {}
    empty = {"calls": 0, "in": 0, "out": 0, "cost": 0.0}
    for v in out.values():
        v.setdefault("today", dict(empty))
        v.setdefault("total", dict(empty))
    return out


_EPHEMERAL_RE = re.compile(r"^task-[0-9a-f]{6}$")


def usage_by_model(since=0):
    """Tokens and cost per instance AND model since `since` (epoch), for the
    chart in the Resources tab. Ephemeral task VMs (task-xxxxxx) fold into
    one row "tasks", else a hundred one-shot names would drown the chart."""
    rows = {}
    try:
        with _hist_lock, _hist_conn() as c:
            for inst, model, calls, pt, ct, cost in c.execute(
                    "SELECT instance, model, COUNT(*), SUM(prompt_tokens), "
                    "SUM(completion_tokens), SUM(cost) FROM llm_usage "
                    "WHERE ts >= ? GROUP BY instance, model", (int(since or 0),)):
                name = "tasks" if _EPHEMERAL_RE.match(inst or "") else (inst or "?")
                r = rows.setdefault((name, model or "?"), {"instance": name, "model": model or "?",
                                                           "calls": 0, "in": 0, "out": 0, "cost": 0.0})
                r["calls"] += calls or 0; r["in"] += pt or 0; r["out"] += ct or 0; r["cost"] += cost or 0.0
    except Exception:
        return []
    out = sorted(rows.values(), key=lambda r: -(r["in"] + r["out"]))
    for r in out:
        r["cost"] = round(r["cost"], 4)
    return out


def usage_for(instance, since=0):
    """Usage of ONE instance since `since` (epoch). For the Activity panel:
    tokens in/out, cost and number of LLM calls in the chosen time window.
    Tokens accrue per LLM turn (not per tool call) — hence a sum, not an
    attribution to individual audit lines."""
    try:
        with _hist_lock, _hist_conn() as c:
            row = c.execute(
                "SELECT COUNT(*), SUM(prompt_tokens), SUM(completion_tokens), SUM(cost) "
                "FROM llm_usage WHERE instance=? AND ts>=?",
                (instance, int(since or 0))).fetchone()
        calls, pt, ct, cost = row or (0, 0, 0, 0)
        return {"calls": calls or 0, "in": pt or 0, "out": ct or 0,
                "cost": round(cost or 0.0, 4)}
    except Exception:
        return {"calls": 0, "in": 0, "out": 0, "cost": 0.0}


def history_add(target, task, result, ok, schedule="", origin=""):
    try:
        with _hist_lock, _hist_conn() as c:
            c.execute("INSERT INTO task_runs(ts,target,task,result,ok,schedule,origin) "
                      "VALUES(?,?,?,?,?,?,?)",
                      (int(time.time()), str(target)[:80], str(task)[:2000],
                       str(result)[:8000], 1 if ok else 0, str(schedule)[:40], str(origin)[:80]))
    except Exception as e:
        print("history_add:", repr(e), flush=True)


def history_search(q="", limit=20, instance=None):
    """Task runs, newest first. `instance` narrows to the runs that instance
    created (origin) or executed (target) — a guest sees only its own share of
    the history, not every other agent's results."""
    q = (q or "").strip()
    limit = max(1, min(int(limit or 20), 100))
    where, args = [], []
    if q:
        like = f"%{q}%"
        where.append("(task LIKE ? OR result LIKE ? OR target LIKE ?)")
        args += [like, like, like]
    if instance:
        where.append("(target = ? OR origin = ?)")
        args += [str(instance), str(instance)]
    sql = "SELECT ts,target,task,result,ok,schedule FROM task_runs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC LIMIT ?"
    try:
        with _hist_conn() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(sql, (*args, limit)).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        print("history_search:", repr(e), flush=True)
        return []
_tasks_lock = threading.Lock()


def load_tasks():
    try:
        with open(TASKS_FILE) as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return []


def save_tasks(tasks):
    with _tasks_lock:
        _save_tasks_locked(tasks)


def _save_tasks_locked(tasks):
    tmp = TASKS_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(tasks, fh, indent=2)
    os.replace(tmp, TASKS_FILE)


def with_tasks(mutator):
    """The ONLY safe way to change the task store: load → mutate → save as one
    critical section. `mutator(tasks)` edits the list in place and returns
    (dirty, result); saving happens only when dirty.

    Why: the lock used to guard just the WRITE. Worker thread and HTTP routes
    both did load→modify→save, and an interleaving lost updates — a task
    created between the worker's load and its save simply vanished."""
    with _tasks_lock:
        tasks = load_tasks()
        dirty, result = mutator(tasks)
        if dirty:
            _save_tasks_locked(tasks)
        return result


def add_task(instance, message, schedule="", model=""):
    schedule = (schedule or "").strip()
    t = {"id": uuid.uuid4().hex[:12], "instance": instance, "message": message,
         "schedule": schedule, "status": "scheduled" if schedule else "pending",
         "result": "", "created": int(time.time()), "updated": int(time.time()),
         "next_run": _next_run(schedule, int(time.time())) if schedule else int(time.time())}
    if (model or "").strip():
        t["model"] = str(model).strip()[:120]      # ephemeral target: VM created with it

    def mut(tasks):
        tasks.append(t)
        return True, t
    return with_tasks(mut)


def update_task(task_id, message=None, schedule=None, instance=None):
    """Change a task. A changed schedule is re-scheduled immediately —
    otherwise the task would run once more on the old plan. An empty plan
    turns the repetition into a one-off task (due now); a task that is
    currently running is left untouched. `instance` moves the task to another
    target (the caller validates the name)."""
    def mut(tasks):
        return _update_task_locked(tasks, task_id, message, schedule, instance)
    return with_tasks(mut)


def _update_task_locked(tasks, task_id, message, schedule, instance=None):
    t = next((x for x in tasks if x.get("id") == task_id), None)
    if t is None:
        return False, "unknown"
    if t.get("status") == "running":
        return False, "task is running — try again when it is done"
    now = int(time.time())
    if instance:
        t["instance"] = str(instance).strip()
        t.pop("target_warned", None)
    if message is not None and str(message).strip():
        t["message"] = str(message).strip()
    if schedule is not None:
        sched = str(schedule).strip()
        if sched != t.get("schedule", ""):
            t["schedule"] = sched
            t["next_run"] = _next_run(sched, now) if sched else now
            t["status"] = "scheduled" if sched else "pending"
    t["updated"] = now
    when = time.strftime("%d.%m. %H:%M", time.localtime(t["next_run"]))
    return True, f"task {task_id} updated (next run {when})"


def _next_run(schedule, from_ts):
    """Next run time (epoch) for a schedule spec.
    Formats: 'every 30m' | 'every 2h' | 'every 1d' | 'daily HH:MM' | 'hourly'."""
    s = (schedule or "").strip().lower()
    m = re.match(r"every\s+(\d+)\s*([mhd])", s)
    if m:
        mult = {"m": 60, "h": 3600, "d": 86400}[m.group(2)]
        return from_ts + int(m.group(1)) * mult
    m = re.match(r"daily\s+(\d{1,2}):(\d{2})", s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        lt = time.localtime(from_ts)
        cand = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
        if cand <= from_ts:
            cand += 86400
        return int(cand)
    if s == "hourly":
        return from_ts + 3600
    return from_ts + 3600



# ---- Semantisches Langzeitgedaechtnis --------------------------------------


def _embed(texts, kind):
    """Texts -> vectors via the embedding service. None if it is not
    reachable (the caller then falls back to the flat memory instead of
    failing)."""
    try:
        body = json.dumps({"texts": texts, "kind": kind}).encode()
        req = urllib.request.Request(EMBED_URL + "/embed", data=body,
                                     headers={"Content-Type": "application/json"})
        r = urllib.request.urlopen(req, timeout=30)
        return json.loads(r.read()).get("vectors")
    except Exception:
        return None


def sem_store(instance, text, key=""):
    """Embed and store a memory. The same (instance,key) is replaced rather
    than duplicated — so the agent updates existing entries."""
    text = (text or "").strip()
    if not instance or not text:
        return False
    # Embed and store the key too: if the value is terse ("Watzmann"),
    # "favourite_mountain: Watzmann" carries at least some context into vector AND
    # in den spaeter angezeigten Treffer.
    full = f"{key}: {text}" if key else text
    vecs = _embed([full], "passage")
    if not vecs:
        return False
    c = _hist_conn()
    with c:
        if key:
            c.execute("DELETE FROM semantic_memory WHERE instance=? AND mkey=?",
                      (instance, key))
        c.execute("INSERT INTO semantic_memory(ts,instance,mkey,text,vec) VALUES(?,?,?,?,?)",
                  (int(time.time()), instance, key or "", full, json.dumps(vecs[0])))
    c.close()
    return True




def sem_search(instance, query, k=5):
    """The k most semantically similar memories of an instance. Cosine in
    memory; the vectors are normalised, so the dot product suffices."""
    query = (query or "").strip()
    if not instance or not query:
        return []
    qv = _embed([query], "query")
    if not qv:
        return []
    q = qv[0]
    c = _hist_conn()
    rows = c.execute("SELECT text, vec FROM semantic_memory WHERE instance=?",
                     (instance,)).fetchall()
    c.close()
    scored = []
    for text, vec in rows:
        try:
            v = json.loads(vec)
            scored.append((sum(a * b for a, b in zip(q, v)), text))
        except (ValueError, TypeError):
            continue
    scored.sort(reverse=True)
    return [{"score": round(s, 3), "text": t} for s, t in scored[:max(1, int(k))]]



# ---- Agent memory (persistent, per instance) ------------------------------


def load_memory():
    try:
        with open(MEMORY_FILE) as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return {}


def mem_store(instance, key, value):
    if not instance or not key:
        return "instance/key missing"
    with _mem_lock:
        m = load_memory()
        # value None deletes the key — tests clean up after themselves, and an
        # agent can retract a note. An empty instance dict goes entirely.
        if value is None:
            m.get(instance, {}).pop(key, None)
            if not m.get(instance):
                m.pop(instance, None)
        else:
            m.setdefault(instance, {})[key] = value
        tmp = MEMORY_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(m, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, MEMORY_FILE)
    return f"deleted: {key}" if value is None else f"saved: {key}"


def mem_recall(instance, key=None):
    m = load_memory().get(instance, {})
    return m if key is None else m.get(key, "")


