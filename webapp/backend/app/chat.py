"""问 AI: a tool-using chat over everything the journal knows.

Progressive disclosure. The model starts from a one-screen catalog in the system
prompt and drills down with tools. Every tool returns a bounded page plus a hint
naming the next, more detailed call:

    L0 catalog    today, data coverage, projects with 30-day time, sources   (system prompt)
    L1 overview   list_projects · range_summary · get_day                    (numbers, chapters)
    L2 rows       search · find_sessions · get_windows · list_commits ·
                  screen_text · feishu · list_reports                        (rows with ids)
    L3 full text  read_session · read_report                                 (paged)

Conversations are stored as raw OpenAI messages (chat_threads / chat_messages).
Only the last two turns are replayed with their tool results; older tool output
is replaced by a stub, so long threads stay cheap.
"""
import datetime as dt
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config, db, projects, reports, sessions_view
from .timeline import _is_prompt
from .timeutil import now_iso, parse_ts, tz

MAX_ROUNDS = 10
RESULT_CHARS = 14000
KEEP_TURNS = 2
TIMEOUT = 120
SP_DATA = Path.home() / ".screenpipe" / "data"


def available() -> bool:
    return bool(config.LLM_API_KEY and config.LLM_BASE_URL and not _disabled())


def _disabled() -> bool:
    import os
    return bool(os.environ.get("JOURNAL_LLM_DISABLE"))


def model() -> str:
    return config.LLM_CLASSIFY.get("chat_model") or config.LLM_CLASSIFY["model"]


# ---------------------------------------------------------------- helpers

def _hm(sec) -> str:
    m = int(round((sec or 0) / 60))
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def _clock(iso, fmt="%H:%M") -> str:
    t = parse_ts(iso) if isinstance(iso, str) else iso
    return t.astimezone(tz()).strftime(fmt) if t else ""


def _day(s, name="day") -> str:
    try:
        return dt.date.fromisoformat(str(s)[:10]).isoformat()
    except ValueError:
        raise ValueError(f"{name} 需要 YYYY-MM-DD 格式,收到 {s!r}")


def _known_days() -> list:
    return [r["day_key"] for r in db.q(
        "SELECT DISTINCT day_key FROM source_events WHERE day_key IS NOT NULL ORDER BY day_key")]


def _range(start, end, default_days=30, cap=92):
    """(start, end) day strings → list of known days in between, newest last."""
    known = _known_days()
    e = _day(end, "end") if end else (known[-1] if known else dt.date.today().isoformat())
    s = _day(start, "start") if start else (dt.date.fromisoformat(e) - dt.timedelta(days=default_days - 1)).isoformat()
    if s > e:
        s, e = e, s
    days = [d for d in known if s <= d <= e]
    return s, e, days[-cap:]


def _api():
    from . import api   # lazy: api imports this module
    return api


