"""Day timeline projection: human / agent / evidence / unknown lanes.

Facts: AW window events (foreground) minus AW afk intervals (user away) =
human active segments, clipped to the local day. Merged into blocks by
(project label, activity type). Unknown = gaps inside the observed span.
Manual overrides patch labels/ranges afterwards and always win.

Pure function of (events, overrides) — deterministic, no hidden state.
"""
import datetime as dt
import hashlib
import json

from . import db, projects
from .timeutil import day_bounds, local_day_key, parse_ts, tz

MERGE_GAP = dt.timedelta(seconds=90)
ACTIVITY_BY_APP = {
    "com.mitchellh.ghostty": "terminal",
    "org.gnome.ptyxis": "terminal",
    "jetbrains-pycharm": "coding",
    "code": "coding",
    "zotero": "reading",
    "google-chrome": "browser",
    "firefox": "browser",
    "org.gnome.texteditor": "writing",
    "org.gnome.nautilus": "files",
    "warp-taskbar": "system",
    "slack": "communication",
    "lark": "communication",
    "feishu": "communication",
}


def _event_rows(day_key: str):
    """AW events overlapping the local day (including ones started the day before)."""
    start, end = day_bounds(day_key)
    rows = db.q("SELECT id, event_type, occurred_at, payload FROM source_events "
                "WHERE event_type IN ('aw_window','aw_afk') "
                "AND occurred_at < ? AND occurred_at > ?",
                (end.isoformat(), (start - dt.timedelta(hours=24)).isoformat()))
    out = []
    for r in rows:
        p = json.loads(r["payload"])
        ts = parse_ts(r["occurred_at"])
        dur = float(p.get("duration") or 0)
        if ts is None or dur <= 0:
            continue
        if ts >= end or ts + dt.timedelta(seconds=dur) <= start:
            continue  # no overlap with this day
        out.append({"id": r["id"], "type": r["event_type"], "ts": ts,
                    "dur": dur, "data": p.get("data") or {}})
    return out


def _clip(iv, lo, hi):
    s, e = max(iv[0], lo), min(iv[1], hi)
    return (s, e) if e > s else None


def _union(intervals):
    out = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _subtract(intervals, cuts):
    """intervals minus cuts; both lists of (start,end) sorted."""
    out = []
    for s, e in intervals:
        cur = s
        for cs, ce in cuts:
            if ce <= cur or cs >= e:
                continue
            if cs > cur:
                out.append((cur, cs))
            cur = max(cur, ce)
            if cur >= e:
                break
        if cur < e:
            out.append((cur, e))
    return out


def _activity_type(app: str, title: str) -> str:
    a = app.lower()
    if a in ACTIVITY_BY_APP:
        return ACTIVITY_BY_APP[a]
    if "terminal" in a or "ghostty" in a or "ptyxis" in a:
        return "terminal"
    if "chrome" in a or "firefox" in a or "browser" in a:
        return "browser"
    if "zotero" in a:
        return "reading"
    return "other"


def _active_segments(day_key: str):
    """Window events clipped to (day minus afk) → [(start,end,app,title)].
    Also returns away intervals and the window-event observed span."""
    start, end = day_bounds(day_key)
    evs = _event_rows(day_key)
    afk_union = _union([(e["ts"], e["ts"] + dt.timedelta(seconds=e["dur"]))
                        for e in evs if e["type"] == "aw_afk"
                        and (e["data"].get("status") == "afk")])
    afk = [iv for iv in (_clip(x, start, end) for x in afk_union) if iv]
    segs = []
    for e in evs:
        if e["type"] != "aw_window":
            continue
        iv = _clip((e["ts"], e["ts"] + dt.timedelta(seconds=e["dur"])), start, end)
        if not iv:
            continue
        kept = _subtract([iv], afk)
        for s, t in kept:
            app = (e["data"].get("app") or "").strip()
            title = (e["data"].get("title") or "").strip()
            segs.append({"start": s, "end": t, "app": app, "title": title})
    segs.sort(key=lambda x: x["start"])
    # observed span: first..last window event touching the day
    win = [e for e in evs if e["type"] == "aw_window"]
    obs = None
    if win:
        obs = (max(start, min(e["ts"] for e in win)),
               min(end, max(e["ts"] + dt.timedelta(seconds=e["dur"]) for e in win)))
    return segs, evs, afk, obs, start, end


