"""Project classification: data-driven rules + explainable decisions.

Every project is a row in the projects table; its rules live in rules_json:

    {"aliases":  ["memoharness", "工作看板"],   keywords (ASCII: word match with
                                                 _-./ treated as spaces; CJK: substring)
     "paths":    ["/home/me/Projects/RSI"],    directories (longest-prefix match)
     "patterns": ["(?i)harness.*benchmark"],   user regexes on the window title
     "parent":   "paperfacts",                 report under another project
     "ignore":   false}                         never match this project

Rows are created for every directory under project_roots (path seeded) and for
`seed_projects` in webapp/config.json (applied once per project). Signals are
tried strongest first and the first hit wins:

    path in title  >  user pattern  >  alias keyword (longest wins)
                   >  category keyword  >  LLM semantic label (llm_classify)
                   >  app category  >  其他应用

Unknown apps never become projects. explain() returns the reason with the label
so the UI can say *why* a window was filed where it was.
"""
import json
import os
import re
import time
from pathlib import Path

from . import config, db
from .timeutil import iso, now_utc

CLS = config.CLASSIFICATION
APP_CAT = {k.lower(): v for k, v in CLS["app_categories"].items()}
CAT_KW = {cat: [k.lower() for k in kws] for cat, kws in CLS["category_keywords"].items()}
OTHER_APP = CLS["other_app_category"]
CATEGORY_LABELS = set(APP_CAT.values()) | set(CAT_KW) | {OTHER_APP, "终端·其他", "网页浏览", "个人事务"}
SKIP_DIRS = {d.lower() for d in CLS["skip_dirs"]}

_SEP = re.compile(r"[\s_\-./\\:|·,;()\[\]{}]+")
# absolute or ~-relative paths that appear as their own token in a title
# POSIX (/x, ~/x) and Windows (C:\x, C:/x) paths inside a window title
_PATH_IN_TITLE = re.compile(r"(?:^|(?<=[\s(\[:'\"]))(~?/[^\s()\[\]'\"|]+|[A-Za-z]:[\\/][^\s()\[\]'\"|]+)")


def _npath(p) -> str:
    """Comparable path: ~ expanded, forward slashes, no trailing slash;
    case-insensitive on Windows."""
    s = str(Path(p).expanduser()).replace("\\", "/").rstrip("/")
    return s.lower() if os.name == "nt" else s


_ASCII = re.compile(r"^[\x00-\x7f]+$")


def _norm(s: str) -> str:
    return " " + _SEP.sub(" ", (s or "").lower()).strip() + " "


def _alias_rx(a: str):
    """ASCII: whole words on the separator-normalised title, optional numeric
    suffix ("casmi" matches "CASMI26"); CJK: plain substring."""
    if _ASCII.match(a):
        core = re.escape(_norm(a).strip())
        return re.compile(r" " + core + r"\d* ")
    return re.compile(re.escape(a))


def parse_rules(raw) -> dict:
    """rules_json → normalized dict (accepts the legacy list / {"match": [...]} forms)."""
    try:
        r = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (TypeError, ValueError):
        r = {}
    if isinstance(r, list):
        r = {"patterns": r}
    if not isinstance(r, dict):
        r = {}
    pats = list(r.get("patterns") or []) + list(r.get("match") or [])
    return {
        "aliases": [a for a in (r.get("aliases") or []) if isinstance(a, str) and a.strip()],
        "paths": [p.rstrip("/") for p in (r.get("paths") or []) if isinstance(p, str) and p.strip()],
        "patterns": [p for p in pats if isinstance(p, str) and p.strip()],
        "parent": r.get("parent") or None,
        "ignore": bool(r.get("ignore")),
        "seeded": bool(r.get("seeded")),
    }


# ---------------- registry: discover + seed + compile ----------------

def discovered_dirs() -> dict:
    """{dir name: absolute path} for directories directly under project_roots."""
    out = {}
    for root in config.PROJECT_ROOTS:
        try:
            for child in sorted(Path(root).expanduser().iterdir()):
                if child.is_dir() and not child.name.startswith(".") and child.name.lower() not in SKIP_DIRS:
                    out.setdefault(child.name, str(child))
        except OSError:
            continue
    return out


def _save_rules(pid: int, rules: dict) -> None:
    db.ex("UPDATE projects SET rules_json=?, updated_at=? WHERE id=?",
          (json.dumps(rules, ensure_ascii=False), iso(now_utc()), pid))