def _snip(text: str, q: str = None, n: int = 240) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if q:
        i = text.lower().find(q.lower())
        if i > n // 2:
            text = "…" + text[i - n // 3:]
    return text[:n] + ("…" if len(text) > n else "")


# ---------------------------------------------------------------- tools (L1)

def list_projects(days: int = 30):
    a = _api()
    stats = a.project_stats(days)["projects"]
    out = []
    for r in db.q("SELECT name, priority, goals_json, notes, rules_json FROM projects WHERE active=1"):
        rules = projects.parse_rules(r["rules_json"])
        if rules.get("ignore"):
            continue
        s = stats.get(r["name"], {})
        out.append({"project": r["name"], "priority": r["priority"], "goal": r["goals_json"] or None, "notes": r["notes"] or None,
                    "parent": rules.get("parent"),
                    "aliases": rules.get("aliases", [])[:6], "paths": rules.get("paths", [])[:3],
                    "you": _hm(s.get("human_sec")), "agent": _hm(s.get("agent_sec")),
                    "sessions": s.get("sessions", 0), "commits": s.get("commits", 0),
                    "last_active": s.get("last_active"), "_sec": s.get("human_sec", 0)})
    out.sort(key=lambda x: -x.pop("_sec"))
    return {"summary": f"{len(out)} 个项目", "window_days": days, "projects": out,
            "next": "range_summary(project=…) 看每天投入;find_sessions(project=…) 看做过的会话"}


def range_summary(start=None, end=None, project=None):
    a = _api()
    s, e, days = _range(start, end)
    rows, tot, tot_agent = [], {}, {}
    for d in days:
        st = a._day_stats(d)
        al = st["allocation"]
        for k, v in al.items():
            tot[k] = tot.get(k, 0) + v
        for k, v in st["agent_allocation"].items():
            tot_agent[k] = tot_agent.get(k, 0) + v
        if project:
            rows.append({"day": d, "observed": st["has_observation"], "you": _hm(al.get(project, 0)),
                         "agent": _hm(st["agent_allocation"].get(project, 0)),
                         "sessions": st["sessions_by_project"].get(project, 0),
                         "commits": st["commits_by_project"].get(project, 0)})
        else:
            top = sorted(al.items(), key=lambda kv: -kv[1])[:3]
            rows.append({"day": d, "weekday": dt.date.fromisoformat(d).isoweekday(),
                         "observed": st["has_observation"], "you": _hm(st["human_sec"]),
                         "agent": _hm(st["agent_sec"]), "prompts": st["prompts"], "commits": st["commits"],
                         "top": ", ".join(f"{k} {_hm(v)}" for k, v in top)})
    by = sorted(tot.items(), key=lambda kv: -kv[1])
    return {"summary": f"{s}~{e} · {len(days)} 天有数据", "start": s, "end": e,
            "totals_you": {k: _hm(v) for k, v in by[:12]},
            "totals_agent": {k: _hm(v) for k, v in sorted(tot_agent.items(), key=lambda kv: -kv[1])[:12]},
            "days": rows, "next": "get_day(day) 看某一天的章节与会话"}


def get_day(day):
    a = _api()
    day = _day(day)
    t = a.cached_timeline(day)
    st = a._day_stats(day)
    other = _other_sources(day)
    if not t["has_observation"] and not t["agent"]:
        return {"summary": f"{day} 无看板数据", "day": day, "note": "这一天没有窗口记录也没有 agent 会话", **other}
    chapters = []
    for e in t["episodes"]:
        ch = {"time": f"{_clock(e['start'])}–{_clock(e['end'])}", "label": e["label"],
              "you": _hm(e["human_sec"]), "summary": _snip(e.get("summary"), n=90),
              "mix": ", ".join(f"{m['label']} {_hm(m['sec'])}" for m in e["mix"][:3])}
        if e["n_prompts"]:
            ch["prompts"] = [f"{_clock(p['at'])} #{p['session_id']} {_snip(p['text'], n=90)}" for p in e["prompts"][:4]]
            ch["n_prompts"] = e["n_prompts"]
        if e["commits"]:
            ch["commits"] = [_snip(c.get("label") or c.get("subject") or "", n=80) for c in e["commits"][:4]]
        if e["sessions"]:
            ch["sessions"] = [f"#{x['id']} {x['agent']} {x['project']} {_hm(x['exec_sec'])}" for x in e["sessions"][:5]]
        chapters.append(ch)
    s0, s1 = sessions_view.day_range(day)
    sess = sessions_view.list_sessions(s0, s1, limit=40)
    commits = a._day_commits(day)
    return {
        "summary": f"{day} · 你 {_hm(st['human_sec'])} · agent {_hm(st['agent_sec'])} · {len(chapters)} 个章节",
        "day": day, "observed_windows": t["has_observation"],
        "you": _hm(st["human_sec"]), "agent_exec": _hm(st["agent_sec"]), "agent_peak_parallel": st["agent_peak"],
        "prompts": st["prompts"], "commits": len(commits),
        "your_time_by_project": {k: _hm(v) for k, v in sorted(st["allocation"].items(), key=lambda kv: -kv[1])},
        "agent_time_by_project": {k: _hm(v) for k, v in sorted(st["agent_allocation"].items(), key=lambda kv: -kv[1])},
        "longest_focus": st["longest"] and {**st["longest"], "start": _clock(st["longest"]["start"])},
        "chapters": chapters,
        "sessions": [_session_row(r) for r in sess],
        "commit_list": [f"{_clock(c['at'])} {c['project']} {c['subject']}" for c in commits[:30]],
        **other,
        "next": "get_windows(day, start, end) 看分钟级窗口;read_session(id) 看会话原文;screen_text 看屏幕 OCR",
    }


def _other_sources(day: str) -> dict:
    """Pointers to the same day in other sources, so the model knows they exist."""
    out = {}
    if (reports.REPORTS / f"{day}.md").exists():
        out["daily_report"] = f"有日报,read_report('daily', '{day}')"
    f = config.REPO_ROOT / "data" / f"{day}.json"
    if f.exists():
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            fs = d.get("feishu") or {}
            if fs.get("chats") or fs.get("files"):
                out["feishu"] = f"{len(fs.get('chats', []))} 个飞书会话、{len(fs.get('files', []))} 个文件,feishu('{day}')"
            aw = d.get("activitywatch") or {}
            if not aw.get("active_time") and aw.get("afk_time"):
                out["activitywatch"] = f"当天电脑基本处于离开状态(AFK {_hm(aw['afk_time'])}),不是采集故障"
        except ValueError:
            pass
    return out


# ---------------------------------------------------------------- tools (L2)

def _session_row(r) -> dict:
    out = {"id": r["id"], "agent": r["agent"], "project": r["project"],
           "start": _clock(r["start_at"], "%m-%d %H:%M"), "exec": _hm(r["exec_sec"]),
           "prompts": r["prompts"], "tools": r["tools"], "title": _snip(r["title"], n=100)}
    if r.get("children"):
        out["subagents"] = len(r["children"])
    return out


def find_sessions(query=None, start=None, end=None, project=None, agent=None, limit=20):
    limit = max(1, min(int(limit or 20), 60))
    s0 = s1 = None
    if start or end:
        s, e, _ = _range(start, end)
        s0, _ = sessions_view.day_range(s)
        _, s1 = sessions_view.day_range(e)
    rows = sessions_view.list_sessions(s0, s1, agent, project, query, False, limit)
    return {"summary": f"{len(rows)} 个会话", "sessions": [_session_row(r) for r in rows],
            "next": "read_session(id) 看你的指令;mode='full' 连 agent 回复一起看"}


def get_windows(day, start=None, end=None, project=None, query=None, limit=120):
    """Window-by-window log (ActivityWatch). Consecutive repeats are merged."""
    a = _api()
    day = _day(day)
    limit = max(1, min(int(limit or 120), 300))
    ws = a.cached_timeline(day)["windows"]
    lo = f"{day}T{start}" if start else None
    hi = f"{day}T{end}" if end else None
    rows = []
    for w in ws:
        t0 = _clock(w["start"], "%Y-%m-%dT%H:%M")
        if lo and t0 < lo[:16] or hi and t0 > hi[:16]:
            continue
        if project and w["project"] != project:
            continue
        if query and query.lower() not in (w["title"] + " " + w["app"]).lower():
            continue
        sec = (parse_ts(w["end"]) - parse_ts(w["start"])).total_seconds()
        prev = rows[-1] if rows else None
        if prev and prev["_k"] == (w["app"], w["title"]):
            prev["_sec"] += sec
            prev["end"] = _clock(w["end"], "%H:%M:%S")
            continue
        rows.append({"_k": (w["app"], w["title"]), "_sec": sec, "start": _clock(w["start"], "%H:%M:%S"),
                     "end": _clock(w["end"], "%H:%M:%S"), "app": w["app"].split(".")[-1],
                     "title": _snip(w["title"], n=110), "project": w["project"]})
    more = len(rows) > limit
    out = [f"{r['start']}–{r['end']} ({_hm(r['_sec'])}) [{r['project']}] {r['app']} · {r['title']}"
           for r in rows[:limit]]
    return {"summary": f"{len(out)} 段窗口" + (" (已截断)" if more else ""), "day": day, "windows": out,
            **({"next": "缩小 start/end 或加 project/query 过滤以看剩余部分"} if more else {})}


def list_commits(start=None, end=None, project=None, query=None, limit=60):
    a = _api()
    s, e, days = _range(start, end)
    rows = []
    for d in days:
        for c in a._day_commits(d):
            if project and c["project"] != project:
                continue
            if query and query.lower() not in c["subject"].lower():
                continue
            rows.append(f"{_clock(c['at'], '%m-%d %H:%M')} [{c['project']}] {c['repo']}@{c['sha'][:7]} {c['subject']}")
    limit = max(1, min(int(limit or 60), 200))
    return {"summary": f"{len(rows)} 个提交 ({s}~{e})", "commits": rows[-limit:],
            **({"note": f"只显示最近 {limit} 个"} if len(rows) > limit else {})}


def _sp_get(params: dict) -> dict:
    token = Path(config.SP_TOKEN_FILE).read_text(encoding="utf-8").strip() if config.SP_TOKEN_FILE else ""
    url = f"{config.SP_URL}/search?" + urllib.parse.urlencode({"content_type": "ocr", **params})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


_SP_COVER = {"at": 0, "v": None}


def _sp_coverage() -> dict:
    if time.time() - _SP_COVER["at"] < 600 and _SP_COVER["v"]:
        return _SP_COVER["v"]
    d = _sp_get({"limit": 1})
    total = (d.get("pagination") or {}).get("total") or 0
    last = (d.get("data") or [{}])[0].get("content", {}).get("timestamp")
    first = None
    if total:
        f = _sp_get({"limit": 1, "offset": total - 1})
        first = (f.get("data") or [{}])[0].get("content", {}).get("timestamp")
    v = {"frames": total, "first": _clock(first, "%Y-%m-%d %H:%M") if first else None,
         "last": _clock(last, "%Y-%m-%d %H:%M") if last else None}
    _SP_COVER.update(at=time.time(), v=v)
    return v


_SP_STATUS = {"at": 0, "v": None, "restarted_at": 0}
SP_LOG_DIR = Path.home() / ".screenpipe"


def _sp_health() -> dict:
    token = Path(config.SP_TOKEN_FILE).read_text(encoding="utf-8").strip() if config.SP_TOKEN_FILE else ""
    req = urllib.request.Request(f"{config.SP_URL}/health", headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:   # 503 = degraded, body still has the details
        return json.load(e)


def _sp_log_cause() -> str:
    """The capture error screenpipe keeps logging, if it is the Wayland portal one."""
    logs = sorted(SP_LOG_DIR.glob("screenpipe.*.log"))
    if not logs:
        return ""
    with open(logs[-1], "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 400_000))
        tail = f.read().decode("utf-8", "replace")
    return "wayland" if "Wayland screen-share approval" in tail else ""


def screen_status() -> dict:
    """Is screenpipe still capturing? ok | stalled | down | off, plus a fix hint."""
    if not config.SP_URL:
        return {"state": "off"}
    # short cache while broken, so an approval shows up quickly
    ttl = 60 if (_SP_STATUS["v"] or {}).get("state") == "ok" else 8
    if time.time() - _SP_STATUS["at"] < ttl and _SP_STATUS["v"]:
        return _SP_STATUS["v"]
    try:
        h = _sp_health()
    except Exception as e:  # noqa: BLE001 — connection refused etc.
        if time.time() - _SP_STATUS["restarted_at"] < 120:
            v = {"state": "stalled", "awaiting_approval": True, "restartable": bool(config.SP_SERVICE),
                 "reason": "正在重启 screenpipe,马上会弹出「屏幕共享」对话框",
                 "hint": "对话框可能在别的窗口后面或另一块屏上,按 Super 键在概览里找"}
            _SP_STATUS.update(at=time.time(), v=v)
            return v
        v = {"state": "down", "reason": f"screenpipe 没有运行({type(e).__name__})",
             "hint": "启动:screenpipe record --use-all-monitors"}
        _SP_STATUS.update(at=time.time(), v=v)
        return v
    last = h.get("last_frame_timestamp")
    lt = dt.datetime.fromisoformat(last) if last else None
    age = (dt.datetime.now(dt.timezone.utc) - lt).total_seconds() if lt else None
    pipe = h.get("pipeline") or {}
    v = {"state": "ok", "last_frame": lt.astimezone(tz()).isoformat() if lt else None,
         "monitors": h.get("monitors") or [], "drop_rate": pipe.get("frame_drop_rate")}
    v["restartable"] = bool(config.SP_SERVICE)
    since_restart = time.time() - _SP_STATUS["restarted_at"]
    if (lt is None or lt.timestamp() < _SP_STATUS["restarted_at"]) and since_restart < 15 * 60:
        # restarted from the page: waiting for the screen-share dialog to be approved
        v["state"] = "stalled"
        v["awaiting_approval"] = True
        v["reason"] = "已重启,正在等你在「屏幕共享」对话框里点允许"
        v["hint"] = "对话框可能在别的窗口后面或另一块屏上,按 Super 键在概览里找;错过了可以再点一次重启"
    elif h.get("vision_db_write_stalled") or age is None or age > 30 * 60:
        v["state"] = "stalled"
        if _sp_log_cause() == "wayland":
            v["reason"] = "Wayland 屏幕共享授权失效或没有被允许(常见于锁屏/注销后,或错过了授权对话框)"
            v["hint"] = "点「重启截屏」,然后在弹出的屏幕共享对话框里选上所有显示器并允许"
        else:
            v["reason"] = (h.get("message") or "截屏没有新帧写入")[:200]
            v["hint"] = "重启 screenpipe,并查看 ~/.screenpipe/ 下的日志"
    _SP_STATUS.update(at=time.time(), v=v)
    return v


def restart_screen() -> dict:
    """systemctl --user restart <screenpipe unit>: brings the screen-share dialog back."""
    import subprocess
    if not config.SP_SERVICE:
        return {"ok": False, "error": "没有配置 screenpipe 服务(config.json sp_service)"}
    if time.time() - _SP_STATUS["restarted_at"] < 15:
        return {"ok": False, "error": "刚刚重启过,稍等几秒"}
    r = subprocess.run(["systemctl", "--user", "restart", config.SP_SERVICE],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or r.stdout or "systemctl 失败").strip()[:300]}
    _SP_STATUS.update(at=0, v=None, restarted_at=time.time())
    return {"ok": True}


def screen_frames(day: str) -> int:
    try:
        d = _sp_get({"limit": 1, "start_time": _utc(day), "end_time": _utc(day, end=True)})
        return (d.get("pagination") or {}).get("total") or 0
    except Exception:  # noqa: BLE001
        return 0


def _utc(day: str, end=False) -> str:
    from .timeutil import day_bounds
    b = day_bounds(day)[1 if end else 0]
    return b.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def frame_url(c: dict):
    """Screenshot URL for an OCR hit: the frame's own jpg if it still exists,
    else (frame inside a compacted mp4) the video plus its frame index."""
    base = SP_DATA.resolve()
    for key, idx in (("frame_name", None), ("file_path", c.get("offset_index"))):
        p = c.get(key) or ""
        if not p or not Path(p).is_file():
            continue
        try:
            rel = Path(p).resolve().relative_to(base)
        except (ValueError, OSError):
            continue
        q = {"f": str(rel)}
        if Path(p).suffix.lower() == ".mp4":
            if idx in (None, "", "None"):
                continue
            q["i"] = int(idx)
        return "/api/v1/screen/frame?" + urllib.parse.urlencode(q)
    return None


FRAME_CACHE = Path.home() / ".cache" / "ai-work-journal" / "frames"


def frame_file(f: str, i: int = None):
    """Validated path of a screenshot under SP_DATA; mp4 frames are extracted
    once with ffmpeg and cached. None if outside SP_DATA or not an image/video."""
    import hashlib
    import subprocess
    base = SP_DATA.resolve()
    path = (base / f).resolve()
    if base not in path.parents or not path.is_file():
        return None
    ext = path.suffix.lower()
    if ext in (".jpg", ".jpeg", ".png") and i is None:
        return path
    if ext != ".mp4" or i is None or i < 0:
        return None
    FRAME_CACHE.mkdir(parents=True, exist_ok=True)
    out = FRAME_CACHE / (hashlib.sha1(f"{path}:{i}".encode()).hexdigest()[:20] + ".jpg")
    if not out.exists():
        tmp = out.with_suffix(".part.jpg")
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-vf", f"select=eq(n\\,{int(i)})",
                            "-vframes", "1", "-q:v", "3", str(tmp)],
                           capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
        if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            tmp.unlink(missing_ok=True)
            return None
        tmp.rename(out)
    return out


def screen_text(query=None, start=None, end=None, limit=8, offset=0):
    """ScreenPipe OCR frames, newest first."""
    limit = max(1, min(int(limit or 8), 20))
    p = {"limit": limit, "offset": max(0, int(offset or 0))}
    if query:
        p["q"] = query
    if start:
        p["start_time"] = _utc(_day(start, "start"))
    if end:
        p["end_time"] = _utc(_day(end, "end"), end=True)
    try:
        d = _sp_get(p)
        cover = _sp_coverage()
    except Exception as e:
        return {"summary": "屏幕 OCR 不可用", "error": f"screenpipe: {e}"}
    total = (d.get("pagination") or {}).get("total") or 0
    frames = []
    for it in d.get("data") or []:
        c = it.get("content") or {}
        frames.append({"time": _clock(c.get("timestamp"), "%m-%d %H:%M:%S"),
                       "window": " · ".join(x for x in (c.get("app_name"), c.get("window_name")) if x) or None,
                       "text": _snip(c.get("text"), query, 360),
                       "image": frame_url(c)})
    nxt = p["offset"] + len(frames)
    return {"summary": f"{total} 帧命中", "total": total, "coverage": cover, "frames": frames,
            "note": "OCR 是英文模型识别的,中文多为乱码;以英文词、文件名、网址为准。image 是截图地址,可用 ![](image) 嵌入回答",
            **({"next_offset": nxt} if nxt < total else {})}


def feishu(day):
    day = _day(day)
    f = config.REPO_ROOT / "data" / f"{day}.json"
    try:
        d = json.loads(f.read_text(encoding="utf-8")).get("feishu") or {}
    except (OSError, ValueError):
        return {"summary": "无飞书记录", "day": day, "note": "这一天没有采集文件(日报流程每天 23:00 才采集)"}
    chats = [{"chat": c["chat"], "mode": c.get("mode"), "messages": c.get("total"), "mine": c.get("mine"),
              "excerpts": c.get("excerpts", [])[:12]} for c in d.get("chats", [])]
    files = [{"name": x.get("name"), "from": x.get("from"), "chat": x.get("chat"), "time": x.get("time"),
              "preview": _snip(re.sub(r"<[^>]+>", " ", x.get("preview") or ""), n=300)} for x in d.get("files", [])]
    return {"summary": f"{len(chats)} 个会话 · {len(files)} 个文件", "day": day, "chats": chats, "files": files}


def list_reports(kind="daily"):
    r = reports.list_reports()
    rows = [{"name": x["name"], "title": x["title"], "feishu": x["feishu_url"], "fallback": x["fallback"]}
            for x in r.get(kind if kind in ("daily", "weekly") else "daily", [])]
    return {"summary": f"{len(rows)} 篇", "reports": rows, "next": "read_report(kind, name)"}


# ---------------------------------------------------------------- tools (L3)

def read_session(id, mode="prompts", offset=0, limit=40):
    sid = int(id)
    r = db.q1("SELECT * FROM agent_sessions WHERE id=?", (sid,))
    if not r:
        return {"summary": "无此会话", "error": f"no session #{sid}"}
    head = sessions_view.summarize(r)
    evs = db.q("SELECT e.event_type, e.occurred_at, e.payload FROM session_event_links l "
               "JOIN source_events e ON e.id = l.event_id WHERE l.session_id=? ORDER BY l.ordinal", (sid,))
    full = mode == "full"
    items = []
    for e in evs:
        p = json.loads(e["payload"])
        text = (p.get("text") or "").strip()
        at = _clock(e["occurred_at"], "%m-%d %H:%M")
        if e["event_type"] == "msg_user":
            if _is_prompt(text) and not head["subagent"]:
                items.append(f"{at} 你: {_snip(text, n=1500 if full else 600)}")
            elif full and text:
                items.append(f"{at} [注入上下文] {_snip(text, n=160)}")
        elif full and e["event_type"] == "msg_assistant":
            tools = [x.get("name") for x in (p.get("parts") or []) if x.get("kind") == "tool_call"]
            if text:
                items.append(f"{at} agent: {_snip(text, n=700)}")
            if tools:
                items.append(f"{at} 工具: {', '.join(tools[:8])}" + (f" 等 {len(tools)} 个" if len(tools) > 8 else ""))
    offset, limit = max(0, int(offset or 0)), max(1, min(int(limit or 40), 120))
    page = items[offset:offset + limit]
    out = {"summary": f"#{sid} {len(page)}/{len(items)} 条", "session": _session_row({**head, "children": []}),
           "cwd": head["cwd"], "end": _clock(head["end_at"], "%m-%d %H:%M"), "models": head["models"],
           "mode": "full" if full else "prompts", "total": len(items), "items": page}
    if offset + limit < len(items):
        out["next_offset"] = offset + limit
    if not full:
        out["next"] = "mode='full' 可看 agent 的回复与工具调用"
    return out


def read_report(kind, name, offset=0):
    if not name.endswith(".md"):
        name += ".md"
    try:
        r = reports.read_report(kind, name)
    except (ValueError, OSError):
        return {"summary": "无此报告", "error": f"no {kind} report {name}"}
    md, offset = r["markdown"], max(0, int(offset or 0))
    chunk = md[offset:offset + 8000]
    out = {"summary": f"{r['title']} · {len(md)} 字", "title": r["title"], "feishu": r["feishu_url"],
           "markdown": chunk}
    if offset + 8000 < len(md):
        out["next_offset"] = offset + 8000
    return out


# ---------------------------------------------------------------- unified search (L2 entry)

def search(query, start=None, end=None):
    """One keyword across every source → hit counts + the best few rows of each."""
    q = (query or "").strip()
    if not q:
        return {"summary": "空查询", "error": "query 不能为空"}
    s, e, days = _range(start, end, default_days=60)
    like = f"%{q}%"
    win = {}
    for r in db.q("SELECT day_key, payload FROM source_events WHERE event_type='aw_window' "
                  "AND day_key BETWEEN ? AND ? AND payload LIKE ?", (s, e, like)):
        p = json.loads(r["payload"])
        title = (p.get("data") or {}).get("title") or ""
        if q.lower() not in title.lower() and q.lower() not in ((p.get("data") or {}).get("app") or "").lower():
            continue
        w = win.setdefault(title, {"sec": 0, "days": set()})
        w["sec"] += p.get("duration") or 0
        w["days"].add(r["day_key"])
    win_rows = sorted(win.items(), key=lambda kv: -kv[1]["sec"])
    s0, _ = sessions_view.day_range(s)
    _, s1 = sessions_view.day_range(e)
    sess = sessions_view.list_sessions(s0, s1, query=q, limit=10)
    commits = list_commits(s, e, query=q, limit=10)["commits"]
    rep = []
    for kind in ("daily", "weekly"):
        for m in reports.list_reports()[kind]:
            try:
                text = reports.read_report(kind, m["name"])["markdown"]
            except (ValueError, OSError):
                continue
            for line in text.splitlines():
                if q.lower() in line.lower():
                    rep.append(f"{kind}/{m['key']}: {_snip(line, q, 140)}")
                    break
    fs = []
    for d in days:
        f = config.REPO_ROOT / "data" / f"{d}.json"
        if not f.exists():
            continue
        try:
            chats = (json.loads(f.read_text(encoding="utf-8")).get("feishu") or {}).get("chats", [])
        except ValueError:
            continue
        for c in chats:
            for x in c.get("excerpts", []):
                if q.lower() in x.lower():
                    fs.append(f"{d} {c['chat']}: {_snip(x, q, 120)}")
    try:
        ocr = (_sp_get({"q": q, "limit": 1, "start_time": _utc(s), "end_time": _utc(e, True)})
               .get("pagination") or {}).get("total")
    except Exception:
        ocr = None
    hits = {"windows": len(win_rows), "sessions": len(sess), "commits": len(commits),
            "reports": len(rep), "feishu": len(fs), "screen_ocr": ocr}
    return {
        "summary": " · ".join(f"{k} {v}" for k, v in hits.items() if v),
        "range": f"{s}~{e}", "hits": hits,
        "windows": [f"{t[:100]} — {_hm(v['sec'])}, {len(v['days'])} 天 ({min(v['days'])}~{max(v['days'])})"
                    for t, v in win_rows[:10]],
        "sessions": [_session_row(r) for r in sess[:8]],
        "commits": commits[-8:], "reports": rep[:8], "feishu": fs[:8],
        "next": "按来源下钻:read_session / get_windows(day, query=…) / screen_text(query) / read_report / feishu(day)",
    }


# ---------------------------------------------------------------- schema

def _fn(name, desc, props=None, required=()):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props or {}, "required": list(required)}}}


