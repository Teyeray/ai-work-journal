"""Project resolution: DB project table (user-owned) + rule matchers + overrides.

Priority: manual override > user project rules > journal heuristics (ported
from collect.py so classifications stay consistent with the daily reports).
"""
import re
from pathlib import Path

from . import config, db
from .timeutil import iso, now_utc

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
CATEGORY_LABELS = set(APP_CAT.values()) | {"个人事务", "终端·其他", "网页浏览"}

_MATCHERS = None


def _build_matchers():
    label_by_key = {}
    dirs = []
    for root in config.PROJECT_ROOTS:
        try:
            for child in Path(root).expanduser().iterdir():
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


def _db_rules():
    """User-defined regex rules from the projects table: [(compiled, project_id)]."""
    out = []
    for r in db.q("SELECT id, name, rules_json FROM projects WHERE active=1"):
        rules = []
        try:
            rules = (r["rules_json"] and __import__("json").loads(r["rules_json"])) or []
        except Exception:
            rules = []
        pats = []
        if isinstance(rules, list):
            pats = [x for x in rules if isinstance(x, str)]
        elif isinstance(rules, dict):
            pats = [x for x in rules.get("match", []) if isinstance(x, str)]
        for pat in pats:
            try:
                out.append((re.compile(pat, re.I), r["id"], r["name"]))
            except re.error:
                continue
    return out


def matchers():
    global _MATCHERS
    if _MATCHERS is None:
        _MATCHERS = _build_matchers()
    return _MATCHERS


def classify(app: str, title: str) -> str:
    """→ project/​category label (heuristic layer, consistent with journal)."""
    if title:
        t = title.lower()
        if "滴答清单" in t or "ta-da" in t:
            return "个人事务"
        best, best_pos = None, 1 << 30
        for rx, label in matchers():
            m = rx.search(t)
            if m and m.start() < best_pos:
                best, best_pos = label, m.start()
        if best:
            return best
    return APP_CAT.get(app.lower(), app)


def resolve_project_id(label: str):
    """Map a heuristic label to a projects row, creating stub rows lazily."""
    if not label:
        return None
    row = db.q1("SELECT id FROM projects WHERE name=?", (label,))
    if row:
        return row["id"]
    db.ex("INSERT INTO projects(name, created_at, updated_at) VALUES(?,?,?)",
          (label, iso(now_utc()), iso(now_utc())))
    return db.q1("SELECT id FROM projects WHERE name=?", (label,))["id"]


def classify_for_session(cwd: str) -> str:
    """Session cwd → project label (uses the same folding rules)."""
    if not cwd:
        return "Unassigned"
    c = cwd.lower().rstrip("/")
    for key, fold in PROJ_FOLD.items():
        if key in c:
            return fold
    best = None
    for rx, label in matchers():
        m = rx.search(c)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), label)
    if best:
        return best[1]
    return "Unassigned"


def is_category(label: str) -> bool:
    return label in CATEGORY_LABELS or label in {c for c in APP_CAT.values()}


STABLE_COLORS = ["#2563eb", "#dc2626", "#16a34a", "#9333ea", "#0891b2",
                 "#d97706", "#db2777", "#4f46e5", "#059669", "#6b7280"]


def project_color(project_id: int, name: str) -> str:
    row = db.q1("SELECT color FROM projects WHERE id=?", (project_id,))
    if row and row["color"]:
        return row["color"]
    return STABLE_COLORS[hash(name) % len(STABLE_COLORS)]
