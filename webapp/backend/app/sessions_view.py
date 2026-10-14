"""Session summaries for the 会话 page: what a session was about and how it went.

    title       your first typed instruction (injected context and subagent turns excluded)
    exec_sec    events clustered with gaps <= 10 min (same rule as the timeline)
    prompts     instructions you typed; tools = tool calls made by the agent
    parent_id   a claude subagent transcript (<proj>/<uuid>/subagents/x.jsonl)
                belongs to the session <proj>/<uuid>.jsonl
"""
import datetime as dt
import json
from pathlib import Path

from . import db, projects
from .timeline import EXEC_GAP, _is_prompt
from .timeutil import parse_ts, tz

_CACHE = {}
_ACTS = {}   # id -> [(t, is_prompt, text)] for slicing a long session to a date range


def _local(iso):
    """Stored UTC → the data timezone, so the UI can slice HH:MM directly."""
    t = parse_ts(iso) if iso else None
    return t.astimezone(tz()).isoformat() if t else iso


def _readable(text: str) -> str:
    """Injected context as a title: '<file name="/a/b.md">' → '附件 b.md',
    '<command-name>/x</command-name>' → '命令 /x'; other tags stripped."""
    import re
    files = re.findall(r'<file name="([^"]+)"', text)
    if files:
        return "附件 " + "、".join(Path(f).name for f in files[:3])
    cmd = re.search(r"<command-name>([^<]+)</command-name>", text)
    if cmd:
        return "命令 " + cmd.group(1).strip()
    return re.sub(r"<[^>]+>", " ", text).strip()


def _parent_external_id(ext: str):
    p = Path(ext or "")
    if p.parent.name != "subagents":
        return None
    return str(p.parent.parent) + ".jsonl"


def summarize(r) -> dict:
    """r: agent_sessions row. Memoised on (id, end_at, link count)."""
    n_links = db.q1("SELECT COUNT(*) AS n FROM session_event_links WHERE session_id=?", (r["id"],))["n"]
    key = (r["id"], r["end_at"], n_links)
    if _CACHE.get(r["id"], (None,))[0] == key:
        return _CACHE[r["id"]][1]
    evs = db.q("SELECT e.event_type, e.occurred_at, e.payload FROM session_event_links l "
               "JOIN source_events e ON e.id = l.event_id WHERE l.session_id=? ORDER BY l.ordinal",
               (r["id"],))
    sub = "/subagents/" in (r["external_id"] or "").replace("\\", "/")
    title, first_any, prompts, tools, tokens, models = None, None, 0, 0, 0, set()
    ts, acts = [], []
    native_parent, native_kind, native_id = None, None, None
    for e in evs:
        t = parse_ts(e["occurred_at"])
        if t:
            ts.append(t)
        p = json.loads(e["payload"])
        if e["event_type"] == "session_meta":
            native_parent = p.get("native_parent_id") or native_parent
            native_kind = p.get("native_kind") or native_kind
            native_id = p.get("native_id") or native_id
            if native_kind == "child":
                sub = True
        is_prompt, text = False, None
        if e["event_type"] == "msg_user":
            text = (p.get("text") or "").strip()
            if text and first_any is None:
                first_any = text
            if not sub and _is_prompt(text):
                prompts += 1
                title = title or text
                is_prompt = True
        elif e["event_type"] == "msg_assistant":
            tools += sum(1 for x in (p.get("parts") or []) if x.get("kind") == "tool_call")
            u = p.get("usage") or {}
            tokens += sum(int(u.get(k) or 0) for k in ("input_tokens", "output_tokens", "input", "output"))
            if p.get("model"):
                models.add(p["model"])
        if t:
            acts.append((t, is_prompt, text if is_prompt else None))
    exec_sec = _exec(sorted(ts))
    label, why = projects.explain_path(r["cwd"])
    out = {
        "id": r["id"], "agent": r["source"], "cwd": r["cwd"], "project": label, "project_reason": why,
        "start_at": _local(r["start_at"]), "end_at": _local(r["end_at"]), "status": r["status"],
        "title": ((title or _readable(first_any or "")).replace("\n", " ")[:140]) or None,
        "subagent": sub, "parent_external_id": _parent_external_id(r["external_id"]) if sub else None,
        "native_id": native_id, "native_parent_id": native_parent, "native_kind": native_kind,
        "n_events": n_links, "prompts": prompts, "tools": tools, "tokens": tokens,
        "models": sorted(models)[:3], "exec_sec": round(exec_sec),
    }
    _CACHE[r["id"]] = (key, out)
    _ACTS[r["id"]] = sorted(acts, key=lambda a: a[0])
    return out