_S = {"type": "string"}
_D = {"type": "string", "description": "YYYY-MM-DD"}
_I = {"type": "integer"}

TOOLS = [
    _fn("search", "一个关键词跨所有来源搜索(窗口标题、会话原文、提交、日报、飞书、屏幕 OCR),返回各来源命中数和最相关几条。不知道从哪查时先用它。",
        {"query": _S, "start": _D, "end": _D}, ["query"]),
    _fn("list_projects", "全部项目:优先级、目标、归类别名、近 N 天你的时间/agent 时间/会话/提交/最近活跃。", {"days": _I}),
    _fn("range_summary", "一段日期内每天的投入(你/agent/指令/提交/前三项目)和项目合计;给 project 时只看该项目每天的投入。默认最近 30 天。",
        {"start": _D, "end": _D, "project": _S}),
    _fn("get_day", "某一天的全貌:各项目时间、按时间排列的工作章节(含你的指令摘要、会话、提交)、会话列表、提交列表。", {"day": _D}, ["day"]),
    _fn("get_windows", "某天分钟级窗口切换记录(时间段、应用、窗口标题、归属项目)。可用 HH:MM 的 start/end、project、query 过滤。",
        {"day": _D, "start": {"type": "string", "description": "HH:MM"}, "end": {"type": "string", "description": "HH:MM"},
         "project": _S, "query": _S, "limit": _I}, ["day"]),
    _fn("find_sessions", "查找 agent 会话(claude/pi):按关键词(全文)、日期、项目、agent 过滤,返回 id、标题(你的第一条指令)、执行时长、指令数。",
        {"query": _S, "start": _D, "end": _D, "project": _S, "agent": {"type": "string", "enum": ["claude", "pi"]}, "limit": _I}),
    _fn("read_session", "读某个会话原文。mode=prompts 只看你的指令(默认,省篇幅);mode=full 含 agent 回复和工具调用。分页用 offset。",
        {"id": _I, "mode": {"type": "string", "enum": ["prompts", "full"]}, "offset": _I, "limit": _I}, ["id"]),
    _fn("list_commits", "你的 git 提交(只含你本人的),可按日期、项目、关键词过滤。", {"start": _D, "end": _D, "project": _S, "query": _S, "limit": _I}),
    _fn("screen_text", "屏幕截图 OCR(screenpipe),按关键词和日期查,返回时间、识别文字片段和截图地址。分页用 offset。",
        {"query": _S, "start": _D, "end": _D, "limit": _I, "offset": _I}),
    _fn("feishu", "某天飞书聊天摘录和收到的文件。", {"day": _D}, ["day"]),
    _fn("list_reports", "已生成的日报/周报列表(含飞书链接)。", {"kind": {"type": "string", "enum": ["daily", "weekly"]}}),
    _fn("read_report", "读一篇日报或周报的 Markdown 正文。name 如 2026-10-07 或 2026-W41。",
        {"kind": {"type": "string", "enum": ["daily", "weekly"]}, "name": _S, "offset": _I}, ["kind", "name"]),
]

