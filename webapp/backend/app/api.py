"""FastAPI app: REST + SSE + static frontend. Binds 127.0.0.1 only."""
import asyncio
import datetime as dt
import json
import threading

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, ingest, timeline, projects, reviews
from .adapters import sessions as sessions_mod
from .timeutil import day_bounds, local_day_key, now_utc, parse_ts, tz

app = FastAPI(title="ai-work-journal dashboard", docs_url=None, redoc_url=None)

_STATE = {"revision": 0, "subscribers": [], "lock": threading.Lock()}


def bump_revision(affected_days=None):
    with _STATE["lock"]:
        _STATE["revision"] += 1
        rev = _STATE["revision"]
        subs = list(_STATE["subscribers"])
    msg = json.dumps({"revision": rev, "affected_days": affected_days or []})
    for q in subs:
        try:
            q.put_nowait(msg)
        except Exception:
            pass


def sync_all():
    r = ingest.run_all()
    bump_revision(r["affected_days"])
    return r


# ---------------- health / sync ----------------

@app.get("/api/v1/health")
def health():
    return {"ok": True, "db": str(config.DB_PATH),
            "llm_enabled": config.LLM_ENABLED}


@app.get("/api/v1/sync/status")
def sync_status():
    return ingest.status()


@app.post("/api/v1/sync")
def sync_now():
    return sync_all()


# ---------------- projection cache ----------------

import hashlib as _hl


def _day_fingerprint(day: str) -> str:
    """Change-detect: max event id + overrides touching the day."""
    mx = db.q1("SELECT COALESCE(MAX(id),0) AS m FROM source_events WHERE day_key=?", (day,))
    # events stored on the previous day can cross midnight into this day
    prev = db.q1("SELECT COALESCE(MAX(id),0) AS m FROM source_events WHERE day_key=?",
                 ((dt.date.fromisoformat(day) - dt.timedelta(days=1)).isoformat(),))
    ov = db.q1("SELECT COALESCE(MAX(id),0) AS m FROM manual_overrides "
               "WHERE day_key=? AND superseded_by IS NULL", (day,))["m"]
    return f"{mx['m']}|{prev['m']}|{ov}|v4"


def cached_timeline(day: str) -> dict:
    fp = _day_fingerprint(day)
    row = db.q1("SELECT metrics_json FROM daily_projections WHERE day_key=? AND "
                "input_fingerprint=?", (day, fp))
    if row:
        return json.loads(row["metrics_json"])
    t = timeline.day_timeline(day)
    for label in {b["project"] for b in t["blocks"]} | {a["project"] for a in t["agent"]}:
        projects.ensure_project(label)
    db.ex("DELETE FROM daily_projections WHERE day_key=?", (day,))
    db.ex("INSERT INTO daily_projections(day_key,projection_version,"
          "input_fingerprint,metrics_json,updated_at) VALUES(?,?,?,?,?)",
          (day, 1, fp, json.dumps(t, ensure_ascii=False), now_utc().isoformat()))
    return t


# ---------------- days ----------------

def _valid_day(day_key: str) -> str:
    try:
        dt.date.fromisoformat(day_key)
    except ValueError:
        raise HTTPException(400, "bad day_key")
    return day_key


@app.get("/api/v1/days")
def days():
    rows = db.q("SELECT DISTINCT day_key FROM source_events ORDER BY day_key")
    return {"days": [r["day_key"] for r in rows if r["day_key"]]}


@app.get("/api/v1/timeline")
def get_timeline(day: str, project: str = None):
    day = _valid_day(day)
    t = cached_timeline(day)
    if project:
        t["blocks"] = [b for b in t["blocks"] if b["project"] == project]
    t["revision"] = _STATE["revision"]
    return t