def _exec(ts) -> float:
    """Sum of activity clusters (gaps <= EXEC_GAP) over sorted timestamps."""
    total, c0, c1 = 0.0, None, None
    for t in ts:
        if c1 is not None and t - c1 <= EXEC_GAP:
            c1 = t
        else:
            if c0 is not None:
                total += (c1 - c0).total_seconds()
            c0 = c1 = t
    if c0 is not None:
        total += (c1 - c0).total_seconds()
    return total


def in_range(sid: int, start, end) -> dict:
    """What a (possibly days-long) session did inside [start, end): a session
    opened on 10-08 and still running is listed by today's activity, not its
    first prompt."""
    acts = [a for a in _ACTS.get(sid, []) if start <= a[0] < end]
    if not acts:
        return {"first_at": None, "last_at": None, "prompts": 0, "exec_sec": 0, "last_prompt": None}
    last_prompt = next((a[2] for a in reversed(acts) if a[1]), None)
    return {"first_at": acts[0][0].astimezone(tz()).isoformat(), "last_at": acts[-1][0].astimezone(tz()).isoformat(),
            "prompts": sum(1 for a in acts if a[1]), "exec_sec": round(_exec([a[0] for a in acts])),
            "last_prompt": _readable(last_prompt).replace("\n", " ")[:140] if last_prompt else None}


def list_sessions(start=None, end=None, agent=None, project=None, query=None,
                  include_subagents=False, limit=200):
    sql = "SELECT * FROM agent_sessions s WHERE 1=1"
    args = []
    if start and end:
        sql += " AND s.start_at < ? AND COALESCE(s.end_at, s.start_at) >= ?"
        args += [end.isoformat(), start.isoformat()]
    if agent:
        sql += " AND s.source = ?"
        args.append(agent)
    if query:
        sql += (" AND s.id IN (SELECT session_id FROM session_event_links l "
                "JOIN source_events e ON e.id = l.event_id WHERE e.payload LIKE ?)")
        args.append(f"%{query}%")
    sql += " ORDER BY s.start_at DESC LIMIT ?"
    args.append(limit * 4)
    rows = [summarize(r) for r in db.q(sql, args)]
    if project:
        rows = [r for r in rows if r["project"] == project]
    by_ext = {r["external_id"]: r["id"] for r in db.q("SELECT id, external_id FROM agent_sessions")}
    by_native = {(r["agent"], r.get("native_id")): r["id"] for r in rows if r.get("native_id")}
    kids = {}
    for r in rows:
        if r["subagent"]:
            r["parent_id"] = by_native.get((r["agent"], r.get("native_parent_id"))) if r.get("native_parent_id") else by_ext.get(r["parent_external_id"])
            kids.setdefault(r["parent_id"], []).append(r["id"])
    for r in rows:
        r["children"] = kids.get(r["id"], [])
    if not include_subagents:
        ids = {r["id"] for r in rows}
        # a subagent whose parent is not in this list stays visible on its own
        rows = [r for r in rows if not r["subagent"] or r.get("parent_id") not in ids]
    if start and end:
        rows = [{**r, "range": in_range(r["id"], start, end),
                 "carried": parse_ts(r["start_at"]) < start} for r in rows]
        # most recently active in the range first, not most recently opened
        rows.sort(key=lambda r: r["range"]["last_at"] or r["start_at"], reverse=True)
    return rows[:limit]


def day_range(day: str, days: int = 1):
    from .timeutil import day_bounds
    s, _ = day_bounds((dt.date.fromisoformat(day) - dt.timedelta(days=days - 1)).isoformat())
    _, e = day_bounds(day)
    return s, e