IMPL = {"search": search, "list_projects": list_projects, "range_summary": range_summary, "get_day": get_day,
        "get_windows": get_windows, "find_sessions": find_sessions, "read_session": read_session,
        "list_commits": list_commits, "screen_text": screen_text, "feishu": feishu,
        "list_reports": list_reports, "read_report": read_report}


def call_tool(name: str, args: dict) -> dict:
    f = IMPL.get(name)
    if not f:
        return {"summary": "未知工具", "error": f"unknown tool {name}"}
    try:
        return f(**args)
    except TypeError as e:
        return {"summary": "参数错误", "error": str(e)}
    except ValueError as e:
        return {"summary": "参数错误", "error": str(e)}


def _dump(result: dict) -> str:
    s = json.dumps(result, ensure_ascii=False, default=str)
    if len(s) > RESULT_CHARS:
        s = s[:RESULT_CHARS] + ' …[结果过长已截断:请用更窄的日期/过滤条件或 offset 分页]'
    return s


# ---------------------------------------------------------------- prompt

def catalog() -> str:
    known = _known_days()
    a = _api()
    stats = a.project_stats(30)["projects"]
    top = sorted(((k, v) for k, v in stats.items() if not projects.is_category(k) and k not in ("Unassigned", "其他活动")),
                 key=lambda kv: -(kv[1]["human_sec"] + kv[1]["agent_sec"]))[:12]
    n_sess = db.q1("SELECT COUNT(*) AS n FROM agent_sessions")["n"]
    today = dt.datetime.now(tz())
    wd = "一二三四五六日"[today.isoweekday() - 1]
    mon = today.date() - dt.timedelta(days=today.isoweekday() - 1)
    lines = [
        f"今天是 {today:%Y-%m-%d} 星期{wd},时区 {config.TIMEZONE}。"
        f"本周 = {mon:%Y-%m-%d}~{mon + dt.timedelta(days=6):%Y-%m-%d},"
        f"上周 = {mon - dt.timedelta(days=7):%Y-%m-%d}~{mon - dt.timedelta(days=1):%Y-%m-%d}(周一开始)。",
        f"看板已同步的日期:{known[0] if known else '-'} ~ {known[-1] if known else '-'}(共 {len(known)} 天);"
        "之后的日期看板还没有数据,但可能已有日报(list_reports)和飞书记录。",
        f"会话总数 {n_sess}(claude / pi)。",
        "近 30 天主要项目(你 / agent):" + ";".join(f"{k} {_hm(v['human_sec'])}/{_hm(v['agent_sec'])}" for k, v in top),
    ]
    return "\n".join(lines)


