#!/usr/bin/env python3
"""Collect raw activity data for one day.

Reads these local sources:
  - ActivityWatch REST API (window focus + AFK)
  - Claude Code session files (~/.claude/projects/**.jsonl)
  - pi session files (~/.pi/agent/sessions/**.jsonl)
  - git commits under configured project roots
  - screen OCR: ScreenPipe local API and/or the web app's built-in sampler

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

import collect_feishu
import agent_sources

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import osutil  # noqa: E402

CFG = osutil.load_config()
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
    # A manual report is a snapshot up to now, never a claim about future work.
    current = dt.datetime.now(LOCAL_TZ)
    if start <= current < end:
        end = current
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

# ---------------------------------------------------------------- project buckets

# terminal title -> project name. built once per collect run.
PROJ_FOLD = {"paperfacts-dev": "paperfacts", "paperfacts-fig": "paperfacts"}
PROJ_SKIP = {"llm", "work", "tmp", "playground", "notes", "projects", "kaggle"}
PROJ_ALIAS = {
    "rsi": r"\brsi\b|memo ?harness|darwin godel|self-?improv|\bharness\b",
    "casmi": r"\bcasmi\b|enveda|mass spectra",
    "ai-work-journal": r"ai-?work-?journal|工作日志",
}
APP_CAT = {
    "zotero": "文献阅读",
    "google-chrome": "网页浏览",
    "com.mitchellh.ghostty": "终端·其他",
    "org.gnome.texteditor": "文本编辑",
    "org.gnome.nautilus": "文件管理",
    "org.gnome.ptyxis": "终端·其他",
    "jetbrains-pycharm": "PyCharm",
    "warp-taskbar": "系统",
}


def build_bucket_matchers():
    """Scan project roots and compile title matchers. Returns [(regex, label)]."""
    label_by_key = {}
    dirs = []
    for root in CFG.get("project_roots", []):
        try:
            for child in Path(root).iterdir():
                if not child.is_dir() or child.name.startswith("."):
                    continue
                dirs.append((child.name.lower(), child.name))
        except OSError:
            pass
    for key, name in sorted(dirs):
        if key in PROJ_SKIP:
            continue
        fold = PROJ_FOLD.get(key, key)
        if key == fold:
            label_by_key[fold] = name
        else:
            label_by_key.setdefault(fold, fold)
    for key in PROJ_ALIAS:
        label_by_key.setdefault(key, key.upper() if len(key) < 5 else key)
    matchers = []
    for key, label in sorted(label_by_key.items()):
        parts = [r"\b" + re.escape(key) + r"\b"]
        if key in PROJ_ALIAS:
            parts.append(PROJ_ALIAS[key])
        matchers.append((re.compile("|".join(parts), re.I), label))
    return matchers


def bucket_for(app, title, matchers):
    """Map one window event to a project or category label."""
    if title:
        t = title.lower()
        if "滴答清单" in t or "ta-da" in t:
            return "个人事务"
        best, best_pos = None, 1 << 30
        for rx, label in matchers:
            m = rx.search(t)
            if m and m.start() < best_pos:
                best, best_pos = label, m.start()
        if best:
            return best
    return APP_CAT.get(app.lower(), app)


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
    hour_apps = {}  # "HH" -> {app: dur}
    hour_titles = {}  # "HH" -> {title: dur}
    proj_agg = {}  # bucket -> dur (day total)
    hour_proj = {}  # "HH" -> {bucket: dur}
    matchers = build_bucket_matchers()
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
            b = bucket_for(app, title, matchers)
            proj_agg[b] = proj_agg.get(b, 0.0) + dur
            # split the event across the local hours it spans
            ts = parse_ts(e.get("timestamp"))
            if ts is not None and dur > 0:
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=dt.timezone.utc)
                t0, left = ts.astimezone(LOCAL_TZ), dur
                while left > 0:
                    hh = t0.replace(minute=0, second=0, microsecond=0)
                    nxt = hh + dt.timedelta(hours=1)
                    seg = min(left, (nxt - t0).total_seconds())
                    key = f"{t0.hour:02d}"
                    ha = hour_apps.setdefault(key, {})
                    ha[app] = ha.get(app, 0.0) + seg
                    pb = hour_proj.setdefault(key, {})
                    pb[b] = pb.get(b, 0.0) + seg
                    if title:
                        ht = hour_titles.setdefault(key, {})
                        ht[title] = ht.get(title, 0.0) + seg
                    left -= seg
                    t0 = nxt
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
    # hourly view for the dashboard: top apps, titles and project buckets per hour
    hour_out = {}
    for key in sorted(hour_apps):
        apps = sorted(hour_apps[key].items(), key=lambda kv: -kv[1])[:5]
        tl = sorted(hour_titles.get(key, {}).items(), key=lambda kv: -kv[1])[:5]
        pj = sorted(hour_proj.get(key, {}).items(), key=lambda kv: -kv[1])[:8]
        hour_out[key] = {
            "apps": {a: round(d) for a, d in apps},
            "titles": [{"title": t[:100], "duration": round(d)} for t, d in tl],
            "proj": {p: round(d) for p, d in pj},
        }
    out["hours"] = hour_out
    out["projects"] = {p: round(v) for p, v in sorted(proj_agg.items(), key=lambda kv: -kv[1])[:20]}
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


def add_tokens(s, model, u):
    """Accumulate per-model token usage on a session record."""
    if not isinstance(u, dict) or not model:
        return
    t = s.setdefault("tokens", {}).setdefault(model, {"in": 0, "out": 0, "cache_r": 0, "cache_w": 0, "msgs": 0})
    t["in"] += u.get("input_tokens", 0) or u.get("input", 0) or 0
    t["out"] += u.get("output_tokens", 0) or u.get("output", 0) or 0
    t["cache_r"] += u.get("cache_read_input_tokens", 0) or u.get("cacheRead", 0) or 0
    t["cache_w"] += u.get("cache_creation_input_tokens", 0) or u.get("cacheWrite", 0) or 0
    t["msgs"] += 1


def turn_ok(txt):
    """Filter system-injected pseudo user messages from the turn list."""
    return not txt.startswith(("<command", "<local-command", "Caveat:", "[Request interrupted"))


def dedup_turns(turns, cap=12):
    """Drop consecutive/resume duplicates, then cap."""
    seen, out = set(), []
    for t in turns:
        k = (t["t"], t["x"])
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out[:cap]


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
             "first": None, "last": None, "turns": []}
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
                                if turn_ok(txt) and ts is not None:
                                    s["turns"].append({
                                        "t": ts.astimezone(LOCAL_TZ).strftime("%H:%M"),
                                        "x": txt.replace("\n", " ")[:160],
                                    })
                    elif t == "assistant":
                        s["assistant_msgs"] += 1
                        msg = d.get("message") or {}
                        add_tokens(s, msg.get("model"), msg.get("usage"))
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
            s["turns"] = dedup_turns(s["turns"])
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
             "first": None, "last": None, "turns": []}
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
                            if turn_ok(txt) and ts is not None:
                                s["turns"].append({
                                    "t": ts.astimezone(LOCAL_TZ).strftime("%H:%M"),
                                    "x": txt.replace("\n", " ")[:160],
                                })
                        elif role == "assistant" and msg.get("content"):
                            s["assistant_msgs"] += 1
                            add_tokens(s, msg.get("model"), msg.get("usage"))
                            if isinstance(msg.get("content"), list):
                                for part in msg["content"]:
                                    if not (isinstance(part, dict) and part.get("type") == "toolCall"):
                                        continue
                                    name = part.get("name") or "?"
                                    s["tools"][name] = s["tools"].get(name, 0) + 1
                                    args = part.get("arguments") or {}
                                    for key in ("path", "file_path", "notebook_path"):
                                        p = args.get(key)
                                        if isinstance(p, str):
                                            s["files"].add(p)
        except OSError:
            continue
        if s["first"] is not None:
            s["span"] = round((s["last"] - s["first"]).total_seconds())
            s["files"] = sorted(s["files"])[:15]
            s["turns"] = dedup_turns(s["turns"])
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
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
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


# ---------------------------------------------------------------- github

def gh_run(args, timeout=30):
    gh = CFG.get("gh_bin", "gh")
    try:
        r = subprocess.run([gh, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    return r if r.returncode == 0 else None


def collect_github(start, end):
    """PR reviews/comments/creates/merges done by the user on github.com."""
    out = {"reviews": [], "comments": [], "prs_created": [], "prs_merged": []}
    r = gh_run(["auth", "status", "-h", "github.com"])
    blob = ((r.stdout or "") + (r.stderr or "")) if r else ""
    if "Logged in" not in blob:
        return {"error": "gh 未登录,跳过 GitHub 采集", **out}

    gql = (
        "query($start:DateTime!,$end:DateTime!){viewer{login "
        "contributionsCollection(from:$start,to:$end)"
        "{pullRequestReviewContributions(first:50){nodes{occurredAt pullRequest{title url}}}} "
        "issueComments(first:100,orderBy:{field:UPDATED_AT,direction:DESC})"
        "{nodes{createdAt url body}}}}"
    )
    r = gh_run(["api", "graphql", "-f", f"query={gql}",
                "-F", f"start={start.astimezone(dt.timezone.utc).isoformat()}",
                "-F", f"end={end.astimezone(dt.timezone.utc).isoformat()}"])
    if r:
        try:
            viewer = json.loads(r.stdout)["data"]["viewer"]
        except (json.JSONDecodeError, KeyError):
            viewer = {}
        for n in viewer.get("contributionsCollection", {}).get("pullRequestReviewContributions", {}).get("nodes", []):
            ts = parse_ts(n.get("occurredAt"))
            if in_day(ts, start, end):
                pr = n.get("pullRequest") or {}
                out["reviews"].append({
                    "time": ts.astimezone(LOCAL_TZ).strftime("%H:%M"),
                    "url": pr.get("url", ""), "state": "",
                    "pr": pr.get("title", "")[:100]})
        for n in viewer.get("issueComments", {}).get("nodes", []):
            ts = parse_ts(n.get("createdAt"))
            if in_day(ts, start, end):
                body = re.sub(r"\s+", " ", (n.get("body") or "")).strip()
                out["comments"].append({
                    "time": ts.astimezone(LOCAL_TZ).strftime("%H:%M"),
                    "url": n.get("url", ""), "text": body[:120]})

    d = start.date().isoformat()
    for key, flags in (("prs_created", ["--author", "@me", "--created", d]),
                       ("prs_merged", ["--merged-by", "@me", "--merged", d])):
        r = gh_run(["search", "prs", *flags, "--limit", "30",
                    "--json", "repository,title,url,state,author"])
        if not r:
            continue
        try:
            for n in json.loads(r.stdout):
                out[key].append({
                    "repo": (n.get("repository") or {}).get("nameWithOwner", ""),
                    "title": n.get("title", "")[:100], "url": n.get("url", ""),
                    "state": n.get("state", ""),
                    "author": (n.get("author") or {}).get("login", "")})
        except json.JSONDecodeError:
            continue
    return out


# ---------------------------------------------------------------- screenpipe

def sp_hhmm(ts):
    """ScreenPipe timestamp -> local HH:MM string, or ''."""
    t = parse_ts(re.sub(r"\.\d+", "", ts or ""))
    return f"{t.astimezone(LOCAL_TZ):%H:%M}" if t else ""


def _builtin_captures(start, end):
    """(timestamp, device, text) rows from the web app's built-in sampler
    (webapp/backend/app/capture.py), read straight from its SQLite store."""
    import sqlite3
    web = osutil.load_config(BASE / "webapp", "config.json")
    path = Path(os.environ.get("JOURNAL_WEB_DB") or web.get("db_path") or DATA / "webapp.db")
    if not path.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
        rows = con.execute("SELECT captured_at, monitor, text FROM screen_captures WHERE captured_at >= ? "
                           "AND captured_at < ? AND text IS NOT NULL",
                           (start.astimezone(dt.timezone.utc).isoformat(),
                            end.astimezone(dt.timezone.utc).isoformat())).fetchall()
        con.close()
    except sqlite3.Error:
        return []
    return [(ts, f"monitor {m}", t) for ts, m, t in rows]


def collect_screenpipe(start, end):
    out = {"frames": 0, "total": 0, "unique": 0, "span": "", "devices": {}, "samples": []}
    builtin = _builtin_captures(start, end)
    if not CFG.get("sp_url"):
        if not builtin:
            return {"error": "screen OCR off (no sp_url, no built-in captures)", **out}
        return _summarize_screen(out, [{"content": {"timestamp": ts, "device_name": dev, "text": t}}
                                       for ts, dev, t in builtin], len(builtin))
    tok_file = CFG.get("sp_token_file")
    headers = {}
    try:
        token = Path(tok_file).read_text(encoding="utf-8").strip()
        headers["Authorization"] = f"Bearer {token}"
    except Exception:
        return {"error": "no screenpipe token", **out}
    base = CFG["sp_url"].rstrip("/")
    limit = 500
    offset = 0
    total = 0
    items = []
    max_frames = CFG.get("sp_max_frames", 5000)
    # 不带 content_type 时,服务端每页返回数约为 limit 的一半。必须显式指定。
    # 单页拿不全,按 pagination.total 翻页。
    while offset < max_frames:
        q = "?" + urllib.parse.urlencode({
            "query": " ", "limit": limit, "offset": offset,
            "content_type": "ocr",
            "start_time": start.isoformat(), "end_time": end.isoformat(),
        })
        try:
            d = http_json(f"{base}/search{q}", headers=headers, timeout=20)
        except Exception as e:
            if not items:
                return {"error": f"screenpipe: {e}", **out}
            break
        page = d.get("data") or []
        items += page
        offset += len(page)
        total = (d.get("pagination") or {}).get("total") or 0
        if not page or (total and offset >= total):
            break
    items += [{"content": {"timestamp": ts, "device_name": dev, "text": t}} for ts, dev, t in builtin]
    return _summarize_screen(out, items, total + len(builtin))


def _summarize_screen(out, items, total):
    out["frames"] = len(items)
    out["total"] = total
    texts = []
    for it in items:
        c = it.get("content") or {}
        dev = c.get("device_name") or "?"
        out["devices"][dev] = out["devices"].get(dev, 0) + 1
        t = (c.get("text") or "").strip()
        if t:
            texts.append((c.get("timestamp") or "", t))
    texts.sort()
    # 全文去重。静止屏幕会重复采到相同文本。
    seen = set()
    uniq = []
    for ts, t in texts:
        key = re.sub(r"\s+", " ", t)
        if key in seen:
            continue
        seen.add(key)
        uniq.append((ts, t))
    out["unique"] = len(uniq)
    if uniq:
        first, last = sp_hhmm(uniq[0][0]), sp_hhmm(uniq[-1][0])
        if first and last:
            out["span"] = f"{first}-{last}"
    n = CFG.get("sp_samples", 40)
    chars = CFG.get("sp_sample_chars", 400)
    step = max(1, len(uniq) // n)
    out["samples"] = [t[:chars] for _, t in uniq[::step]][:n]
    return out


# ---------------------------------------------------------------- digest writer

def agent_summary(sessions):
    """Merge claude+pi per project for compactness."""
    by_proj = {}
    for s in sessions:
        proj = s.get("cwd") or "(unknown)"
        proj = osutil.short_home(proj)
        p = by_proj.setdefault(proj, {"projects": proj, "sessions": []})
        p["sessions"].append(s)
    return by_proj


def write_digest(d, data):
    lines = []
    lines.append(f"# 原始活动数据 {d.isoformat()}")
    lines.append(f"采集截止: {data.get('generated_at', '')}。这是已采集记录的快照，不能证明全天覆盖或本人手工完成。")
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
                if s.get("native_parent_id"):
                    lines.append(f"  原生子会话，父ID: {s['native_parent_id']}；不得与主任务重复计成果")
                if s.get("forked_from_id"):
                    lines.append(f"  fork/继承背景: {s['forked_from_id']}；不是子任务完成证明")
                # Include outcomes, not only the opening request and tool counts.
                # Full records remain in day JSON; the digest is a bounded sample.
                turns = s.get("turns") or []
                for role, label in (("user", "用户侧反馈/要求"), ("assistant", "Agent结果说明（自述）"), ("tool", "工具返回记录")):
                    selected = [t for t in turns if t.get("role") == role]
                    # Reading a previously generated diary is context, not a
                    # new execution result. Avoid feeding stale report claims
                    # back into the next report as if they were fresh outcomes.
                    if role == "tool":
                        selected = [t for t in selected if not re.search(r"#{1,2}\s*工作日报\s+\d{4}-\d{2}-\d{2}", str(t.get("x") or ""))]
                    for t in selected[-(4 if role == "tool" else 3):]:
                        text = re.sub(r"\s+", " ", str(t.get("x") or ""))
                        excerpt = text if len(text) <= 500 else text[:230] + " [中段省略] " + text[-230:]
                        lines.append(f"  {label} {t.get('t','')}: {excerpt}")
    else:
        lines.append("- 无")
    lines.append("")
    lines.append("## 3. Git 提交")
    if data["git"]:
        for r in data["git"]:
            repo = osutil.short_home(r["repo"])
            lines.append(f"### {repo} ({len(r['commits'])} commits)")
            for c in r["commits"]:
                lines.append(f"- {c['time']} {c['hash']} {c['subject']}")
    else:
        lines.append("- 无")
    lines.append("")
    lines.append("## 4. GitHub PR 活动")
    gh = data.get("github") or {}
    if gh.get("error"):
        lines.append(f"- {gh['error']}")
    else:
        def gh_list(items, fmt):
            for i in items[:20]:
                lines.append(f"- {fmt(i)}")
        if gh.get("reviews"):
            lines.append("提交的 review:")
            gh_list(gh["reviews"], lambda i: f"{i['time']} [{i['state']}] {i['pr']} {i['url']}")
        if gh.get("comments"):
            lines.append("评论:")
            gh_list(gh["comments"], lambda i: f"{i['time']} {i['text']} {i['url']}")
        if gh.get("prs_created"):
            lines.append("创建的 PR:")
            gh_list(gh["prs_created"], lambda i: f"{i['repo']} {i['title']} [{i['state']}] {i['url']}")
        if gh.get("prs_merged"):
            lines.append("合并的 PR:")
            gh_list(gh["prs_merged"], lambda i: f"{i['repo']} {i['title']} (作者 {i['author']}) {i['url']}")
        if not any(gh.get(k) for k in ("reviews", "comments", "prs_created", "prs_merged")):
            lines.append("- 当天无 PR 活动")
    sp = data["screenpipe"]
    lines.append("")
    lines.append("## 5. 屏幕 OCR 采样")
    if sp.get("error"):
        lines.append(f"- {sp['error']}")
    else:
        total = sp.get("total") or sp["frames"]
        lines.append(f"- 采集帧数: {sp['frames']}/{total}, 去重文本 {sp.get('unique', 0)} 条, "
                     f"覆盖时段 {sp.get('span') or '无'}, 设备: {sp.get('devices')}")
        for i, t in enumerate(sp.get("samples", []), 1):
            clean = re.sub(r"\s+", " ", t)
            lines.append(f"- 样本{i}: {clean}")
    fs = data.get("feishu") or {}
    lines.append("")
    lines.append("## 6. 飞书协作")
    if fs.get("error"):
        lines.append(f"- {fs['error']}")
    elif not fs.get("chats"):
        lines.append("- 当天无可见会话消息")
    else:
        for c in fs["chats"]:
            kind = "私聊" if c.get("mode") == "p2p" else "群"
            lines.append(f"### {kind} {c['chat']} — {c['total']} 条消息(我发 {c['mine']} 条)"
                         + (f", 分享文档链接 {c['doc_links']} 次" if c.get("doc_links") else ""))
            for ex in c["excerpts"]:
                lines.append(f"- {ex}")
        if fs.get("files"):
            lines.append("")
            lines.append("收到的文件:")
            for f in fs["files"][:8]:
                lines.append(f"- {f.get('time','')} [{f.get('chat','')}] {f.get('name')} — {f.get('preview','')[:400]}")
    return "\n".join(lines) + "\n"


def main():
    d = parse_date(sys.argv[1] if len(sys.argv) > 1 else None)
    start, end = bounds(d)
    data = {
        "date": d.isoformat(),
        "generated_at": dt.datetime.now(LOCAL_TZ).isoformat(),
        "activitywatch": collect_activitywatch(start, end),
        "agent_sessions": (collect_claude(start, end) + collect_pi(start, end)
                           + agent_sources.collect(start, end)),
        "git": collect_git(start, end),
        "github": collect_github(start, end),
        "screenpipe": collect_screenpipe(start, end),
        "feishu": collect_feishu.collect(start, end),
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