def _merge_blocks(segs):
    """Merge contiguous segments with same (project, activity)."""
    for s in segs:
        s["project"] = projects.classify(s["app"], s["title"])
        s["activity"] = _activity_type(s["app"], s["title"])
        s["conf"] = 0.75 if s["title"] else 0.45
    blocks = []
    for s in segs:
        if (blocks and blocks[-1]["project"] == s["project"]
                and blocks[-1]["activity"] == s["activity"]
                and s["start"] - blocks[-1]["end"] <= MERGE_GAP):
            b = blocks[-1]
            b["end"] = s["end"]
            b["n_ev"] += 1
            if s["title"]:
                b.setdefault("titles", []).append(s["title"][:100])
        else:
            blocks.append({"start": s["start"], "end": s["end"], "project": s["project"],
                           "activity": s["activity"], "conf": s["conf"], "n_ev": 1,
                           "titles": [s["title"][:100]] if s["title"] else []})
    for b in blocks:
        b["id"] = "blk_" + hashlib.sha1(
            f"{b['start'].isoformat()}|{b['project']}|{b['activity']}".encode()).hexdigest()[:10]
    return blocks


def _apply_overrides(day_key: str, blocks):
    rows = db.q("SELECT * FROM manual_overrides WHERE day_key=? AND target_type="
                "'activity_segment' AND superseded_by IS NULL "
                "AND field IN ('project','activity_type','end') ORDER BY id", (day_key,))
    applied = []
    for b in blocks:
        for r in rows:
            anchor = parse_ts(r["anchor_start"])
            if anchor is None or not (b["start"] <= anchor < b["end"]):
                continue
            if r["field"] == "project":
                b["project"] = r["value"]
            elif r["field"] == "activity_type":
                b["activity"] = r["value"]
            elif r["field"] == "end":
                new_end = parse_ts(r["value"])
                if new_end and new_end > b["start"]:
                    b["end"] = new_end
            b["conf"] = 1.0
            b["override_id"] = r["id"]
            applied.append(r["id"])
    return blocks, applied


def _agent_lane(day_key: str):
    start, end = day_bounds(day_key)
    rows = db.q("SELECT s.id, s.source, s.start_at, s.end_at, s.status, s.cwd "
                "FROM agent_sessions s WHERE s.start_at < ? AND "
                "COALESCE(s.end_at, s.start_at) >= ?", (end.isoformat(), start.isoformat()))
    out = []
    for r in rows:
        s0 = parse_ts(r["start_at"])
        s1 = parse_ts(r["end_at"]) or s0
        iv = _clip((s0, s1), start, end)
        if not iv:
            continue
        out.append({"id": r["id"], "agent": r["source"], "start": iv[0], "end": iv[1],
                    "status": r["status"], "cwd": r["cwd"],
                    "project": projects.classify_for_session(r["cwd"])})
    return out


def _evidence_points(day_key: str):
    start, end = day_bounds(day_key)
    pts = []
    for r in db.q("SELECT id, source_key, event_type, occurred_at, payload FROM source_events "
                  "WHERE event_type IN ('git_commit','msg_user','session_meta') "
                  "AND occurred_at >= ? AND occurred_at < ? ORDER BY occurred_at",
                  (start.isoformat(), end.isoformat())):
        p = json.loads(r["payload"])
        kind = {"git_commit": "commit", "msg_user": "agent_user",
                "session_meta": "session_meta"}[r["event_type"]]
        pts.append({"id": r["id"], "at": parse_ts(r["occurred_at"]), "kind": kind,
                    "label": (p.get("subject") or p.get("text") or
                              (p.get("parts") and next((x.get("name") for x in p["parts"]
                                                        if x.get("kind") == "tool_call"), "")) or "")
                    [:80].replace("\n", " "),
                    "source": r["source_key"]})
    return pts