@app.get("/api/v1/today")
def get_today(day: str):
    """KPI strip + allocations + data quality for the Today page."""
    day = _valid_day(day)
    t = cached_timeline(day)
    alloc = {}
    for b in t["blocks"]:
        alloc[b["project"]] = alloc.get(b["project"], 0) + (
            parse_ts(b["end"]) - parse_ts(b["start"])).total_seconds()
    agent_exec_alloc = {}
    quality = {
        "has_observation": t["has_observation"],
        "unknown_sec": t["metrics"]["unknown_sec"],
        "observed_span_sec": t["metrics"]["observed_span_sec"],
        "events_indexed": db.q1("SELECT COUNT(*) AS n FROM source_events WHERE day_key=?",
                                (day,))["n"],
        "unlinked_sessions": db.q1(
            "SELECT COUNT(*) AS n FROM agent_sessions WHERE project_id IS NULL")["n"],
    }
    m = t["metrics"]
    kpis = [
        {"key": "human_active", "label": "人类活跃时长",
         "value": round(m["human_active_sec"] / 60), "unit": "min",
         "state": "measured" if m["human_active_sec"] else "none"},
        {"key": "agent_exec", "label": "Agent 执行时长",
         "value": round(m.get("agent_exec_sec", 0) / 60), "unit": "min",
         "note": f"生命周期总和 {round(m['agent_runtime_sec'] / 60)} min", "state": "measured"},
        {"key": "project_coverage", "label": "可归属项目比例",
         "value": None, "state": "n/a"},
        {"key": "unknown", "label": "未知区间",
         "value": round(m["unknown_sec"] / 60), "unit": "min",
         "state": "measured" if t["has_observation"] else "no_observation"},
    ]
    proj_sec = sum(v for k, v in alloc.items() if not projects.is_category(k))
    known_sec = sum(alloc.values())
    if known_sec:
        kpis[2]["value"] = round(proj_sec / known_sec, 3)
        kpis[2]["state"] = "measured"
    agent_exec_alloc = {}
    for a in t["agent"]:
        agent_exec_alloc[a["project"]] = agent_exec_alloc.get(a["project"], 0) + a["exec_sec"]
    commits = _day_commits(day)
    return {"day_key": day, "kpis": kpis, "allocation": alloc,
            "agent_allocation": agent_exec_alloc, "quality": quality,
            "metrics": m, "has_observation": t["has_observation"],
            "insights": _insights(day, t, alloc, commits),
            "outcomes": {"commits": commits,
                         "n_sessions": len(t["agent"]),
                         "n_user_inputs": sum(a["n_user"] for a in t["agent"])}}


def _day_commits(day: str) -> list:
    rows = db.q("SELECT occurred_at, payload FROM source_events WHERE day_key=? "
                "AND event_type='git_commit' ORDER BY occurred_at", (day,))
    out = []
    for r in rows:
        p = json.loads(r["payload"])
        at = parse_ts(r["occurred_at"])
        out.append({"at": at.astimezone(tz()).isoformat() if at else r["occurred_at"],
                    "repo": p.get("repo") or "",
                    "subject": str(p.get("subject") or "")[:100],
                    "sha": str(p.get("sha") or "")[:12]})
    return out


def _hm(sec) -> str:
    sec = int(sec)
    h, m = sec // 3600, sec % 3600 // 60
    return f"{h}h{m:02d}m" if h else f"{m}m"