SYSTEM = """你是用户个人工作看板里的助手,回答关于 TA 自己工作的问题:做了什么、在哪个项目上、和 agent 怎么协作、进展与问题。

## 你能看到的数据
- 窗口记录(ActivityWatch):每个前台窗口的应用、标题、时长,已按项目归类。
- agent 会话(claude code / pi):用户的每条指令、agent 的回复和工具调用。
- git 提交(只含用户本人的)、项目设定(优先级、目标)。
- 屏幕截图 OCR(screenpipe),覆盖时段有限,英文 OCR,中文多为乱码。
- 每天的飞书聊天摘录;已生成的日报/周报。

## 怎么查(渐进式)
先看概要再下钻,不要一上来读原文:
1. 范围类问题 → range_summary / list_projects;某一天 → get_day;找东西 → search。
2. 需要细节时再用 find_sessions / get_windows / list_commits / screen_text / feishu。
3. 只有确实需要原话时才 read_session / read_report,并优先用默认的精简模式。
能并行的调用一次发出。一般 1–4 次调用就够,不要为了周全把所有工具都调一遍。

## 回答
- 用中文,先给结论,再列依据;数字带单位(如 1h20m)。
- 只根据工具返回的数据回答;查不到就直说"数据里没有",并说明查了哪里。区分"没有记录"和"没做"(比如那天窗口没采集)。
- 引用时给出可点的链接。只有这四种格式可用,不要发明别的(提交、窗口、PR 不加链接):
  会话 [标题](#session/ID),某天 [10-07](#day/2026-10-07),日报 [日报 10-07](#report/daily/2026-10-07),周报 [W41](#report/weekly/2026-W41)。
- observed=false 表示那天没有窗口记录,"你的时间"为 0 是因为没采集,不是没工作。
- 屏幕截图可以用 ![说明](image地址) 嵌入,最多 3 张。
- 简洁,不要复述工具的原始 JSON。

## 当前状态
{catalog}
"""