def _seed() -> None:
    """Idempotent: rows for discovered dirs (with their path) and config seeds
    (applied once — a user who later removes an alias keeps it removed)."""
    dirs = discovered_dirs()
    seeds = CLS.get("seed_projects") or {}
    for name in set(dirs) | set(seeds) | {s.get("parent") for s in seeds.values() if s.get("parent")}:
        if not name:
            continue
        ensure_project(name)
        row = db.q1("SELECT id, rules_json FROM projects WHERE name=?", (name,))
        rules = parse_rules(row["rules_json"])
        changed = False
        if name in dirs and dirs[name] not in rules["paths"]:
            rules["paths"].append(dirs[name])
            changed = True
        if name in seeds and not rules["seeded"]:
            sd = seeds[name]
            rules["aliases"] += [a for a in sd.get("aliases", []) if a not in rules["aliases"]]
            rules["patterns"] += [p for p in sd.get("patterns", []) if p not in rules["patterns"]]
            rules["parent"] = rules["parent"] or sd.get("parent")
            rules["ignore"] = rules["ignore"] or bool(sd.get("ignore"))
            rules["seeded"] = True
            changed = True
        if changed:
            _save_rules(row["id"], rules)
    # stub rows that were really app ids (from the old heuristic) are hidden, not deleted
    apps = {r["a"] for r in db.q("SELECT DISTINCT lower(json_extract(payload,'$.data.app')) AS a "
                                 "FROM source_events WHERE event_type='aw_window'") if r["a"]}
    for r in db.q("SELECT id, name, rules_json, goals_json, notes, color FROM projects WHERE active=1"):
        if (r["name"] not in dirs and r["name"] not in seeds and not r["goals_json"] and not r["notes"]
                and not r["color"] and not any(parse_rules(r["rules_json"])[k]
                                               for k in ("aliases", "patterns", "paths"))
                and (r["name"].lower() in apps or not r["name"].strip() or is_category(r["name"])
                     or r["name"] == "Unassigned")):
            db.ex("UPDATE projects SET active=0, updated_at=? WHERE id=?", (iso(now_utc()), r["id"]))


class _Compiled:
    def __init__(self):
        rows = db.q("SELECT id, name, priority, rules_json FROM projects WHERE active=1 "
                    "ORDER BY priority, id")
        by_name = {r["name"]: parse_rules(r["rules_json"]) for r in rows}
        self.paths, self.patterns, self.aliases, self.prefixes = [], [], [], []
        for r in rows:
            rules = by_name[r["name"]]
            if rules["ignore"]:
                continue
            label = self.label_of(r["name"], by_name)
            for p in rules["paths"]:
                self.paths.append((_npath(p), label))
            for p in rules["patterns"]:
                try:
                    self.patterns.append((re.compile(p, re.I), p, label))
                except re.error:
                    continue
            for a in set(rules["aliases"]) | {r["name"]}:
                a = a.strip().lower()
                if len(a) < 3 and _ASCII.match(a):
                    continue  # too short to be a reliable keyword
                self.aliases.append((_alias_rx(a), a, label, bool(_ASCII.match(a))))
            nm = r["name"].lower()
            if _ASCII.match(nm) and len(nm) >= 6:
                self.prefixes.append((nm, label))
        # semantic labels for evidence-free titles, valid for this exact project set
        from . import llm_classify
        top = [r["name"] for r in rows if not by_name[r["name"]]["parent"] and not by_name[r["name"]]["ignore"]]
        self.llm = llm_classify.lookup_table(llm_classify.projects_sig(top))
        self.paths.sort(key=lambda x: -len(x[0]))          # longest prefix first
        self.aliases.sort(key=lambda x: -len(x[1]))        # most specific first

    def with_rule(self, project: str, kind: str, value: str) -> "_Compiled":
        """A copy with one extra rule (for previews; nothing is saved)."""
        import copy
        c = copy.copy(self)
        c.paths, c.patterns, c.aliases = list(self.paths), list(self.patterns), list(self.aliases)
        if kind == "path":
            c.paths.append((_npath(value), project))
            c.paths.sort(key=lambda x: -len(x[0]))
        elif kind == "pattern":
            c.patterns.insert(0, (re.compile(value, re.I), value, project))
        else:
            a = value.strip().lower()
            ascii_ = bool(_ASCII.match(a))
            c.aliases.append((_alias_rx(a), a, project, ascii_))
            c.aliases.sort(key=lambda x: -len(x[1]))
        return c

    @staticmethod
    def label_of(name, by_name, depth=0):
        parent = by_name.get(name, {}).get("parent")
        if parent and depth < 5 and parent != name:
            return _Compiled.label_of(parent, by_name, depth + 1)
        return name

    def match_path(self, path: str):
        p = _npath(path)
        for root, label in self.paths:
            if p == root or p.startswith(root + "/"):
                return label, root
        return None

    def match_alias(self, text: str):
        n, low = _norm(text), (text or "").lower()
        best = None
        for rx, alias, label, ascii_ in self.aliases:
            m = rx.search(n if ascii_ else low)
            if m and (best is None or len(alias) > len(best[1]) or
                      (len(alias) == len(best[1]) and m.start() < best[2])):
                best = (label, alias, m.start())
                if len(alias) > 24:
                    break
        if best:
            return best[0], best[1]
        # an abbreviated project name as its own token ("t paperfact" → paperfacts)
        for tok in n.split():
            if len(tok) >= 5:
                for nm, label in self.prefixes:
                    if nm.startswith(tok) and len(tok) >= len(nm) * 0.8:
                        return label, tok
        return None


