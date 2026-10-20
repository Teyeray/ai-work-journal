"""Day timeline projection: human / agent / evidence / unknown lanes.

Facts: AW window events (foreground) minus AW afk intervals (user away) of at
least config.AFK_MIN_SEC = human active segments, clipped to the local day. Merged into blocks by
(project label, activity type). Unknown = gaps inside the observed span.
Manual overrides patch labels/ranges afterwards and always win.

Pure function of (events, overrides) — deterministic, no hidden state.
"""
import datetime as dt
import hashlib
import json
from collections import Counter

from . import config, db, episodes, projects
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
    # short idle stretches are reading / watching an agent, not leaving the desk
    afk = [iv for iv in (_clip(x, start, end) for x in afk_union
                         if (x[1] - x[0]).total_seconds() >= config.AFK_MIN_SEC) if iv]
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


def _classify_segments(segs):
    """Direct evidence: what each window says about itself."""
    for s in segs:
        s["project"], s["reason"] = projects.explain(s["app"], s["title"])
        s["activity"] = _activity_type(s["app"], s["title"])
        s["conf"] = 0.75 if s["title"] else 0.45
    return segs


def _merge_blocks(segs):
    """Merge contiguous segments with same (project, activity)."""
    blocks = []
    for s in segs:
        if (blocks and blocks[-1]["project"] == s["project"]
                and blocks[-1]["activity"] == s["activity"]
                and s["start"] - blocks[-1]["end"] <= MERGE_GAP):
            b = blocks[-1]
            b["end"] = s["end"]
            b["n_ev"] += 1
            b["segs"].append(s)
            if s["title"]:
                b.setdefault("titles", []).append(s["title"][:100])
        else:
            blocks.append({"start": s["start"], "end": s["end"], "project": s["project"],
                           "reason": s["reason"], "attributed_by": s.get("attributed_by"),
                           "activity": s["activity"], "conf": s["conf"], "n_ev": 1,
                           "titles": [s["title"][:100]] if s["title"] else [],
                           "segs": [s]})
    for b in blocks:
        b["id"] = "blk_" + hashlib.sha1(
            f"{b['start'].isoformat()}|{b['project']}|{b['activity']}".encode()).hexdigest()[:10]
    return blocks


VISIT_JOIN = dt.timedelta(seconds=5)


def _window_visits(blocks):
    """Raw foreground windows at second precision, labelled with their (final)
    block's project. Consecutive segments of the same window (gap <= 5s,
    e.g. split only by an AFK edge or watcher tick) join into one visit."""
    visits = []
    for b in blocks:
        for sg in b["segs"]:
            last = visits[-1] if visits else None
            if (last and last["app"] == sg["app"] and last["title"] == sg["title"]
                    and last["project"] == b["project"] and sg["start"] - last["end"] <= VISIT_JOIN):
                last["end"] = max(last["end"], sg["end"])
            else:
                visits.append({"start": sg["start"], "end": sg["end"], "app": sg["app"],
                               "title": sg["title"][:160], "project": b["project"],
                               "activity": b["activity"],
                               "reason": sg["reason"] if b["project"] == sg["project"] else b["reason"]})
    visits.sort(key=lambda v: v["start"])
    return visits


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
                b["attributed_by"] = None
                b["reason"] = "人工修正"
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


EXEC_GAP = dt.timedelta(seconds=600)  # events further apart than this = waiting, not executing