def system_prompt(context: dict = None) -> str:
    s = SYSTEM.replace("{catalog}", catalog())
    if context:
        bits = []
        if context.get("tab"):
            bits.append(f"用户正在看「{context['tab']}」页")
        if context.get("day"):
            bits.append(f"选中日期 {context['day']}")
        if context.get("session_id"):
            bits.append(f"选中会话 #{context['session_id']}")
        if context.get("view"):
            bits.append(f"时间轴视野 {context['view']}")
        if bits:
            s += "\n" + ",".join(bits) + "。问题里的『今天/这天/这个会话』默认指这些。\n"
    return s


# ---------------------------------------------------------------- storage

def list_threads(limit=50):
    return [dict(r) for r in db.q("SELECT id, title, created_at, updated_at FROM chat_threads "
                                   "ORDER BY updated_at DESC LIMIT ?", (limit,))]


def delete_thread(tid: int):
    with db.Tx() as c:
        c.execute("DELETE FROM chat_messages WHERE thread_id=?", (tid,))
        c.execute("DELETE FROM chat_threads WHERE id=?", (tid,))


def _messages(tid: int):
    return [(r["turn"], json.loads(r["body"])) for r in
            db.q("SELECT turn, body FROM chat_messages WHERE thread_id=? ORDER BY id", (tid,))]