_STATE = {"key": None, "checked": 0.0, "compiled": None}


def _version():
    r = db.q1("SELECT COUNT(*) AS n, COALESCE(MAX(updated_at),'') AS u, "
              "COALESCE(SUM(active),0) AS a FROM projects")
    l = db.q1("SELECT COUNT(*) AS n, COALESCE(MAX(created_at),'') AS u FROM llm_title_labels")
    return (r["n"], r["u"], r["a"], l["n"], l["u"])


def compiled() -> _Compiled:
    """Rules compiled once per projects-table version (re-checked at most every 0.5s)."""
    now = time.monotonic()
    if _STATE["compiled"] is None or now - _STATE["checked"] > 0.5:
        _STATE["checked"] = now
        key = _version()
        if key != _STATE["key"] or _STATE["compiled"] is None:
            _seed()
            _STATE["key"] = _version()
            _STATE["compiled"] = _Compiled()
    return _STATE["compiled"]


def rules_version() -> str:
    """Changes whenever any project rule changes — part of projection cache keys."""
    compiled()
    return "|".join(str(x) for x in _STATE["key"])


def invalidate() -> None:
    _STATE["compiled"] = None


# ---------------- classification ----------------

def explain(app: str, title: str, c: "_Compiled" = None, use_llm: bool = True):
    """→ (label, reason) for one foreground window."""
    c = c or compiled()
    t = title or ""
    for m in _PATH_IN_TITLE.findall(t):
        hit = c.match_path(m)
        if hit:
            return hit[0], f"路径 {m}"
    for rx, pat, label in c.patterns:
        if rx.search(t):
            return label, f"规则 /{pat}/"
    hit = c.match_alias(t)
    if hit:
        return hit[0], f"关键词 “{hit[1]}”"
    low = t.lower()
    for cat, kws in CAT_KW.items():
        for k in kws:
            if k in low:
                return cat, f"关键词 “{k}”"
    if use_llm and c.llm:
        from . import llm_classify
        hit = c.llm.get(llm_classify.title_key(app, t))
        if hit and hit[1] >= config.LLM_CLASSIFY["min_confidence"]:
            return hit[0], f"语义判断:{hit[2]}"
    a = (app or "").lower()
    if a in APP_CAT:
        return APP_CAT[a], "按应用分类"
    return OTHER_APP, "未知应用"


def classify(app: str, title: str) -> str:
    return explain(app, title)[0]


def explain_path(path: str):
    """Session cwd / repo path → (label, reason): longest registered directory,
    else a project keyword among the path components."""
    if not path:
        return "Unassigned", "无工作目录"
    c = compiled()
    hit = c.match_path(path)
    if hit:
        return hit[0], f"目录 {hit[1]}"
    hit = c.match_alias(path)
    if hit:
        return hit[0], f"关键词 “{hit[1]}”"
    return "Unassigned", "目录未登记"


def classify_for_session(cwd: str) -> str:
    return explain_path(cwd)[0]


def is_category(label: str) -> bool:
    return label in CATEGORY_LABELS


def ensure_project(name: str) -> None:
    """Idempotently create a stub projects row for a label in use."""
    if not name or not name.strip() or is_category(name) or name == "Unassigned":
        return
    db.ex("INSERT OR IGNORE INTO projects(name, created_at, updated_at) VALUES(?,?,?)",
          (name, iso(now_utc()), iso(now_utc())))


def resolve_project_id(label: str):
    """Map a label to a projects row, creating a stub row lazily."""
    if not label or label == "Unassigned":
        return None
    ensure_project(label)
    row = db.q1("SELECT id FROM projects WHERE name=?", (label,))
    return row["id"] if row else None


STABLE_COLORS = ["#2563eb", "#dc2626", "#16a34a", "#9333ea", "#0891b2",
                 "#d97706", "#db2777", "#4f46e5", "#059669", "#6b7280"]


def project_color(project_id: int, name: str) -> str:
    row = db.q1("SELECT color FROM projects WHERE id=?", (project_id,))
    if row and row["color"]:
        return row["color"]
    return STABLE_COLORS[hash(name) % len(STABLE_COLORS)]
