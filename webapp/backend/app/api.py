"""FastAPI app: REST + SSE + static frontend. Binds 127.0.0.1 only."""
import asyncio
import datetime as dt
import json
import threading
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import analytics, capture, chat, config, i18n, ocr, reports, db, ingest, llm_classify, timeline, projects, reviews
from .adapters import sessions as sessions_mod
from .i18n import label, tr
from .timeutil import day_bounds, local_day_key, now_utc, parse_ts, tz

app = FastAPI(title="ai-work-journal dashboard", docs_url=None, redoc_url=None)


@app.middleware("http")
async def _lang(request: Request, call_next):
    """Generated text follows the dashboard's 中/EN toggle (the `lang` cookie)."""
    i18n.set_lang(request.cookies.get("lang") or "zh")
    return await call_next(request)

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _origin_is_local(origin: str) -> bool:
    from urllib.parse import urlsplit
    try:
        parsed = urlsplit(origin)
        return parsed.scheme in {"http", "https"} and parsed.hostname in {"127.0.0.1", "localhost", "::1"} and parsed.port == config.PORT
    except ValueError:
        return False


@app.middleware("http")
async def _local_origin_guard(request: Request, call_next):
    """Any web page can make a browser send a state-changing request to
    127.0.0.1. Refuse those: mutating API calls must come from the dashboard
    itself. A request without an Origin (curl, the CLI, the scheduler) is not
    a browser and is left alone."""
    origin = request.headers.get("origin")
    if origin and request.method not in _SAFE_METHODS and request.url.path.startswith("/api/"):
        if not _origin_is_local(origin):
            return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
    return await call_next(request)


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


def _relabel_done():
    projects.invalidate()
    bump_revision(_all_days())


def sync_all():
    r = ingest.run_all()
    bump_revision(r["affected_days"])
    if r.get("total_new"):
        llm_classify.start_background(on_done=_relabel_done)   # label any new titles
    return r


AUTOSYNC_SEC = 300


def _autosync_loop():
    import os
    import time
    while True:
        time.sleep(AUTOSYNC_SEC)
        if os.environ.get("JOURNAL_AUTOSYNC") == "0":
            continue
        try:
            sync_all()
        except Exception:
            pass   # a source being down must not kill the loop; next round retries


@app.on_event("startup")
def _llm_on_startup():
    llm_classify.start_background(on_done=_relabel_done)
    # keep the dashboard (and 问 AI) current without pressing 同步数据
    threading.Thread(target=_autosync_loop, name="autosync", daemon=True).start()
    capture.start()   # built-in screen sampler, if enabled in webapp/config.json


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
    return f"{mx['m']}|{prev['m']}|{ov}|{projects.rules_version()}|v11"