def _save(tid: int, turn: int, msg: dict):
    with db.Tx() as c:
        c.execute("INSERT INTO chat_messages(thread_id, turn, role, body, created_at) VALUES (?,?,?,?,?)",
                  (tid, turn, msg["role"], json.dumps(msg, ensure_ascii=False), now_iso()))
        c.execute("UPDATE chat_threads SET updated_at=? WHERE id=?", (now_iso(), tid))


def thread_view(tid: int):
    """Messages grouped by turn for display: question, tool steps, answer."""
    t = db.q1("SELECT * FROM chat_threads WHERE id=?", (tid,))
    if not t:
        return None
    turns = {}
    for turn, m in _messages(tid):
        v = turns.setdefault(turn, {"turn": turn, "question": "", "steps": [], "answer": ""})
        if m["role"] == "user":
            v["question"] = m.get("display") or m["content"]
            v["context"] = m.get("context")
        elif m["role"] == "assistant":
            for tc in m.get("tool_calls") or []:
                v["steps"].append({"id": tc["id"], "name": tc["function"]["name"],
                                   "args": _args(tc["function"]["arguments"]), "summary": None})
            if m.get("content"):
                v["answer"] += m["content"]
        elif m["role"] == "tool":
            for st in v["steps"]:
                if st["id"] == m["tool_call_id"]:
                    st["summary"] = m.get("summary")
        elif m["role"] == "error":
            v["error"] = m["content"]
    return {**dict(t), "turns": [turns[k] for k in sorted(turns)]}