def _insights(day: str, t: dict, alloc: dict, commits: list) -> list:
    """Rule-based daily insights, every number traceable to real events."""
    out = []
    m = t["metrics"]
    # 1. longest focus segment (same project, <=8min gap merged)
    segs = []
    for b in t["blocks"]:
        p = parse_ts(b["start"])
        q = parse_ts(b["end"])
        if segs and segs[-1][2] == b["project"] and p - segs[-1][1] <= dt.timedelta(minutes=8):
            segs[-1][1] = max(segs[-1][1], q)
            segs[-1][3] += 1
        else:
            segs.append([p, q, b["project"], 1])
    if segs:
        best = max(segs, key=lambda x: (x[1] - x[0]).total_seconds())
        dur = (best[1] - best[0]).total_seconds()
        if dur >= 15 * 60:
            out.append({"kind": "focus", "title": f"最长专注 {_hm(dur)} · {best[2]}",
                        "detail": f"{best[0].astimezone().strftime('%H:%M')}–"
                                   f"{best[1].astimezone().strftime('%H:%M')},"
                                   f"其间 {best[3]} 次窗口切换"})
    # 2. human vs agent-execution overlap
    if m["human_active_sec"] > 0 and m.get("agent_exec_sec", 0) > 0:
        hun = _ivs(t["blocks"])
        exn = _ivs([iv for a in t["agent"] for iv in a["segments"]])
        ov = _overlap_sec(hun, exn)
        ratio = ov / m["agent_exec_sec"]
        if ratio >= 0.6:
            out.append({"kind": "overlap", "title": f"Agent 执行与人在线重叠 {round(ratio * 100)}%",
                        "detail": f"执行总 {_hm(m['agent_exec_sec'])},其中 {_hm(ov)} "
                                   f"发生在你前台活跃时"})
        else:
            out.append({"kind": "overlap", "title": f"Agent 多数执行无人值守(重叠 {round(ratio * 100)}%)",
                        "detail": f"执行 {_hm(m['agent_exec_sec'])} 中仅 {_hm(ov)} 与前台活跃重叠,"
                                   f"其余为后台/自动运行"})
    # 3. overnight execution
    early_sec = 0
    for a in t["agent"]:
        for iv in a["segments"]:
            s0 = parse_ts(iv["start"]).astimezone()
            if s0.hour < 8:
                early_sec += (parse_ts(iv["end"]) - parse_ts(iv["start"])).total_seconds()
    if early_sec > 20 * 60:
        out.append({"kind": "night", "title": f"凌晨自动执行 {_hm(early_sec)}",
                    "detail": "08:00 前的 Agent 执行段,多为长任务/定时任务"})
    # 4. biggest unknown gap
    if t["unknown"]:
        big = max(t["unknown"], key=lambda iv: (parse_ts(iv["end"]) - parse_ts(iv["start"])).total_seconds())
        d = (parse_ts(big["end"]) - parse_ts(big["start"])).total_seconds()
        if d > 5 * 60:
            out.append({"kind": "unknown", "title": f"观察缺口 {_hm(d)}",
                        "detail": f"{parse_ts(big['start']).astimezone().strftime('%H:%M')}–"
                                   f"{parse_ts(big['end']).astimezone().strftime('%H:%M')} "
                                   f"无窗口数据,可在时间轴上标注"})
    # 5. projects
    top = sorted(alloc.items(), key=lambda x: -x[1])[:2]
    if top and sum(alloc.values()) > 0:
        total = sum(alloc.values())
        t0 = f"{top[0][0]} {_hm(top[0][1])}({round(top[0][1] / total * 100)}%)"
        t1 = f", {top[1][0]} {_hm(top[1][1])}({round(top[1][1] / total * 100)}%)" if len(top) > 1 else ""
        out.append({"kind": "projects", "title": f"投入首位 {top[0][0]}",
                    "detail": f"{t0}{t1}"})
    # 6. outcomes
    if commits:
        repos = {}
        for c in commits:
            repos[c["repo"]] = repos.get(c["repo"], 0) + 1
        rs = "、".join(f"{k}×{v}" for k, v in sorted(repos.items(), key=lambda x: -x[1]))
        out.append({"kind": "outcomes", "title": f"{len(commits)} 个提交",
                    "detail": rs})
    # 7. manual overrides active
    n_ov = db.q1("SELECT COUNT(*) AS n FROM manual_overrides WHERE day_key=? "
                 "AND field IN ('project','activity_type','end') AND superseded_by IS NULL",
                 (day,))["n"]
    if n_ov:
        out.append({"kind": "corrections", "title": f"{n_ov} 条人工修正生效中",
                    "detail": "重采集与重新分析不会覆盖;可在详情面板撤销"})
    return out[:7]


