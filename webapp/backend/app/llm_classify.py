"""Semantic classification of window titles that carry no direct evidence.

Rules (path / pattern / keyword) decide most windows. What is left — titles
filed only by their app ("CATL260913 超声扫描数据 EDA 报告", a ChatGPT
conversation name) — is sent to an LLM, deduplicated by title, together with
each project's name, keywords, directories and a few titles already filed
under it. Answers are cached per title against the current set of projects,
so each title is asked once; adding/renaming a project re-asks.

The model may answer null (generic page, chat app, new tab…); only answers
with confidence >= min_confidence are used. In the classifier these sit below
direct rules and above context inference (see projects.explain).

Runs in a background thread, never in a request path. Sends window titles and
project names/keywords/example titles to the configured endpoint.
"""
import hashlib
import json
import re
import threading
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from . import config, db, projects
from .timeutil import iso, now_utc

CFG = config.LLM_CLASSIFY
SYSTEM = (
    "你在帮用户把电脑前台窗口的标题归到他自己的项目里。\n"
    "规则:\n"
    "1. 只有标题内容明确指向某个项目时才给出项目名;项目名必须是给定列表中的原样名称。\n"
    "2. 通用页面(新标签页、搜索首页、邮箱、聊天软件首页、系统设置、购物、新闻、视频等)以及与所有项目都无明显关系的内容,project 为 null。\n"
    "3. 参考每个项目的 hints(关键词/目录)和 examples(已确认属于该项目的窗口标题),按语义判断,而不只是字面匹配。\n"
    "4. confidence 是你对归属正确的把握(0~1);拿不准就给低分或 null。\n"
    "5. titles 中每条有 id、app(应用)和 title;每条都要回答一次,原样回传 id,并在 head 中原样抄写该条 title 的前 8 个字符(用于核对)。\n"
    '只输出 JSON 数组,不要任何解释文字: [{"id": "t0", "head": "标题前8个字符", "project": "项目名" 或 null, '
    '"confidence": 0~1, "why": "不超过30字,针对这一条标题"}]'
)

_STATUS = {"running": False, "done": 0, "total": 0, "labelled": 0, "last_run": None,
           "error": None, "model": CFG["model"]}
_RUN_LOCK = threading.Lock()


def clean_title(title: str) -> str:
    return re.sub(r" - (Google Chrome|Zotero|Mozilla Firefox|Visual Studio Code|Text Editor)$", "",
                  (title or "").strip()).strip()


def title_key(app: str, title: str) -> str:
    return hashlib.sha1(f"{(app or '').lower()}\n{clean_title(title)}".encode()).hexdigest()


def projects_sig(names) -> str:
    return hashlib.sha1("|".join(sorted(names)).encode()).hexdigest()[:16]


def status() -> dict:
    return {**_STATUS, "enabled": config.LLM_CLASSIFY_ENABLED}


# ---------------- prompt material ----------------

def _project_cards(windows):
    """Top-level active projects with hints and up to 6 example titles that the
    rules already filed under them (by time)."""
    rows = db.q("SELECT name, rules_json FROM projects WHERE active=1")
    by = {r["name"]: projects.parse_rules(r["rules_json"]) for r in rows}
    examples = {}
    for app_, title, dur in windows:
        label, reason = projects.explain(app_, title, use_llm=False)
        if reason.startswith(("关键词", "路径", "规则")) and not projects.is_category(label):
            examples.setdefault(label, Counter())[clean_title(title)] += dur
    cards = []
    for name, r in sorted(by.items()):
        if r["parent"] or r["ignore"]:
            continue
        kids = [n for n, k in by.items() if k["parent"] == name]
        hints = r["aliases"] + [p.rstrip("/").split("/")[-1] for p in r["paths"]] + kids
        cards.append({"name": name, "hints": sorted(set(hints))[:12],
                      "examples": [t for t, _ in examples.get(name, Counter()).most_common(6)]})
    return cards


def _recent_windows(days: int):
    since = iso(now_utc() - __import__("datetime").timedelta(days=days))
    for r in db.q("SELECT payload FROM source_events WHERE event_type='aw_window' AND occurred_at >= ?",
                  (since,)):
        p = json.loads(r["payload"])
        d = p.get("data") or {}
        dur = float(p.get("duration") or 0)
        if dur > 0:
            yield d.get("app") or "", d.get("title") or "", dur


# ---------------- model call ----------------