def _agent_lane(day_key: str):
    """One lane per session. Lifecycle (first..last event) vs execution:
    event clusters with internal gaps <= EXEC_GAP are executing; gaps between
    clusters are waiting-for-user / idle. Both clipped to the day."""
    start, end = day_bounds(day_key)
    rows = db.q("SELECT s.id, s.source, s.external_id, s.start_at, s.end_at, s.status, s.cwd "
                "FROM agent_sessions s WHERE s.start_at < ? AND "
                "COALESCE(s.end_at, s.start_at) >= ?", (end.isoformat(), start.isoformat()))
    out = []
    for r in rows:
        s0 = parse_ts(r["start_at"])
        s1 = parse_ts(r["end_at"]) or s0
        if s0 and s1 <= s0:
            s1 = s0 + dt.timedelta(seconds=1)  # single-instant session: keep it (and its prompts)
        iv = _clip((s0, s1), start, end)
        if not iv:
            continue
        evs = db.q("SELECT e.id, e.occurred_at, e.event_type, CASE WHEN e.event_type="
                   "'msg_user' THEN e.payload END AS payload FROM session_event_links l "
                   "JOIN source_events e ON e.id = l.event_id WHERE l.session_id=? "
                   "ORDER BY e.occurred_at", (r["id"],))
        ts = [parse_ts(e["occurred_at"]) for e in evs]
        ts = [t for t in ts if t is not None and start <= t < end]
        n_user = sum(1 for e in evs if e["event_type"] == "msg_user")
        project = projects.classify_for_session(r["cwd"])
        # a subagent transcript's "user" turns are written by the parent agent
        subagent = "/subagents/" in (r["external_id"] or "").replace("\\", "/")
        prompts = []
        for e in evs:
            if subagent or e["event_type"] != "msg_user":
                continue
            at = parse_ts(e["occurred_at"])
            text = (json.loads(e["payload"]).get("text") or "").strip()
            if at is None or not (start <= at < end) or not _is_prompt(text):
                continue
            prompts.append({"event_id": e["id"], "at": at, "text": text[:300],
                            "project": project, "session_id": r["id"]})
        clusters = []
        for t in ts:
            if clusters and t - clusters[-1][1] <= EXEC_GAP:
                clusters[-1][1] = t
            else:
                clusters.append([t, t])
        exec_sec = 0
        segments = []
        for c0, c1 in clusters:
            c = _clip((c0, c1), start, end)
            if not c:
                continue
            segments.append(c)
            exec_sec += (c[1] - c[0]).total_seconds()
        out.append({"id": r["id"], "agent": r["source"], "start": iv[0], "end": iv[1],
                    "status": r["status"], "cwd": r["cwd"],
                    "project": project, "prompts": prompts,
                    "segments": segments, "exec_sec": round(exec_sec),
                    "n_events": len(evs), "n_user": 0 if subagent else n_user,
                    "subagent": subagent})
    return out


# harness-injected "user" turns (hooks, forks, compaction, interrupts) — not typed by a human
INJECTED_PREFIXES = ("<", "A session-scoped Stop hook", "The fork runs as its own",
                     "This session is being continued", "[Request interrupted", "Caveat:")


def _is_prompt(text: str) -> bool:
    """Human-typed instruction, not injected context."""
    return len(text) >= 5 and not text.startswith(INJECTED_PREFIXES)


# --- context attribution: windows that say nothing about themselves ---
# A window with no direct evidence (classified only by its app: a bare tmux
# tab "t", "New Tab", a chat app, "~") borrows the project of what surrounds it:
#   * windows with direct evidence before/after it, weighted by duration and
#     decayed by the time gap,
#   * prompts you sent to a project's agent, strongest when sent during it.
# Direct evidence is never overridden; deliberate categories (keyword rules
# such as 个人事务) are never reassigned; a long silent stretch (> MAX_ABSORB
# with no prompt in it) keeps its category rather than being swallowed.
CONTEXT_TAU = 4.0          # minutes: weight halves roughly every ~3 min of distance
CONTEXT_REACH = dt.timedelta(minutes=12)
CONTEXT_MAX_ABSORB = dt.timedelta(minutes=15)
CONTEXT_MIN_SHARE = 0.6
WEAK_REASONS = ("按应用分类", "未知应用")


def _is_weak(sg) -> bool:
    return sg["reason"] in WEAK_REASONS


def _gap_min(t, s0, s1) -> float:
    """Minutes from instant t to interval [s0, s1] (0 inside)."""
    if s0 <= t <= s1:
        return 0.0
    return min(abs((t - s0).total_seconds()), abs((t - s1).total_seconds())) / 60


