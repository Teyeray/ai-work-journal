#!/usr/bin/env python3
"""Build reports/dashboard.html from data/*.json.

Self-contained offline HTML: inline CSS + vanilla JS + SVG, no CDN.

Usage: dashboard.py
"""
import datetime as dt
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
OUT = BASE / "reports" / "dashboard.html"
LOCAL_TZ = dt.datetime.now().astimezone().tzinfo

FOLD = {"paperfacts-dev": "paperfacts", "paperfacts-fig": "paperfacts"}
SKIP_PROJ = {"llm", "work", "tmp", "playground", "notes", "projects", "kaggle",
             "documents", "home", ""}


def proj_of(cwd):
    """Map a session cwd to a project label."""
    if not cwd:
        return "其他"
    name = cwd.rstrip("/").rsplit("/", 1)[-1]
    key = name.lower()
    if key in SKIP_PROJ:
        return "其他"
    return FOLD.get(key, name)


def hm(iso):
    t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(LOCAL_TZ)
    return t.strftime("%H:%M")


def compact_day(j):
    aw = j.get("activitywatch") or {}
    hours = {}
    for hh, h in (aw.get("hours") or {}).items():
        hours[hh] = {
            "a": h.get("apps") or {},
            "t": [{"t": x["title"], "d": x["duration"]} for x in h.get("titles") or []],
            "p": h.get("proj") or {},
        }
    sessions = []
    for s in j.get("agent_sessions") or []:
        sessions.append({
            "g": s["agent"], "p": proj_of(s.get("cwd")),
            "k": (s.get("task") or "").replace("\n", " ")[:140],
            "t0": hm(s["first"]), "t1": hm(s["last"]),
            "sp": s.get("span", 0),
            "u": s.get("user_msgs", 0), "a": s.get("assistant_msgs", 0),
            "tok": s.get("tokens") or {},
            "tl": sorted((s.get("tools") or {}).items(), key=lambda kv: -kv[1])[:5],
        })
    sessions.sort(key=lambda s: s["t0"])
    commits = []
    for r in j.get("git") or []:
        cs = r.get("commits") or []
        if not cs:
            continue
        repo = r["repo"].replace("/home/yangrm/", "")
        commits.append({
            "r": repo, "n": len(cs),
            "ts": [c["time"] for c in cs],
            "subj": [f"{c['time']} {c['hash']} {c['subject'][:70]}" for c in cs[:12]],
        })
    gh = j.get("github") or {}
    prs = gh.get("prs_created") or []
    reviews = gh.get("reviews") or []
    comments = gh.get("comments") or []
    fs = j.get("feishu") or {}
    chats = []
    ex_re = re.compile(r"^(\d{1,2}):(\d{2})\s+(.+?):\s+(.*)$")
    for c in fs.get("chats") or []:
        exs = []
        for e in c.get("excerpts") or []:
            m = ex_re.match(e)
            if m:
                exs.append({"h": m.group(1), "m": m.group(2),
                            "w": m.group(3)[:24], "x": m.group(4)[:90]})
        if c.get("total"):
            chats.append({"c": c["chat"][:24], "n": c["total"],
                          "me": c.get("mine", 0), "ex": exs[:12]})
    files = [{"n": f["name"][:60], "t": f.get("time", ""), "c": f.get("chat", "")[:20]}
             for f in (fs.get("files") or [])[:12]]
    return {
        "date": j["date"],
        "aw": {
            "active": round(aw.get("active_time", 0)),
            "afk": round(aw.get("afk_time", 0)),
            "apps": [{"app": a["app"], "d": a["duration"],
                      "t": [[x["title"], x["duration"]] for x in a.get("titles") or []]}
                     for a in aw.get("apps") or []],
            "hours": hours,
            "projects": aw.get("projects") or {},
        },
        "sessions": sessions,
        "commits": commits,
        "gh": {
            "prc": len(prs), "prm": sum(1 for p in prs if p.get("state") == "merged"),
            "rv": len(reviews), "cm": len(comments),
            "pr": [{"t": p["title"][:60], "s": p.get("state", "")} for p in prs[:12]],
            "cmv": [f"{c['time']} {c.get('text', '')[:80]}" for c in comments[:10]],
        },
        "fs": {"chats": chats, "files": files,
               "n": sum(c["n"] for c in chats),
               "me": sum(c["me"] for c in chats)},
    }


def main():
    days = []
    for f in sorted(DATA.glob("????-??-??.json")):
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if j.get("date"):
            days.append(compact_day(j))
    if not days:
        print("no data days found")
        sys.exit(1)
    rng = f"{days[0]['date']} ~ {days[-1]['date']}"
    data = json.dumps(days, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = (BASE / "templates" / "dashboard.html").read_text(encoding="utf-8")
    html = (html.replace("__DATA__", data)
                .replace("__RANGE__", rng)
                .replace("__NDAYS__", str(len(days)))
                .replace("__GENERATED__", dt.datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")))
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print(f"dashboard written: {OUT} ({len(days)} days, {OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
