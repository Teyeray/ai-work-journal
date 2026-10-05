#!/usr/bin/env python3
"""Collect raw activity data for one day.

Reads these local sources:
  - ActivityWatch REST API (window focus + AFK)
  - Claude Code session files (~/.claude/projects/**.jsonl)
  - pi session files (~/.pi/agent/sessions/**.jsonl)
  - git commits under configured project roots
  - ScreenPipe local API (OCR frames)

Writes:
  data/<date>.json      structured facts
  data/<date>.digest.md compact digest for LLM input

Usage: collect.py [YYYY-MM-DD]
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)

LOCAL_TZ = dt.datetime.now().astimezone().tzinfo


def parse_date(arg):
    if arg:
        return dt.date.fromisoformat(arg)
    return dt.date.today()


def bounds(d):
    start = dt.datetime(d.year, d.month, d.day, tzinfo=LOCAL_TZ)
    end = start + dt.timedelta(days=1)
    return start, end


def parse_ts(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def in_day(ts, start, end):
    if ts is None:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=LOCAL_TZ)
    return start <= ts < end


def cwd_from_dir(dirname):
    """Decode pi session dir name to a path. "--home-yangrm-a-b--" -> "/home/yangrm/a/b"."""
    name = dirname or ""
    name = name.removeprefix("--").removesuffix("--")
    if not name or not name.startswith("home-"):
        return ""
    return "/" + name.replace("-", "/")


def fmt_dur(sec):
    sec = int(sec)
    h, m = sec // 3600, (sec % 3600) // 60
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m"


def http_json(url, headers=None, timeout=10):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------- ActivityWatch

def collect_activitywatch(start, end):
    out = {"apps": [], "active_time": 0, "afk_time": 0}
    base = CFG["aw_url"].rstrip("/")
    host = CFG["aw_host"]
    try:
        info = http_json(f"{base}/api/0/info")
        host = info.get("hostname", host)
    except Exception:
        pass
    q = "?" + urllib.parse.urlencode({
        "start": start.astimezone(dt.timezone.utc).isoformat(),
        "end": end.astimezone(dt.timezone.utc).isoformat(),
    })
    # window events
    agg = {}
    try:
        evs = http_json(f"{base}/api/0/buckets/aw-watcher-window_{host}/events{q}")
        for e in evs:
            app = (e.get("data") or {}).get("app") or "(unknown)"
            title = (e.get("data") or {}).get("title") or ""
            dur = e.get("duration", 0)
            a = agg.setdefault(app, {"dur": 0, "titles": {}})
            a["dur"] += dur
            if title:
                a["titles"][title] = a["titles"].get(title, 0) + dur
    except Exception as e:
        return {"error": f"activitywatch window: {e}", **out}
    # afk events
    try:
        evs = http_json(f"{base}/api/0/buckets/aw-watcher-afk_{host}/events{q}")
        for e in evs:
            dur = e.get("duration", 0)
            if (e.get("data") or {}).get("status") == "not-afk":
                out["active_time"] += dur
            else:
                out["afk_time"] += dur
    except Exception:
        pass
    n_titles = CFG.get("top_titles_per_app", 3)
    for app, a in sorted(agg.items(), key=lambda kv: -kv[1]["dur"])[: CFG.get("top_apps", 25)]:
        titles = sorted(a["titles"].items(), key=lambda kv: -kv[1])[:n_titles]
        out["apps"].append({
            "app": app,
            "duration": round(a["dur"]),
            "titles": [{"title": t[:100], "duration": round(d)} for t, d in titles],
        })
    return out


# ---------------------------------------------------------------- agent sessions

def first_text(content):
    """Extract human prompt text from a message content field."""
    if isinstance(content, str):
        t = content.strip()
        if t and not t.startswith("<"):
            return t
        return None
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                t = (part.get("text") or "").strip()
                if t and not t.startswith("<"):
                    return t
    return None


def is_tool_result(content):
    if isinstance(content, list):
        return any(isinstance(p, dict) and p.get("type") == "tool_result" for p in content)
    return False


def collect_claude(start, end):
    root = Path(CFG["claude_dir"])
    sessions = []
    if not root.exists():
        return sessions
    for f in root.rglob("*.jsonl"):
        try:
            st = f.stat()
        except OSError:
            continue
        # cheap skip: file not touched since day start
        if dt.datetime.fromtimestamp(st.st_mtime, LOCAL_TZ) < start:
            continue
        s = {"agent": "claude-code", "file": str(f), "cwd": "", "task": None,
             "user_msgs": 0, "assistant_msgs": 0, "tools": {}, "files": set(),
             "first": None, "last": None}
        try:
            with f.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = parse_ts(d.get("timestamp"))
                    if not in_day(ts, start, end):
                        continue
                    s["first"] = ts if s["first"] is None else s["first"]
                    s["last"] = ts
                    if not s["cwd"]:
                        s["cwd"] = d.get("cwd") or ""
                    t = d.get("type")
                    if t == "user":
                        msg = d.get("message") or {}
                        if not is_tool_result(msg.get("content")):
                            txt = first_text(msg.get("content"))
                            if txt:
                                s["user_msgs"] += 1
                                if s["task"] is None:
                                    s["task"] = txt
                    elif t == "assistant":
                        s["assistant_msgs"] += 1
                        msg = d.get("message") or {}
                        content = msg.get("content")
                        if isinstance(content, list):
                            for part in content:
                                if isinstance(part, dict) and part.get("type") == "tool_use":
                                    name = part.get("name") or "?"
                                    s["tools"][name] = s["tools"].get(name, 0) + 1
                                    args = part.get("input") or {}
                                    for key in ("file_path", "path", "notebook_path"):
                                        p = args.get(key)
                                        if isinstance(p, str):
                                            s["files"].add(p)
        except OSError:
            continue
        if s["first"] is not None:
            s["span"] = round((s["last"] - s["first"]).total_seconds())
            s["files"] = sorted(s["files"])[:15]
            sessions.append(s)
    return sessions


def collect_pi(start, end):
    root = Path(CFG["pi_dir"])
    sessions = []
    if not root.exists():
        return sessions
    for f in root.rglob("*.jsonl"):
        try:
            st = f.stat()
        except OSError:
            continue
        if dt.datetime.fromtimestamp(st.st_mtime, LOCAL_TZ) < start:
            continue
        s = {"agent": "pi", "file": str(f), "cwd": "", "task": None,
             "user_msgs": 0, "assistant_msgs": 0, "tools": {}, "files": set(),
             "first": None, "last": None}
        if not s["cwd"]:
            s["cwd"] = cwd_from_dir(f.parent.name)
        try:
            with f.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = parse_ts(d.get("timestamp"))
                    if not in_day(ts, start, end):
                        continue
                    if d.get("type") == "session":
                        s["cwd"] = d.get("cwd") or s["cwd"]
                    s["first"] = ts if s["first"] is None else s["first"]
                    s["last"] = ts
                    if d.get("type") == "message":
                        msg = d.get("message") or {}
                        role = msg.get("role")
                        txt = first_text(msg.get("content"))
                        if role == "user" and txt:
                            s["user_msgs"] += 1
                            if s["task"] is None:
                                s["task"] = txt
                        elif role == "assistant" and msg.get("content"):
                            s["assistant_msgs"] += 1
        except OSError:
            continue
        if s["first"] is not None:
            s["span"] = round((s["last"] - s["first"]).total_seconds())
            s["files"] = sorted(s["files"])[:15]
            sessions.append(s)
    return sessions


# ---------------------------------------------------------------- git

def collect_git(start, end):
    repos = []
    seen_commits = set()
    depth = CFG.get("git_max_depth", 3)
    until = end.strftime("%Y-%m-%d %H:%M")
    since = start.strftime("%Y-%m-%d %H:%M")
    for root in CFG.get("project_roots", []):
        p = Path(root)
        if not p.exists():
            continue
        for i, gdir in enumerate(p.rglob(".git")):
            if i > 200:
                break
            repo = gdir.parent
            if any(seg in ("node_modules", ".venv", "venv") for seg in repo.parts):
                continue
            try:
                r = subprocess.run(
                    ["git", "-C", str(repo), "log", "--all", "--since", since,
                     "--until", until, "--pretty=format:%h|%ad|%s",
                     "--date=format:%H:%M"],
                    capture_output=True, text=True, timeout=15)
            except subprocess.TimeoutExpired:
                continue
            commits = []
            for ln in (r.stdout or "").splitlines():
                parts = ln.split("|", 2)
                if len(parts) == 3 and parts[0] not in seen_commits:
                    seen_commits.add(parts[0])
                    commits.append({"hash": parts[0], "time": parts[1],
                                    "subject": parts[2][:120]})
            if commits:
                repos.append({"repo": str(repo), "commits": commits})
    return repos


# ---------------------------------------------------------------- screenpipe

def collect_screenpipe(start, end):
    out = {"frames": 0, "devices": {}, "samples": []}
    tok_file = CFG.get("sp_token_file")
    headers = {}
    try:
        token = Path(tok_file).read_text(encoding="utf-8").strip()
        headers["Authorization"] = f"Bearer {token}"
    except Exception:
        return {"error": "no screenpipe token", **out}
    base = CFG["sp_url"].rstrip("/")
    q = "?" + urllib.parse.urlencode({
        "query": " ", "limit": 500, "offset": 0,
        "start_time": start.isoformat(), "end_time": end.isoformat(),
    })
    try:
        d = http_json(f"{base}/search{q}", headers=headers, timeout=20)
    except Exception as e:
        return {"error": f"screenpipe: {e}", **out}
    items = d.get("data") or []
    out["frames"] = len(items)
    texts = []
    for it in items:
        c = it.get("content") or {}
        dev = c.get("device_name") or "?"
        out["devices"][dev] = out["devices"].get(dev, 0) + 1
        t = (c.get("text") or "").strip()
        if t:
            texts.append((c.get("timestamp") or "", t))
    texts.sort()
    step = max(1, len(texts) // CFG.get("sp_samples", 8))
    out["samples"] = [t[:220] for _, t in texts[::step]][: CFG.get("sp_samples", 8)]
    return out


# ---------------------------------------------------------------- digest writer

def agent_summary(sessions):
    """Merge claude+pi per project for compactness."""
    by_proj = {}
    for s in sessions:
        proj = s.get("cwd") or "(unknown)"
        if proj.startswith("/home/yangrm"):
            proj = "~" + proj[len("/home/yangrm"):]
        p = by_proj.setdefault(proj, {"projects": proj, "sessions": []})
        p["sessions"].append(s)
    return by_proj


def write_digest(d, data):
    lines = []
    lines.append(f"# 原始活动数据 {d.isoformat()}")
    aw = data["activitywatch"]
    lines.append("")
    lines.append("## 1. 电脑使用统计")
    if aw.get("error"):
        lines.append(f"- 采集异常: {aw['error']}")
    lines.append(f"- 活跃时长(非空闲): {fmt_dur(aw.get('active_time', 0))}, 空闲: {fmt_dur(aw.get('afk_time', 0))}")
    if aw.get("apps"):
        lines.append("")
        lines.append("应用时长排行:")
        for a in aw["apps"]:
            titles = "; ".join(f"{t['title']}({fmt_dur(t['duration'])})" for t in a["titles"])
            lines.append(f"- {a['app']} {fmt_dur(a['duration'])} — {titles}")
    lines.append("")
    lines.append("## 2. AI 编程助手会话")
    if data["agent_sessions"]:
        for proj, p in sorted(agent_summary(data["agent_sessions"]).items()):
            total_span = sum(s["span"] for s in p["sessions"])
            lines.append(f"### 项目 {proj} — {len(p['sessions'])} 个会话, 活动跨度 {fmt_dur(total_span)}")
            for s in p["sessions"]:
                lines.append(f"- [{s['agent']}] {s['first'].astimezone(LOCAL_TZ):%H:%M}-{s['last'].astimezone(LOCAL_TZ):%H:%M}, "
                             f"用户消息 {s['user_msgs']}, 助手消息 {s['assistant_msgs']}")
                if s.get("task"):
                    task = s["task"].replace("\n", " ")[:280]
                    lines.append(f"  任务: {task}")
                if s.get("tools"):
                    tops = sorted(s["tools"].items(), key=lambda kv: -kv[1])[:6]
                    lines.append("  工具: " + ", ".join(f"{k}×{v}" for k, v in tops))
                if s.get("files"):
                    lines.append("  文件: " + ", ".join(s["files"][:10]))
    else:
        lines.append("- 无")
    lines.append("")
    lines.append("## 3. Git 提交")
    if data["git"]:
        for r in data["git"]:
            repo = r["repo"].replace("/home/yangrm", "~")
            lines.append(f"### {repo} ({len(r['commits'])} commits)")
            for c in r["commits"]:
                lines.append(f"- {c['time']} {c['hash']} {c['subject']}")
    else:
        lines.append("- 无")
    sp = data["screenpipe"]
    lines.append("")
    lines.append("## 4. 屏幕 OCR 采样")
    if sp.get("error"):
        lines.append(f"- {sp['error']}")
    else:
        lines.append(f"- 采集帧数: {sp['frames']}, 设备: {sp.get('devices')}")
        for i, t in enumerate(sp.get("samples", []), 1):
            clean = re.sub(r"\s+", " ", t)
            lines.append(f"- 样本{i}: {clean}")
    return "\n".join(lines) + "\n"


def main():
    d = parse_date(sys.argv[1] if len(sys.argv) > 1 else None)
    start, end = bounds(d)
    data = {
        "date": d.isoformat(),
        "generated_at": dt.datetime.now(LOCAL_TZ).isoformat(),
        "activitywatch": collect_activitywatch(start, end),
        "agent_sessions": collect_claude(start, end) + collect_pi(start, end),
        "git": collect_git(start, end),
        "screenpipe": collect_screenpipe(start, end),
    }
    (DATA / f"{d.isoformat()}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    digest = write_digest(d, data)
    (DATA / f"{d.isoformat()}.digest.md").write_text(digest, encoding="utf-8")
    n_sessions = len(data["agent_sessions"])
    n_commits = sum(len(r["commits"]) for r in data["git"])
    print(f"collected {d.isoformat()}: {n_sessions} agent sessions, "
          f"{n_commits} commits, digest {len(digest)} chars")


if __name__ == "__main__":
    main()