def _attribute_by_context(segs, prompts):
    import bisect
    import math
    strong = [g for g in segs if not _is_weak(g) and not projects.is_category(g["project"])]
    starts = [g["start"] for g in strong]
    p_times = [p["at"] for p in prompts]
    for g in segs:
        if not _is_weak(g):
            continue
        lo, hi = g["start"] - CONTEXT_REACH, g["end"] + CONTEXT_REACH
        score, by_prompt, inside_prompt = Counter(), Counter(), False
        for p in prompts[bisect.bisect_left(p_times, lo):bisect.bisect_right(p_times, hi)]:
            if p["project"] == "Unassigned":
                continue
            d = _gap_min(p["at"], g["start"], g["end"])
            inside_prompt |= d == 0
            w = 2.0 * math.exp(-d / CONTEXT_TAU)
            score[p["project"]] += w
            by_prompt[p["project"]] += w
        i = bisect.bisect_left(starts, lo - CONTEXT_REACH)
        for n in strong[i:]:
            if n["start"] > hi:
                break
            if n["end"] < lo:
                continue
            gap = 0.0 if n["end"] >= g["start"] and n["start"] <= g["end"] else \
                min(abs((g["start"] - n["end"]).total_seconds()), abs((n["start"] - g["end"]).total_seconds())) / 60
            dur = min((n["end"] - n["start"]).total_seconds() / 60, 5.0) / 5.0
            score[n["project"]] += dur * math.exp(-gap / CONTEXT_TAU)
        if not score:
            continue
        top, sc = score.most_common(1)[0]
        if sc < 0.3 or sc / sum(score.values()) < CONTEXT_MIN_SHARE:
            continue
        if g["end"] - g["start"] > CONTEXT_MAX_ABSORB and not inside_prompt:
            continue
        g["category"] = g["project"]
        g["project"] = top
        g["attributed_by"] = "context"
        g["reason"] = (f"上下文:此时你在给 {top} 的 agent 发 prompt" if by_prompt[top] >= sc / 2
                       else f"上下文:前后都在 {top}")
    return segs