def _call(model: str, cards: list, titles: list) -> list:
    """One chat completion → [{"id", "head", "project", "confidence", "why"}].
    `titles` are [{"id": "t0", "title": ...}] — explicit ids, never positions."""
    body = {"model": model, "temperature": 0, "enable_thinking": False,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": json.dumps(
                             {"projects": cards, "titles": titles}, ensure_ascii=False)}]}
    req = urllib.request.Request(
        config.LLM_BASE_URL + "/chat/completions", json.dumps(body).encode(),
        {"Authorization": f"Bearer {config.LLM_API_KEY}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        out = json.load(resp)
    text = out["choices"][0]["message"]["content"]
    m = re.search(r"\[.*\]", text, re.S)
    return json.loads(m.group(0)) if m else []


# ---------------- run ----------------

def run(days: int = None, call=None) -> dict:
    """Label every evidence-free title of the last `days` days not yet labelled
    against the current project set. Synchronous; returns a summary."""
    call = call or _call
    days = days or CFG["days"]
    projects.invalidate()
    projects.compiled()   # discover + seed projects first, so the project set is stable
    windows = list(_recent_windows(days))
    cards = _project_cards(windows)
    names = {c["name"] for c in cards}
    sig = projects_sig(names)
    have = {r["key"] for r in db.q("SELECT key FROM llm_title_labels WHERE projects_sig=?", (sig,))}
    todo, seen = [], set()
    for app_, title, _ in windows:
        k = title_key(app_, title)
        if k in seen or k in have or len(clean_title(title)) < 3:
            continue
        label, reason = projects.explain(app_, title, use_llm=False)
        if reason not in ("按应用分类", "未知应用"):
            continue
        seen.add(k)
        todo.append((k, app_, clean_title(title)))
    _STATUS.update(total=len(todo), done=0, labelled=0, error=None, model=CFG["model"])
    batches = [todo[i:i + CFG["batch"]] for i in range(0, len(todo), CFG["batch"])]

    def one(batch):
        titles = [{"id": f"t{j}", "app": appname, "title": t} for j, (_, appname, t) in enumerate(batch)]
        try:
            res = call(CFG["model"], cards, titles)
        except Exception as e:  # keep going; failed titles are retried next run
            _STATUS["error"] = str(e)[:200]
            return 0
        n = 0
        for item in res if isinstance(res, list) else []:
            try:
                j = int(str(item["id"]).lstrip("t"))
                k, app_, t = batch[j]
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            # the answer must quote the title it is about — drop misaligned answers
            head = re.sub(r"\s+", "", str(item.get("head") or ""))[:6]
            if not head or head not in re.sub(r"\s+", "", titles[j]["title"]):
                _STATUS["misaligned"] = _STATUS.get("misaligned", 0) + 1
                continue
            proj = item.get("project")
            if proj not in names:
                proj = None
            db.ex("INSERT OR REPLACE INTO llm_title_labels(key,app,title,project,confidence,why,model,"
                  "projects_sig,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                  (k, app_, t, proj, float(item.get("confidence") or 0), str(item.get("why") or "")[:120],
                   CFG["model"], sig, iso(now_utc())))
            n += proj is not None
        _STATUS["done"] += len(batch)
        _STATUS["labelled"] += n
        return n

    with ThreadPoolExecutor(max_workers=CFG["workers"]) as pool:
        labelled = sum(pool.map(one, batches))
    projects.invalidate()
    return {"titles": len(todo), "labelled": labelled, "projects_sig": sig}


def start_background(days: int = None, on_done=None) -> bool:
    """Run once in a daemon thread unless disabled or already running."""
    if not config.LLM_CLASSIFY_ENABLED or not _RUN_LOCK.acquire(blocking=False):
        return False

    def go():
        _STATUS["running"] = True
        try:
            r = run(days)
            _STATUS["last_run"] = {**r, "at": iso(now_utc())}
            if on_done and r["titles"]:
                on_done()
        except Exception as e:
            _STATUS["error"] = str(e)[:200]
        finally:
            _STATUS["running"] = False
            _RUN_LOCK.release()

    threading.Thread(target=go, daemon=True, name="llm-classify").start()
    return True


def lookup_table(sig: str) -> dict:
    """{title key: (project, confidence, why)} for answers given against `sig`."""
    return {r["key"]: (r["project"], r["confidence"], r["why"])
            for r in db.q("SELECT key, project, confidence, why FROM llm_title_labels "
                          "WHERE projects_sig=? AND project IS NOT NULL", (sig,))}


def _settle():  # small helper for tests / scripts: wait for a background run
    while _STATUS["running"]:
        time.sleep(0.1)