def _unknown_intervals(day_key: str, blocks, afk, obs):
    """Inside the window-event observed span:
    - away: known afk intervals (user not at keyboard — positively observed)
    - unknown: no window coverage at all (watcher gap / screen off mid-day)"""
    if not obs:
        return [], None
    away = [iv for iv in (_clip(x, obs[0], obs[1]) for x in afk) if iv]
    covered = _union([(b["start"], b["end"]) for b in blocks] + away)
    unknown = _subtract([(obs[0], obs[1])], covered)
    return unknown, obs


def day_timeline(day_key: str) -> dict:
    segs, evs, afk, obs, start, end = _active_segments(day_key)
    blocks = _merge_blocks(segs)
    blocks, applied = _apply_overrides(day_key, blocks)
    unknown, obs_span = _unknown_intervals(day_key, blocks, afk, obs)
    away_ivs = [iv for iv in (_clip(x, obs[0], obs[1]) for x in afk) if iv] if obs else []
    agent = _agent_lane(day_key)
    evidence = _evidence_points(day_key)
    for b in blocks:
        b["evidence_count"] = sum(1 for p in evidence if b["start"] <= p["at"] < b["end"])

    human_union = _union([(b["start"], b["end"]) for b in blocks])
    human_total = sum((e - s).total_seconds() for s, e in human_union)
    agent_total = sum((a["end"] - a["start"]).total_seconds() for a in agent)
    agent_union = _union([(a["start"], a["end"]) for a in agent])
    agent_union_total = sum((e - s).total_seconds() for s, e in agent_union) if agent else 0
    window_total = 0
    for e in evs:
        if e["type"] == "aw_window":
            iv = _clip((e["ts"], e["ts"] + dt.timedelta(seconds=e["dur"])), start, end)
            if iv:
                window_total += (iv[1] - iv[0]).total_seconds()
    obs_dur = ((obs[1] - obs[0]).total_seconds() if obs else 0)
    away_total = sum((e - s).total_seconds() for s, e in away_ivs)

    def ser(b):
        return {"id": b["id"], "start": b["start"].astimezone(tz()).isoformat(),
                "end": b["end"].astimezone(tz()).isoformat(),
                "project": b["project"], "activity_type": b["activity"],
                "confidence": round(b["conf"], 2), "evidence_count": b["evidence_count"],
                "source": "manual" if b.get("override_id") else "inferred",
                "is_manual_override": bool(b.get("override_id")),
                "n_events": b["n_ev"], "titles": b.get("titles", [])[:8]}

    return {
        "day_key": day_key,
        "timezone": str(tz()),
        "blocks": [ser(b) for b in blocks],
        "agent": [{"id": a["id"], "agent": a["agent"], "cwd": a["cwd"],
                   "project": a["project"], "status": a["status"],
                   "start": a["start"].astimezone(tz()).isoformat(),
                   "end": a["end"].astimezone(tz()).isoformat()} for a in agent],
        "evidence": [{"id": p["id"], "at": p["at"].astimezone(tz()).isoformat(),
                      "kind": p["kind"], "label": p["label"], "source": p["source"]}
                     for p in evidence],
        "unknown": [{"start": s.astimezone(tz()).isoformat(),
                     "end": e.astimezone(tz()).isoformat()} for s, e in (unknown or [])],
        "away": [{"start": s.astimezone(tz()).isoformat(),
                  "end": e.astimezone(tz()).isoformat()} for s, e in away_ivs],
        "observed_span": ({"start": obs[0].astimezone(tz()).isoformat(),
                           "end": obs[1].astimezone(tz()).isoformat()} if obs else None),
        "metrics": {
            "human_active_sec": round(human_total),
            "human_active_blocks": len(blocks),
            "window_total_sec": round(window_total),
            "afk_sec": round(away_total),
            "observed_span_sec": round(obs_dur),
            "observed_coverage": round(human_total / obs_dur, 3) if obs_dur else None,
            "agent_runtime_sec": round(agent_total),
            "agent_runtime_union_sec": round(agent_union_total),
            "unknown_sec": round(sum((e - s).total_seconds() for s, e in (unknown or []))),
            "evidence_count": len(evidence),
        },
        "has_observation": obs is not None,
    }