def _ivs(items):
    out = []
    for b in items:
        s, e = parse_ts(b["start"]), parse_ts(b["end"])
        if s and e and e > s:
            out.append((s, e))
    return _sort_union(out)


def _sort_union(intervals):
    out = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _overlap_sec(a, b) -> float:
    if not a or not b:
        return 0
    i = j = 0
    total = 0.0
    while i < len(a) and j < len(b):
        s = max(a[i][0], b[j][0])
        e = min(a[i][1], b[j][1])
        if e > s:
            total += (e - s).total_seconds()
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return total


# ---------------- evidence ----------------

@app.get("/api/v1/evidence/{event_id}")
def evidence(event_id: int):
    r = db.q1("SELECT * FROM source_events WHERE id=?", (event_id,))
    if not r:
        raise HTTPException(404, "no such event")
    p = json.loads(r["payload"])
    # redact obvious secrets in any text we hand out
    for k in ("text",):
        if isinstance(p.get(k), str):
            p[k] = _redact(p[k])
    for tr in p.get("tool_results") or []:
        if isinstance(tr.get("text"), str):
            tr["text"] = _redact(tr["text"])
    return {"id": r["id"], "source": r["source_key"], "type": r["event_type"],
            "occurred_at": r["occurred_at"], "payload": p}


SECRET_PATTERNS = ["sk-ant", "ghp_", "gho_", "sk_", "AKIA", "AIza", "xoxb-"]


def _redact(text: str) -> str:
    import re
    for prefix in SECRET_PATTERNS:
        text = re.sub(re.escape(prefix) + r"[A-Za-z0-9_\-]{8,}",
                      prefix + "***REDACTED***", text)
    return text


# ---------------- sessions ----------------

@app.get("/api/v1/sessions")
def list_sessions(day: str = None, project: str = None, agent: str = None,
                  query: str = None, limit: int = 50, cursor: int = 0):
    sql = ("SELECT s.id, s.source, s.cwd, s.start_at, s.end_at, s.status, "
           "s.project_id FROM agent_sessions s WHERE 1=1")
    args = []
    if day:
        start, end = day_bounds(day)
        sql += " AND s.start_at < ? AND COALESCE(s.end_at, s.start_at) >= ?"
        args += [end.isoformat(), start.isoformat()]
    if agent:
        sql += " AND s.source = ?"
        args.append(agent)
    if project:
        sql += " AND s.cwd LIKE ?"
        args.append(f"%{project}%")
    if query:
        sql += (" AND s.id IN (SELECT session_id FROM session_event_links l "
                "JOIN source_events e ON e.id = l.event_id "
                "WHERE e.payload LIKE ?)")
        args.append(f"%{query}%")
    sql += " ORDER BY s.start_at DESC LIMIT ? OFFSET ?"
    args += [limit, cursor]
    rows = db.q(sql, args)
    out = []
    for r in rows:
        label = projects.classify_for_session(r["cwd"])
        n_msgs = db.q1("SELECT COUNT(*) AS n FROM session_event_links WHERE session_id=?",
                       (r["id"],))["n"]
        out.append({"id": r["id"], "agent": r["source"], "cwd": r["cwd"],
                    "project": label, "start_at": r["start_at"], "end_at": r["end_at"],
                    "status": r["status"], "n_events": n_msgs})
    return {"sessions": out, "next_cursor": cursor + len(out) if len(out) == limit else None}