def _evidence_points(day_key: str):
    start, end = day_bounds(day_key)
    from .adapters import git as git_mod
    pts = []
    repo_paths = {json.loads(r["payload"]).get("path") for r in db.q(
        "SELECT payload FROM source_events WHERE event_type='git_commit' AND occurred_at >= ? "
        "AND occurred_at < ?", (start.isoformat(), end.isoformat()))}
    for r in db.q("SELECT id, source_key, event_type, occurred_at, payload FROM source_events "
                  "WHERE event_type IN ('git_commit','msg_user','session_meta') "
                  "AND occurred_at >= ? AND occurred_at < ? ORDER BY occurred_at",
                  (start.isoformat(), end.isoformat())):
        p = json.loads(r["payload"])
        if r["event_type"] == "git_commit" and not git_mod.is_mine(p, repo_paths):
            continue  # a collaborator's commit pulled into your clone
        kind = {"git_commit": "commit", "msg_user": "agent_user",
                "session_meta": "session_meta"}[r["event_type"]]
        pts.append({"id": r["id"], "at": parse_ts(r["occurred_at"]), "kind": kind,
                    "label": (p.get("subject") or p.get("text") or
                              (p.get("parts") and next((x.get("name") for x in p["parts"]
                                                        if x.get("kind") == "tool_call"), "")) or "")
                    [:80].replace("\n", " "),
                    "source": r["source_key"],
                    "repo": p.get("repo") if kind == "commit" else None,
                    # same folding/rules as sessions: 'paperfacts-dev' → paperfacts
                    "commit_project": (projects.classify_for_session(p.get("path") or p.get("repo") or "")
                                       if kind == "commit" else None)})
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
    agent = _agent_lane(day_key)
    prompts = sorted((p for a in agent for p in a["prompts"]), key=lambda p: p["at"])
    blocks = _merge_blocks(_attribute_by_context(_classify_segments(segs), prompts))
    blocks, applied = _apply_overrides(day_key, blocks)
    unknown, obs_span = _unknown_intervals(day_key, blocks, afk, obs)
    away_ivs = [iv for iv in (_clip(x, obs[0], obs[1]) for x in afk) if iv] if obs else []
    evidence = _evidence_points(day_key)
    by_event = {p["event_id"]: p for p in prompts}
    # keep only human-typed prompts (drop tool results, injected context, subagent turns)
    evidence = [p for p in evidence if p["kind"] != "agent_user" or p["id"] in by_event]
    for p in evidence:
        if p["kind"] == "agent_user" and p["id"] in by_event:
            p["project"] = by_event[p["id"]]["project"]
            p["session_id"] = by_event[p["id"]]["session_id"]
    commits = [{"at": p["at"], "repo": p["repo"] or "", "subject": p["label"],
                "project": p["commit_project"] if p["commit_project"] != "Unassigned" else (p["repo"] or "")}
               for p in evidence if p["kind"] == "commit"]
    eps = episodes.build(start, blocks, agent, prompts, commits)
    for b in blocks:
        b["evidence_count"] = sum(1 for p in evidence if b["start"] <= p["at"] < b["end"])

    human_union = _union([(b["start"], b["end"]) for b in blocks])
    human_total = sum((e - s).total_seconds() for s, e in human_union)
    agent_total = sum((a["end"] - a["start"]).total_seconds() for a in agent)
    agent_exec_total = sum(a["exec_sec"] for a in agent)
    agent_union = _union([(a["start"], a["end"]) for a in agent])
    exec_union = _union([sg for a in agent for sg in a["segments"]])
    # peak number of sessions executing at the same instant
    edges = sorted([(sg[0], 1) for a in agent for sg in a["segments"]] +
                   [(sg[1], -1) for a in agent for sg in a["segments"]], key=lambda x: (x[0], x[1]))
    peak = cur = 0
    for _, d in edges:
        cur += d
        peak = max(peak, cur)
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
                "n_events": b["n_ev"], "titles": b.get("titles", [])[:8],
                "attributed_by": b.get("attributed_by"), "reason": b.get("reason")}

    def iso_l(t):
        return t.astimezone(tz()).isoformat()

    def ser_ep(ep):
        return {**ep, "start": iso_l(ep["start"]), "end": iso_l(ep["end"]),
                "prompts": [{**p, "at": iso_l(p["at"])} for p in ep["prompts"]],
                "commits": [{**c, "at": iso_l(c["at"])} for c in ep["commits"]]}

    return {
        "day_key": day_key,
        "timezone": str(tz()),
        "blocks": [ser(b) for b in blocks],
        "episodes": [ser_ep(ep) for ep in eps],
        "windows": [{"start": iso_l(v["start"]), "end": iso_l(v["end"]), "app": v["app"],
                     "title": v["title"], "project": v["project"], "activity": v["activity"],
                     "reason": v["reason"]}
                    for v in _window_visits(blocks)],
        "agent": [{"id": a["id"], "agent": a["agent"], "cwd": a["cwd"],
                   "project": a["project"], "status": a["status"],
                   "start": a["start"].astimezone(tz()).isoformat(),
                   "end": a["end"].astimezone(tz()).isoformat(),
                   "segments": [{"start": s0.astimezone(tz()).isoformat(),
                                 "end": e0.astimezone(tz()).isoformat()}
                                for s0, e0 in a["segments"]],
                   "exec_sec": a["exec_sec"], "n_events": a["n_events"],
                   "n_user": a["n_user"], "subagent": a["subagent"]} for a in agent],
        "evidence": [{"id": p["id"], "at": p["at"].astimezone(tz()).isoformat(),
                      "kind": p["kind"], "label": p["label"], "source": p["source"],
                      **({"repo": p["repo"], "project": p["commit_project"]}
                         if p["kind"] == "commit" else {}),
                      **({"project": p["project"], "session_id": p["session_id"]}
                         if "project" in p else {})}
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
            "agent_exec_sec": round(agent_exec_total),
            "agent_exec_union_sec": round(sum((e - s).total_seconds() for s, e in exec_union)),
            "agent_peak_parallel": peak,
            "unknown_sec": round(sum((e - s).total_seconds() for s, e in (unknown or []))),
            "evidence_count": len(evidence),
        },
        "has_observation": obs is not None,
    }
