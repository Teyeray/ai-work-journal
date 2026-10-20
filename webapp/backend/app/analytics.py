"""Range analytics over day projections (pure functions of timeline dicts).

Definitions (shown to the user, so keep them simple and stable):

  human time      foreground windows minus away time (timeline metrics)
  agent time      union of all sessions' execution segments (counted once)
  project lane    a window's project; categories (浏览/阅读/…) form one "其他活动" lane
  stretch         a run of windows in one lane; windows < STRAY_SEC don't break it,
                  an away gap > BREAK_GAP does. Its length is end - start.
  switch          two consecutive stretches in different lanes with < BREAK_GAP between
  focus share     share of project time spent in stretches >= 30 min
"""
import datetime as dt
from collections import Counter, defaultdict

from .i18n import tr
from . import projects
from .timeutil import parse_ts

STRAY_SEC = 60
BREAK_GAP = dt.timedelta(minutes=5)
FOCUS_BINS = [(0, 5, "<5 分钟"), (5, 15, "5–15 分钟"), (15, 30, "15–30 分钟"),
              (30, 60, "30–60 分钟"), (60, 10 ** 6, "≥60 分钟")]
OTHER = "其他活动"


def _lane(project: str) -> str:
    return OTHER if projects.is_category(project) else project


def _minutes_by_hour(intervals, day_start):
    out = [0.0] * 24
    for s, e in intervals:
        cur = s
        while cur < e:
            h = int((cur - day_start).total_seconds() // 3600)
            if not 0 <= h < 24:
                break
            nxt = min(e, day_start + dt.timedelta(hours=h + 1))
            out[h] += (nxt - cur).total_seconds() / 60
            cur = nxt
    return [round(x, 1) for x in out]


def _union(ivs):
    out = []
    for s, e in sorted(ivs):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def stretches(windows):
    """[(lane, start, end)] from window visits (see module doc)."""
    vs = [(parse_ts(w["start"]), parse_ts(w["end"]), _lane(w["project"])) for w in windows]
    vs.sort()
    runs = []
    for s, e, lane in vs:
        if runs and runs[-1][0] == lane and s - runs[-1][2] <= BREAK_GAP:
            runs[-1][2] = max(runs[-1][2], e)
        elif (e - s).total_seconds() < STRAY_SEC and runs and s - runs[-1][2] <= BREAK_GAP:
            continue  # a stray glance: neither its own stretch nor a switch
        else:
            runs.append([lane, s, e])
    return [tuple(r) for r in runs]


def day_stats(t: dict, day_start) -> dict:
    """Everything the analytics page needs from one day's timeline."""
    m = t["metrics"]
    alloc = Counter()
    for b in t["blocks"]:
        alloc[_lane(b["project"])] += (parse_ts(b["end"]) - parse_ts(b["start"])).total_seconds()
    agent_by = defaultdict(list)
    for a in t["agent"]:
        for sg in a["segments"]:
            agent_by[a["project"] or "Unassigned"].append((parse_ts(sg["start"]), parse_ts(sg["end"])))
    agent_alloc = {p: sum((e - s).total_seconds() for s, e in _union(iv)) for p, iv in agent_by.items()}
    agent_union = _union([iv for ivs in agent_by.values() for iv in ivs])
    human_ivs = [(parse_ts(b["start"]), parse_ts(b["end"])) for b in t["blocks"]]

    runs = stretches(t.get("windows", []))
    switches, sw_hour = 0, [0] * 24
    for a, b in zip(runs, runs[1:]):
        if a[0] != b[0] and b[1] - a[2] <= BREAK_GAP:
            switches += 1
            h = int((b[1] - day_start).total_seconds() // 3600)
            if 0 <= h < 24:
                sw_hour[h] += 1
    focus = [0.0] * len(FOCUS_BINS)
    longest = None
    for lane, s, e in runs:
        if lane == OTHER:
            continue
        mins = (e - s).total_seconds() / 60
        for i, (lo, hi, _) in enumerate(FOCUS_BINS):
            if lo <= mins < hi:
                focus[i] += mins
        if longest is None or mins > longest["min"]:
            longest = {"project": lane, "min": round(mins, 1), "start": s.isoformat()}
    ev = t["evidence"]
    return {
        "day": t["day_key"],
        "has_observation": t["has_observation"],
        "human_sec": m["human_active_sec"],
        "agent_sec": m.get("agent_exec_union_sec", 0),
        "agent_peak": m.get("agent_peak_parallel", 0),
        "unknown_sec": m["unknown_sec"],
        "prompts": sum(1 for p in ev if p["kind"] == "agent_user"),
        "commits": sum(1 for p in ev if p["kind"] == "commit"),
        "sessions": sum(1 for a in t["agent"] if not a.get("subagent")),
        "allocation": {k: round(v) for k, v in alloc.items()},
        "agent_allocation": {k: round(v) for k, v in agent_alloc.items()},
        "hourly_human": _minutes_by_hour(_union(human_ivs), day_start),
        "hourly_agent": _minutes_by_hour(agent_union, day_start),
        "hourly_switches": sw_hour,
        "switches": switches,
        "focus_min": [round(x, 1) for x in focus],
        "longest": longest,
        "sessions_by_project": dict(Counter(a["project"] or "Unassigned" for a in t["agent"] if not a.get("subagent"))),
        "commits_by_project": dict(Counter((p.get("project") or p.get("repo") or "") for p in ev if p["kind"] == "commit")),
    }


def summarize(days: list) -> dict:
    """Range totals; per-observed-day averages for human-side measures."""
    obs = [d for d in days if d["has_observation"]]
    n_obs = len(obs)
    human = sum(d["human_sec"] for d in obs)
    proj_time = sum(v for d in obs for k, v in d["allocation"].items() if k != OTHER)
    all_time = sum(v for d in obs for v in d["allocation"].values())
    focus = [sum(d["focus_min"][i] for d in obs) for i in range(len(FOCUS_BINS))]
    focus_total = sum(focus)
    return {
        "days": len(days), "observed_days": n_obs,
        "human_per_day": human / n_obs if n_obs else None,
        "agent_per_day": sum(d["agent_sec"] for d in days) / len(days) if days else None,
        "project_share": proj_time / all_time if all_time else None,
        "switches_per_hour": (sum(d["switches"] for d in obs) / (human / 3600)) if human else None,
        "focus_share": (focus[3] + focus[4]) / focus_total if focus_total else None,
        "commits": sum(d["commits"] for d in days),
        "prompts": sum(d["prompts"] for d in days),
    }


def project_table(days: list, priorities: dict) -> list:
    hum, ag = Counter(), Counter()
    for d in days:
        for k, v in d["allocation"].items():
            hum[k] += v
        for k, v in d["agent_allocation"].items():
            ag[k] += v
    rows = []
    for p in set(hum) | set(ag):
        rows.append({"project": p, "human_sec": round(hum[p]), "agent_sec": round(ag[p]),
                     "priority": priorities.get(p)})
    return sorted(rows, key=lambda r: -(r["human_sec"] * 2 + r["agent_sec"]))


def insights(days: list, cur: dict, prev: dict, table: list) -> list:
    """Claims computed from the data, each with its evidence and confidence."""
    out = []
    n, n_obs = cur["days"], cur["observed_days"]
    conf = "高" if n_obs >= 10 else "中" if n_obs >= 5 else "低"
    if n_obs < n:
        out.append({"kind": "quality", "title": tr(f"{n_obs}/{n} 天有窗口记录", f"{n_obs}/{n} days with window data"),
                    "detail": tr("其余日子只计 Agent", "Other days: agents only")
                              + (tr(" · 样本少", " · small sample") if n_obs < 5 else ""),
                    "confidence": "高"})
    if not n_obs:
        return out
    proj = sorted([r for r in table if r["project"] not in (OTHER, "Unassigned") and r["human_sec"] > 0],
                  key=lambda r: -r["human_sec"])   # "your time" ranks by your time only
    if proj:
        tot = sum(r["human_sec"] for r in proj)
        top = proj[0]
        out.append({"kind": "projects", "title": tr("投入最多:", "Most time: ") + f"{top['project']} {top['human_sec'] / tot:.0%}",
                    "detail": tr("、", ", ").join(f"{r['project']} {r['human_sec'] / 3600:.1f}h"
                                                   for r in proj[:4]), "confidence": conf})
        pri = [r for r in proj if r["priority"] is not None and r["priority"] <= 2]
        if pri:
            share = sum(r["human_sec"] for r in pri) / tot
            out.append({"kind": "priority", "title": tr(f"高优先级项目占项目时间 {share:.0%}", f"High-priority projects take {share:.0%} of project time"),
                        "detail": "P1/P2: " + tr("、", ", ").join(r["project"] for r in pri), "confidence": conf})
        else:
            out.append({"kind": "priority", "title": tr("还没有设置项目优先级", "No project priorities set yet"),
                        "detail": tr("在「项目」页设置 P1/P2", "Set P1/P2 on Projects"),
                        "confidence": "高"})
    if cur["focus_share"] is not None:
        f = cur["focus_share"]
        d = ""
        if prev and prev.get("focus_share") is not None and prev["observed_days"]:
            delta = f - prev["focus_share"]
            d = tr(f" · {'↑' if delta >= 0 else '↓'}{abs(delta) * 100:.0f}pp",
                   f" · {'↑' if delta >= 0 else '↓'}{abs(delta) * 100:.0f}pp")
        out.append({"kind": "focus", "title": tr(f"长专注 {f:.0%}{d}", f"Long focus {f:.0%}{d}"),
                    "detail": tr("≥30 分钟连续段占项目时间", "Share of project time in runs ≥30 min"),
                    "confidence": conf})
    hours = [sum(d["hourly_switches"][h] for d in days) for h in range(24)]
    if sum(hours) >= 10:
        h = max(range(24), key=lambda i: hours[i])
        out.append({"kind": "switch", "title": f"{h:02d}:00–{h + 1:02d}:00" + tr(" 切换最多", " most switches"),
                    "detail": tr(f"{hours[h]} 次 / 共 {sum(hours)}", f"{hours[h]} of {sum(hours)} switches"),
                    "confidence": conf})
    lev = [r for r in proj if r["human_sec"] >= 1800 and r["agent_sec"] > 0]
    if lev:
        best = max(lev, key=lambda r: r["agent_sec"] / r["human_sec"])
        x = best['agent_sec'] / best['human_sec']
        out.append({"kind": "leverage", "title": tr(f"{best['project']} · Agent 杠杆 {x:.1f}×", f"{best['project']} · agent leverage {x:.1f}×"),
                    "detail": tr("Agent 小时 / 你的小时", "Agent hours per hour of yours"),
                    "confidence": conf})
    return out