@app.get("/api/v1/sessions/{sid}")
def session_detail(sid: int):
    r = db.q1("SELECT * FROM agent_sessions WHERE id=?", (sid,))
    if not r:
        raise HTTPException(404, "no such session")
    evs = db.q("SELECT e.id, e.event_type, e.occurred_at, e.payload "
               "FROM session_event_links l JOIN source_events e ON e.id = l.event_id "
               "WHERE l.session_id = ? ORDER BY l.ordinal", (sid,))
    events = []
    for e in evs:
        p = json.loads(e["payload"])
        events.append({"id": e["id"], "type": e["event_type"], "at": e["occurred_at"],
                       "role": p.get("role"), "text": _redact((p.get("text") or "")[:600]),
                       "tools": [x.get("name") for x in (p.get("parts") or [])
                                 if x.get("kind") == "tool_call"],
                       "tool_results": p.get("tool_results") or [],
                       "usage": p.get("usage"), "model": p.get("model")})
    return {"id": r["id"], "agent": r["source"], "cwd": r["cwd"],
            "project": projects.classify_for_session(r["cwd"]),
            "start_at": r["start_at"], "end_at": r["end_at"], "status": r["status"],
            "events": events,
            "review": reviews.get_review(sid)}


# ---------------- corrections (append-only, never auto-overwritten) ----------------

class OverrideIn(BaseModel):
    day_key: str
    anchor_start: str   # ISO ts inside the block
    field: str          # project | activity_type | end
    value: str
    note: str = None


@app.post("/api/v1/overrides")
def add_override(o: OverrideIn):
    _valid_day(o.day_key)
    if o.field not in ("project", "activity_type", "end"):
        raise HTTPException(400, "field must be project|activity_type|end")
    t = timeline.day_timeline(o.day_key)
    target = None
    anchor = parse_ts(o.anchor_start)
    for b in t["blocks"]:
        if parse_ts(b["start"]) <= anchor < parse_ts(b["end"]):
            target = b
            break
    if target is None:
        raise HTTPException(404, "no block at anchor_start")
    from .timeutil import iso
    db.ex("INSERT INTO manual_overrides(target_type,day_key,anchor_start,field,value,"
          "original_value,note,created_at) VALUES(?,?,?,?,?,?,?,?)",
          ("activity_segment", o.day_key, iso(anchor), o.field, o.value,
           target["project"] if o.field == "project" else
           (target["activity_type"] if o.field == "activity_type" else target["end"]),
           o.note, __import__("datetime").datetime.now(dt.timezone.utc).isoformat()))
    bump_revision([o.day_key])
    return {"ok": True, "original": target.get("project" if o.field == "project"
                                               else "activity_type")}


@app.delete("/api/v1/overrides/{oid}")
def revoke_override(oid: int):
    row = db.q1("SELECT * FROM manual_overrides WHERE id=?", (oid,))
    if not row:
        raise HTTPException(404, "no such override")
    db.ex("INSERT INTO manual_overrides(target_type,day_key,anchor_start,field,value,"
          "original_value,note,created_at) VALUES(?,?,?,?,?,?,?,?)",
          ("activity_segment", row["day_key"], row["anchor_start"], "__revoke__",
           row["original_value"], row["original_value"],
           f"revoke #{oid}", now_utc().isoformat()))
    tomb_id = db.q1("SELECT last_insert_rowid() AS i")
    # pointer update is the only mutation of an override row (append-only spirit)
    db.ex("UPDATE manual_overrides SET superseded_by=? WHERE id=?",
          (tomb_id["i"], oid))
    bump_revision([row["day_key"]])
    return {"ok": True}


@app.get("/api/v1/overrides")
def list_overrides(day: str = None):
    if day:
        rows = db.q("SELECT * FROM manual_overrides WHERE day_key=? ORDER BY id", (day,))
    else:
        rows = db.q("SELECT * FROM manual_overrides ORDER BY id DESC LIMIT 200")
    return {"overrides": [dict(r) for r in rows]}


# ---------------- session review (LLM-gated) ----------------

@app.post("/api/v1/sessions/{sid}/review")
def session_review(sid: int):
    r = reviews.run_review(sid)
    if r.get("status") == "analyzed":
        bump_revision()
    return r