def _args(raw: str) -> dict:
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def _history(tid: int) -> tuple:
    """Replay messages; tool output older than KEEP_TURNS turns is stubbed."""
    msgs = _messages(tid)
    last = max((t for t, _ in msgs), default=0)
    answered = {m["tool_call_id"] for _, m in msgs if m["role"] == "tool"}
    out = []
    for turn, m in msgs:
        if m["role"] == "error":
            continue
        m = {k: v for k, v in m.items() if k in ("role", "content", "tool_calls", "tool_call_id")}
        if m.get("tool_calls"):
            # a turn cut off mid-tool (closed tab) left calls without results: drop them
            m["tool_calls"] = [c for c in m["tool_calls"] if c["id"] in answered] or None
            if not m["tool_calls"]:
                m.pop("tool_calls")
                if not m.get("content"):
                    continue
        if m["role"] == "tool" and turn <= last - KEEP_TURNS:
            m["content"] = "(早前的查询结果已省略,需要时请重新调用)"
        out.append(m)
    return out, last


# ---------------------------------------------------------------- the loop

def _stream(messages: list, tools=True):
    """Yield ('delta', text) and finally ('message', assistant_msg)."""
    body = {"model": model(), "messages": messages, "stream": True, "temperature": 0.3,
            "enable_thinking": False}
    if tools:
        body["tools"] = TOOLS
    req = urllib.request.Request(config.LLM_BASE_URL + "/chat/completions", json.dumps(body).encode(),
                                 {"Authorization": f"Bearer {config.LLM_API_KEY}",
                                  "Content-Type": "application/json"})
    content, calls = "", {}
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            for ch in chunk.get("choices") or []:
                d = ch.get("delta") or {}
                if d.get("content"):
                    content += d["content"]
                    yield "delta", d["content"]
                for tc in d.get("tool_calls") or []:
                    c = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                    c["id"] = tc.get("id") or c["id"]
                    f = tc.get("function") or {}
                    c["name"] += f.get("name") or ""
                    c["arguments"] += f.get("arguments") or ""
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = [{"id": c["id"] or f"call_{i}", "type": "function",
                              "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                             for i, c in sorted(calls.items())]
    yield "message", msg


def ask(text: str, thread_id: int = None, context: dict = None):
    """Run one turn. Yields display events (dicts); persists every message."""
    text = (text or "").strip()
    if not text:
        yield {"type": "error", "message": "问题为空"}
        return
    if thread_id and not db.q1("SELECT id FROM chat_threads WHERE id=?", (thread_id,)):
        thread_id = None
    if not thread_id:
        with db.Tx() as c:
            thread_id = c.execute("INSERT INTO chat_threads(title, created_at, updated_at) VALUES (?,?,?)",
                                  (_snip(text, n=40), now_iso(), now_iso())).lastrowid
    yield {"type": "thread", "id": thread_id}
    history, last = _history(thread_id)
    turn = last + 1
    user = {"role": "user", "content": text, "display": text, "context": context or None}
    _save(thread_id, turn, user)
    messages = [{"role": "system", "content": system_prompt(context)}, *history,
                {"role": "user", "content": text}]
    try:
        for rnd in range(MAX_ROUNDS + 1):
            msg = None
            for kind, v in _stream(messages, tools=rnd < MAX_ROUNDS):
                if kind == "delta":
                    yield {"type": "delta", "text": v}
                else:
                    msg = v
            _save(thread_id, turn, msg)
            messages.append(msg)
            if not msg.get("tool_calls"):
                break
            for tc in msg["tool_calls"]:
                args = _args(tc["function"]["arguments"])
                yield {"type": "step", "id": tc["id"], "name": tc["function"]["name"], "args": args}
                t0 = time.time()
                result = call_tool(tc["function"]["name"], args)
                summary = result.get("summary") or ""
                tm = {"role": "tool", "tool_call_id": tc["id"], "content": _dump(result), "summary": summary}
                _save(thread_id, turn, tm)
                messages.append({k: tm[k] for k in ("role", "tool_call_id", "content")})
                yield {"type": "step_done", "id": tc["id"], "summary": summary,
                       "error": result.get("error"), "ms": round((time.time() - t0) * 1000)}
            if msg.get("content"):
                yield {"type": "delta", "text": "\n\n"}
    except Exception as e:   # network / API error: keep what we have, tell the user
        err = f"{type(e).__name__}: {e}"[:300]
        _save(thread_id, turn, {"role": "error", "content": err})
        yield {"type": "error", "message": err}
        return
    yield {"type": "done", "thread_id": thread_id}