def cached_timeline(day: str) -> dict:
    fp = _day_fingerprint(day)
    row = db.q1("SELECT metrics_json FROM daily_projections WHERE day_key=? AND "
                "input_fingerprint=?", (day, fp))
    if row:
        return json.loads(row["metrics_json"])
    t = timeline.day_timeline(day)
    for label in {b["project"] for b in t["blocks"]} | {a["project"] for a in t["agent"]}:
        projects.ensure_project(label)
    # concurrent cold requests (/timeline + /today) may both compute: last write wins
    db.ex("DELETE FROM daily_projections WHERE day_key=? AND input_fingerprint<>?", (day, fp))
    db.ex("INSERT OR REPLACE INTO daily_projections(day_key,projection_version,"
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
        {"key": "human_active", "label": tr("人类活跃时长", "Human active"),
         "value": round(m["human_active_sec"] / 60), "unit": "min",
         "state": "measured" if m["human_active_sec"] else "none"},
        {"key": "agent_exec", "label": tr("Agent 执行时长", "Agent execution"),
         "value": round(m.get("agent_exec_sec", 0) / 60), "unit": "min",
         "note": tr("生命周期总和", "lifetime total") + f" {round(m['agent_runtime_sec'] / 60)} min", "state": "measured"},
        {"key": "project_coverage", "label": tr("可归属项目比例", "Attributed to projects"),
         "value": None, "state": "n/a"},
        {"key": "unknown", "label": tr("未知区间", "Unknown time"),
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
    from .adapters import git as git_mod
    rows = db.q("SELECT occurred_at, payload FROM source_events WHERE day_key=? "
                "AND event_type='git_commit' ORDER BY occurred_at", (day,))
    paths = {json.loads(r["payload"]).get("path") for r in rows}
    out = []
    for r in rows:
        p = json.loads(r["payload"])
        if not git_mod.is_mine(p, paths):
            continue
        at = parse_ts(r["occurred_at"])
        out.append({"at": at.astimezone(tz()).isoformat() if at else r["occurred_at"],
                    "repo": p.get("repo") or "",
                    "project": projects.classify_for_session(p.get("path") or p.get("repo") or ""),
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
            out.append({"kind": "focus", "title": tr(f"最长专注 {_hm(dur)} · {best[2]}", f"Longest focus {_hm(dur)} · {label(best[2])}"),
                        "detail": f"{best[0].astimezone().strftime('%H:%M')}–"
                                   f"{best[1].astimezone().strftime('%H:%M')}"
                                   + tr(f",其间 {best[3]} 次窗口切换", f", {best[3]} window switch" + ("es" if best[3] != 1 else ""))})
    # 2. human vs agent-execution overlap
    if m["human_active_sec"] > 0 and m.get("agent_exec_sec", 0) > 0:
        hun = _ivs(t["blocks"])
        exn = _ivs([iv for a in t["agent"] for iv in a["segments"]])
        ov = _overlap_sec(hun, exn)
        ratio = ov / m["agent_exec_sec"]
        if ratio >= 0.6:
            out.append({"kind": "overlap", "title": tr(f"Agent 执行与人在线重叠 {round(ratio * 100)}%", f"Agents ran while you were active {round(ratio * 100)}% of the time"),
                        "detail": tr(f"执行总 {_hm(m['agent_exec_sec'])},其中 {_hm(ov)} 发生在你前台活跃时",
                                     f"{_hm(ov)} of {_hm(m['agent_exec_sec'])} execution overlapped your activity")})
        else:
            out.append({"kind": "overlap", "title": tr(f"Agent 多数执行无人值守(重叠 {round(ratio * 100)}%)", f"Agents mostly ran unattended ({round(ratio * 100)}% overlap)"),
                        "detail": tr(f"执行 {_hm(m['agent_exec_sec'])} 中仅 {_hm(ov)} 与前台活跃重叠,其余为后台/自动运行",
                                     f"Only {_hm(ov)} of {_hm(m['agent_exec_sec'])} overlapped your activity; the rest ran in the background")})
    # 3. overnight execution
    early_sec = 0
    for a in t["agent"]:
        for iv in a["segments"]:
            s0 = parse_ts(iv["start"]).astimezone()
            if s0.hour < 8:
                early_sec += (parse_ts(iv["end"]) - parse_ts(iv["start"])).total_seconds()
    if early_sec > 20 * 60:
        out.append({"kind": "night", "title": tr(f"凌晨自动执行 {_hm(early_sec)}", f"{_hm(early_sec)} of overnight execution"),
                    "detail": tr("08:00 前的 Agent 执行段,多为长任务/定时任务", "Agent runs before 08:00, mostly long or scheduled jobs")})
    # 4. biggest unknown gap
    if t["unknown"]:
        big = max(t["unknown"], key=lambda iv: (parse_ts(iv["end"]) - parse_ts(iv["start"])).total_seconds())
        d = (parse_ts(big["end"]) - parse_ts(big["start"])).total_seconds()
        if d > 5 * 60:
            out.append({"kind": "unknown", "title": tr(f"观察缺口 {_hm(d)}", f"Observation gap {_hm(d)}"),
                        "detail": f"{parse_ts(big['start']).astimezone().strftime('%H:%M')}–"
                                   f"{parse_ts(big['end']).astimezone().strftime('%H:%M')} "
                                   + tr("无窗口数据,可在时间轴上标注", "no window data; you can label it on the timeline")})
    # 5. projects
    top = sorted(alloc.items(), key=lambda x: -x[1])[:2]
    if top and sum(alloc.values()) > 0:
        total = sum(alloc.values())
        t0 = f"{label(top[0][0])} {_hm(top[0][1])} ({round(top[0][1] / total * 100)}%)"
        t1 = tr(",", ", ") + f"{label(top[1][0])} {_hm(top[1][1])} ({round(top[1][1] / total * 100)}%)" if len(top) > 1 else ""
        out.append({"kind": "projects", "title": tr(f"投入首位 {top[0][0]}", f"Most time: {label(top[0][0])}"),
                    "detail": f"{t0}{t1}"})
    # 6. outcomes
    if commits:
        repos = {}
        for c in commits:
            repos[c["repo"]] = repos.get(c["repo"], 0) + 1
        rs = tr("、", ", ").join(f"{k}×{v}" for k, v in sorted(repos.items(), key=lambda x: -x[1]))
        out.append({"kind": "outcomes", "title": tr(f"{len(commits)} 个提交", f"{len(commits)} commit" + ("s" if len(commits) != 1 else "")),
                    "detail": rs})
    # 7. manual overrides active
    n_ov = db.q1("SELECT COUNT(*) AS n FROM manual_overrides WHERE day_key=? "
                 "AND field IN ('project','activity_type','end') AND superseded_by IS NULL",
                 (day,))["n"]
    if n_ov:
        out.append({"kind": "corrections", "title": tr(f"{n_ov} 条人工修正生效中", f"{n_ov} manual correction" + ("s" if n_ov != 1 else "") + " in effect"),
                    "detail": tr("重采集与重新分析不会覆盖;可在详情面板撤销", "Kept across re-sync and re-analysis; undo in the detail panel")})
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
def list_sessions(day: str = None, days: int = 1, project: str = None, agent: str = None,
                  query: str = None, subagents: bool = False, limit: int = 200):
    """Enriched session list (title, exec time, prompts, tools, tokens); claude
    subagents are folded under their parent unless subagents=true."""
    from . import sessions_view
    start = end = None
    if day:
        start, end = sessions_view.day_range(_valid_day(day), max(1, min(days, 90)))
    rows = sessions_view.list_sessions(start, end, agent, project, query, subagents, min(limit, 500))
    return {"sessions": rows, "next_cursor": None}


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
        at = parse_ts(e["occurred_at"])
        events.append({"id": e["id"], "type": e["event_type"],
                       "at": at.astimezone(tz()).isoformat() if at else e["occurred_at"],
                       "role": p.get("role"), "text": _redact((p.get("text") or "")[:2000]),
                       "tools": [x.get("name") for x in (p.get("parts") or [])
                                 if x.get("kind") == "tool_call"],
                       "tool_results": p.get("tool_results") or [],
                       "usage": p.get("usage"), "model": p.get("model")})
    from . import sessions_view
    return {**sessions_view.summarize(r), "events": events,
            "review": reviews.get_review(sid), "review_available": reviews.available()}


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
    rules: dict = None


def _all_days() -> list:
    return [r["day_key"] for r in db.q("SELECT DISTINCT day_key FROM source_events WHERE day_key IS NOT NULL")]


@app.get("/api/v1/projects")
def list_projects():
    projects.compiled()  # discover + seed before listing
    dirs = projects.discovered_dirs()
    recent = recent_project_seconds()
    rows = db.q("SELECT * FROM projects ORDER BY active DESC, priority, name")
    return {"projects": [{**dict(r), "rules": projects.parse_rules(r["rules_json"]),
                          "discovered": r["name"] in dirs, "recent_sec": round(recent.get(r["name"], 0))}
                         for r in rows],
            "categories": sorted(projects.CATEGORY_LABELS)}


@app.post("/api/v1/projects")
def add_project(p: ProjectIn):
    from .timeutil import iso
    now = iso(now_utc())
    if db.q1("SELECT id FROM projects WHERE name=?", (p.name,)):
        raise HTTPException(409, "project exists")
    db.ex("INSERT INTO projects(name,priority,goals_json,rules_json,notes,created_at,"
          "updated_at) VALUES(?,?,?,?,?,?,?)",
          (p.name, p.priority, p.goals,
           json.dumps(projects.parse_rules(p.rules or {}), ensure_ascii=False), p.notes, now, now))
    projects.invalidate()
    bump_revision(_all_days())
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
        elif k == "rules":
            rules = projects.parse_rules(v)
            for pat in rules["patterns"]:
                try:
                    __import__("re").compile(pat)
                except Exception as e:
                    raise HTTPException(400, f"bad pattern {pat!r}: {e}")
            rules["seeded"] = True  # user-owned from now on
            sets.append("rules_json=?")
            args.append(json.dumps(rules, ensure_ascii=False))
    if not sets:
        raise HTTPException(400, "nothing to update")
    sets.append("updated_at=?")
    args.append(now_utc().isoformat())
    args.append(pid)
    db.ex(f"UPDATE projects SET {', '.join(sets)} WHERE id=?", args)
    projects.invalidate()
    bump_revision(_all_days())   # every day re-projects with the new rules
    llm_classify.start_background(on_done=_relabel_done)   # new project set → re-ask
    return {"ok": True}


@app.get("/api/v1/classify/llm")
def llm_status():
    return llm_classify.status()


@app.post("/api/v1/classify/llm/run")
def llm_run():
    started = llm_classify.start_background(on_done=_relabel_done)
    return {**llm_classify.status(), "started": started}


def _recent_windows(days: int):
    since = (now_utc() - dt.timedelta(days=days)).isoformat()
    for r in db.q("SELECT occurred_at, payload FROM source_events WHERE event_type='aw_window' "
                  "AND occurred_at >= ?", (since,)):
        p = json.loads(r["payload"])
        d = p.get("data") or {}
        dur = float(p.get("duration") or 0)
        if dur > 0:
            yield d.get("app") or "", d.get("title") or "", dur


def _clean(title: str) -> str:
    return __import__("re").sub(r" - (Google Chrome|Zotero|Mozilla Firefox|Visual Studio Code|Text Editor)$", "",
                                title or "").strip()


@app.get("/api/v1/classify/unclassified")
def unclassified(days: int = 14, limit: int = 40):
    """Windows that landed in a category (no project), grouped by title, by time."""
    groups = {}
    for app_, title, dur in _recent_windows(days):
        label, reason = projects.explain(app_, title)
        # deliberate categories (keyword rules) and titles with nothing to match on are not "unclassified"
        if not projects.is_category(label) or reason.startswith("关键词") or len(_clean(title).strip()) < 3:
            continue
        key = (app_.lower(), _clean(title))
        g = groups.setdefault(key, {"app": app_, "title": _clean(title), "category": label,
                                    "reason": reason, "sec": 0.0, "n": 0})
        g["sec"] += dur
        g["n"] += 1
    rows = sorted(groups.values(), key=lambda g: -g["sec"])[:limit]
    return {"days": days, "windows": [{**g, "sec": round(g["sec"])} for g in rows]}


@app.get("/api/v1/classify/explain")
def classify_explain(title: str = "", app: str = "google-chrome", path: str = ""):
    """Try the current rules on a window title (or a working directory)."""
    label, reason = projects.explain_path(path) if path else projects.explain(app, title)
    return {"label": label, "reason": reason, "is_category": projects.is_category(label)}


class PreviewIn(BaseModel):
    project: str
    kind: str          # alias | pattern | path
    value: str
    days: int = 14


@app.post("/api/v1/classify/preview")
def preview_rule(p: PreviewIn):
    """What a new rule would re-file over the last N days, before saving it."""
    import re as _re
    if p.kind not in ("alias", "pattern", "path") or not p.value.strip():
        raise HTTPException(400, "kind must be alias|pattern|path with a value")
    if p.kind == "pattern":
        try:
            _re.compile(p.value)
        except _re.error as e:
            raise HTTPException(400, f"bad pattern: {e}")
    trial = projects.compiled().with_rule(p.project, p.kind, p.value.strip())
    moved, sec, n, samples = {}, 0.0, 0, {}
    for app_, title, dur in _recent_windows(p.days):
        before = projects.explain(app_, title)[0]
        after = projects.explain(app_, title, trial)[0]
        if after == p.project and before != p.project:
            moved[before] = moved.get(before, 0) + dur
            sec += dur
            n += 1
            t = _clean(title)
            samples[t] = samples.get(t, 0) + dur
    top = sorted(samples.items(), key=lambda kv: -kv[1])[:8]
    return {"windows": n, "sec": round(sec),
            "from": {k: round(v) for k, v in sorted(moved.items(), key=lambda kv: -kv[1])},
            "samples": [{"title": t, "sec": round(s)} for t, s in top]}


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


_DAY_STATS = {}


def _day_stats(day: str) -> dict:
    """analytics.day_stats for one day, memoised per projection fingerprint."""
    fp = _day_fingerprint(day)
    hit = _DAY_STATS.get(day)
    if hit and hit[0] == fp:
        return hit[1]
    st = analytics.day_stats(cached_timeline(day), day_bounds(day)[0])
    _DAY_STATS[day] = (fp, st)
    return st


def _days(d0: dt.date, d1: dt.date) -> list:
    out, d = [], d0
    while d <= d1:
        out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


@app.get("/api/v1/analytics/overview")
def analytics_overview(start: str, end: str):
    """Per-day stats, range summary vs the previous equal-length period,
    per-project you-vs-agent table, focus bins and computed insights."""
    try:
        d0, d1 = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    except ValueError:
        raise HTTPException(400, "bad dates")
    if d1 < d0 or (d1 - d0).days > 120:
        raise HTTPException(400, "range must be 1–121 days")
    known = {r["day_key"] for r in db.q("SELECT DISTINCT day_key FROM source_events")}
    days = [_day_stats(d) for d in _days(d0, d1)]
    n = (d1 - d0).days + 1
    prev_days = [_day_stats(d) for d in _days(d0 - dt.timedelta(days=n), d0 - dt.timedelta(days=1)) if d in known]
    cur, prev = analytics.summarize(days), analytics.summarize(prev_days) if prev_days else None
    pri = {r["name"]: r["priority"] for r in db.q("SELECT name, priority FROM projects WHERE active=1")}
    table = analytics.project_table(days, pri)
    return {"days": days, "summary": cur, "previous": prev,
            "projects": table,
            "focus_bins": [label for _, _, label in analytics.FOCUS_BINS],
            "insights": analytics.insights(days, cur, prev, table)}


@app.get("/api/v1/projects/stats")
def project_stats(days: int = 30):
    """Per project over the last `days` calendar days: your time, agent time,
    sessions, commits, last active day, and a daily series of your time."""
    days = max(7, min(days, 120))
    today = dt.date.today()
    keys = [(today - dt.timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    known = {r["day_key"] for r in db.q("SELECT DISTINCT day_key FROM source_events")}
    out = {}
    for i, k in enumerate(keys):
        if k not in known:
            continue
        st = _day_stats(k)
        names = set(st["allocation"]) | set(st["agent_allocation"]) | set(st["sessions_by_project"]) | set(st["commits_by_project"])
        for p in names:
            o = out.setdefault(p, {"human_sec": 0, "agent_sec": 0, "sessions": 0, "commits": 0,
                                   "last_active": None, "series": [0] * days})
            h = st["allocation"].get(p, 0)
            o["human_sec"] += h
            o["agent_sec"] += st["agent_allocation"].get(p, 0)
            o["sessions"] += st["sessions_by_project"].get(p, 0)
            o["commits"] += st["commits_by_project"].get(p, 0)
            o["series"][i] = round(h)
            if h or st["agent_allocation"].get(p):
                o["last_active"] = k
    return {"days": keys, "projects": out}


def recent_project_seconds(n: int = 30) -> dict:
    """Your time per project over the last n days with data (for colour slots)."""
    rows = db.q("SELECT DISTINCT day_key FROM source_events WHERE day_key IS NOT NULL "
                "ORDER BY day_key DESC LIMIT ?", (n,))
    tot = {}
    for r in rows:
        for k, v in _day_stats(r["day_key"])["allocation"].items():
            tot[k] = tot.get(k, 0) + v
    return tot


# ---------------- reports (daily / weekly, Feishu links, schedule) ----------------

@app.get("/api/v1/reports")
def reports_list():
    return {**reports.list_reports(), "schedule": reports.get_schedule(), "timers": reports.timer_status()}


@app.get("/api/v1/reports/status")
def reports_status():
    return {"schedule": reports.get_schedule(), "timers": reports.timer_status()}


@app.put("/api/v1/reports/schedule")
def reports_schedule(body: dict):
    return {"schedule": reports.set_schedule(body)}


@app.post("/api/v1/reports/run/{kind}")
def reports_run(kind: str):
    try:
        return reports.run_now(kind)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/v1/reports/{kind}/{name}")
def reports_read(kind: str, name: str):
    try:
        return reports.read_report(kind, name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except OSError:
        raise HTTPException(404, "no such report")


# ---------------- SSE ----------------

# ---------------- 问 AI (tool-using chat over all journal data) ----------------

class ChatIn(BaseModel):
    text: str
    thread_id: Optional[int] = None    # null = start a new thread
    context: Optional[dict] = None


@app.get("/api/v1/chat/status")
def chat_status():
    return {"available": chat.available(), "model": chat.model(),
            "tools": [t["function"]["name"] for t in chat.TOOLS]}


@app.get("/api/v1/chat/threads")
def chat_threads():
    return {"threads": chat.list_threads()}


@app.get("/api/v1/chat/threads/{tid}")
def chat_thread(tid: int):
    v = chat.thread_view(tid)
    if not v:
        raise HTTPException(404, "no such thread")
    return v


@app.delete("/api/v1/chat/threads/{tid}")
def chat_thread_delete(tid: int):
    chat.delete_thread(tid)
    return {"ok": True}


@app.post("/api/v1/chat")
def chat_ask(body: ChatIn):
    """NDJSON stream: thread · step/step_done (tool calls) · delta (answer text) · done | error."""
    if not chat.available():
        raise HTTPException(503, tr("LLM 未配置", "LLM not configured"))

    lang = i18n.lang()   # the stream runs outside this request's context

    def gen():
        for ev in chat.ask(body.text, body.thread_id, body.context, lang=lang):
            yield json.dumps(ev, ensure_ascii=False) + "\n"
    return StreamingResponse(gen(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/api/v1/screen/status")
def screen_status(day: str = None):
    """Is screen capture (screenpipe or the built-in sampler) alive? Plus that
    day's frame count and the OCR engine in use."""
    st = dict(chat.screen_status())
    st.setdefault("ocr", ocr.describe())
    if day and st["state"] != "off":
        st["day_frames"] = chat.screen_frames(_valid_day(day)) if st["state"] != "down" else 0
    return st


@app.post("/api/v1/screen/restart")
def screen_restart():
    """Restart the screenpipe service so the screen-share approval dialog shows again."""
    r = chat.restart_screen()
    if not r["ok"]:
        raise HTTPException(409, r["error"])
    return r


@app.get("/api/v1/screen/capture/{cid}")
def screen_capture_image(cid: int):
    """A screenshot taken by the built-in sampler."""
    path = capture.image_file(cid)
    if not path:
        raise HTTPException(404, "no such capture")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


@app.post("/api/v1/screen/capture")
def screen_capture_now():
    """Take one round of screenshots now (built-in sampler)."""
    ok, why = capture.support()
    if not ok:
        raise HTTPException(409, why)
    return capture.capture_once()


@app.get("/api/v1/screen/frame")
def screen_frame(f: str, i: Optional[int] = None):
    """A screenpipe screenshot, only from inside ~/.screenpipe/data
    (i = frame index inside a compacted mp4)."""
    path = chat.frame_file(f, i)
    if not path:
        raise HTTPException(404, "no such frame")
    return FileResponse(path, media_type="image/jpeg" if path.suffix != ".png" else "image/png",
                        headers={"Cache-Control": "max-age=86400"})


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