@app.post("/api/v1/reviews/feedback")
def review_feedback(body: dict):
    sid = body.get("session_id")
    if not isinstance(sid, int):
        raise HTTPException(400, "session_id required")
    rating = body.get("rating") or ""
    if rating not in ("helpful", "partially", "not_helpful", "wrong"):
        raise HTTPException(400, "bad rating")
    rev = db.q1("SELECT id FROM session_reviews WHERE session_id=? AND status='analyzed' "
                "ORDER BY id DESC", (sid,))
    if not rev:
        raise HTTPException(404, "no analyzed review for this session")
    db.ex("INSERT INTO review_feedback(review_id,category,note,created_at) "
          "VALUES(?,?,?,?)", (rev["id"], rating,
                              str(body.get("comment") or "")[:500],
                              now_utc().isoformat()))
    return {"ok": True}


# ---------------- projects ----------------

class ProjectIn(BaseModel):
    name: str
    priority: int = 3
    goals: str = None
    notes: str = None
    rules: list = None


@app.get("/api/v1/projects")
def list_projects():
    rows = db.q("SELECT * FROM projects ORDER BY priority, name")
    return {"projects": [dict(r) for r in rows]}


@app.post("/api/v1/projects")
def add_project(p: ProjectIn):
    from .timeutil import iso
    now = iso(now_utc())
    db.ex("INSERT INTO projects(name,priority,goals_json,rules_json,notes,created_at,"
          "updated_at) VALUES(?,?,?,?,?,?,?)",
          (p.name, p.priority, p.goals, json.dumps(p.rules) if p.rules else None,
           p.notes, now, now))
    return {"ok": True}


@app.patch("/api/v1/projects/{pid}")
def patch_project(pid: int, body: dict):
    row = db.q1("SELECT * FROM projects WHERE id=?", (pid,))
    if not row:
        raise HTTPException(404, "no such project")
    sets, args = [], []
    for k, v in body.items():
        if k in ("priority", "notes", "goals_json", "color", "name", "active"):
            sets.append(f"{k}=?")
            args.append(v)
    if not sets:
        raise HTTPException(400, "nothing to update")
    sets.append("updated_at=?")
    args.append(now_utc().isoformat())
    args.append(pid)
    db.ex(f"UPDATE projects SET {', '.join(sets)} WHERE id=?", args)
    return {"ok": True}


# ---------------- analytics ----------------

@app.get("/api/v1/analytics/time")
def analytics_time(start: str, end: str):
    """Daily human/agent totals + project stacks over a date range."""
    try:
        d0, d1 = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    except ValueError:
        raise HTTPException(400, "bad dates")
    out = []
    d = d0
    while d <= d1:
        t = cached_timeline(d.isoformat())
        m = t["metrics"]
        alloc = {}
        for b in t["blocks"]:
            alloc[b["project"]] = alloc.get(b["project"], 0) + (
                parse_ts(b["end"]) - parse_ts(b["start"])).total_seconds()
        out.append({"day": d.isoformat(), "human_sec": m["human_active_sec"],
                    "agent_sec": m["agent_runtime_sec"], "unknown_sec": m["unknown_sec"],
                    "has_observation": t["has_observation"],
                    "allocation": {k: round(v) for k, v in alloc.items()}})
        d += dt.timedelta(days=1)
    return {"days": out}


# ---------------- SSE ----------------

@app.get("/api/v1/events")
async def sse_events():
    q: asyncio.Queue = asyncio.Queue()

    def sub():
        with _STATE["lock"]:
            _STATE["subscribers"].append(q)

    sub()

    async def gen():
        try:
            yield f'data: {json.dumps({"revision": _STATE["revision"], "hello": True})}\n\n'
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=25)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"event: data_changed\ndata: {msg}\n\n"
        finally:
            with _STATE["lock"]:
                try:
                    _STATE["subscribers"].remove(q)
                except ValueError:
                    pass

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------------- static frontend ----------------

@app.get("/")
def index():
    p = config.FRONTEND_DIST / "index.html"
    if p.exists():
        return FileResponse(p)
    return JSONResponse({"error": {"code": "frontend_not_built",
                                   "message": "run: cd webapp/frontend && npm run build"}},
                        status_code=503)


if config.FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=str(config.FRONTEND_DIST)), name="static")
