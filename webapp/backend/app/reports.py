"""Daily / weekly reports: list, read, schedule, trigger.

Reports are written by the journal pipeline (daily.py → reports/<date>.md,
weekly.py → reports/weekly/<year>-W<ww>.md) and pushed to Feishu by feishu.py,
which records each document's URL in data/feishu-map.json.

The schedule lives outside the repo (<config dir>/schedule.json) and is
honoured by daily.py / weekly.py when the OS scheduler (systemd timer, launchd
agent or Task Scheduler task, see osutil.JOBS) starts them; running them by
hand ignores it.
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from . import config

REPORTS = config.REPO_ROOT / "reports"
WEEKLY = REPORTS / "weekly"
FEISHU_MAP = config.REPO_ROOT / "data" / "feishu-map.json"
SCHEDULE = Path(os.environ.get("JOURNAL_SCHEDULE_FILE", str(config.CONFIG_DIR / "schedule.json")))
LOG_DIR = config.CACHE_DIR
DEFAULT_SCHEDULE = {"daily_weekdays": [1, 2, 3, 4, 5, 6, 7], "weekly_enabled": True, "feishu_push": True}
UNITS = {"daily": "ai-journal-daily", "weekly": "ai-journal-weekly"}  # = osutil.JOBS labels
_NAME = {"daily": re.compile(r"^\d{4}-\d{2}-\d{2}\.md$"), "weekly": re.compile(r"^\d{4}-W\d{2}\.md$")}
_RUNS = {}


def _feishu_map() -> dict:
    try:
        return json.loads(FEISHU_MAP.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _meta(path: Path, kind: str, fmap: dict) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    gen = re.search(r"<!-- generated (\S+)", text)
    title = next((l[2:].strip() for l in text.splitlines() if l.startswith("# ")), path.stem)
    fallback = "LLM 生成失败" in text[:400]
    f = fmap.get(path.name) or {}
    out = {"kind": kind, "name": path.name, "key": path.stem, "title": title,
           "generated_at": gen.group(1) if gen else None, "chars": len(text),
           "fallback": fallback, "feishu_url": f.get("url"), "pushed_at": f.get("pushed_at")}
    if kind == "daily":
        out["weekday"] = dt.date.fromisoformat(path.stem).isoweekday()
    return out


def list_reports() -> dict:
    fmap = _feishu_map()
    daily = sorted((p for p in REPORTS.glob("*.md") if _NAME["daily"].match(p.name)), reverse=True)
    weekly = sorted((p for p in WEEKLY.glob("*.md") if _NAME["weekly"].match(p.name)), reverse=True)
    return {"daily": [_meta(p, "daily", fmap) for p in daily],
            "weekly": [_meta(p, "weekly", fmap) for p in weekly]}


def read_report(kind: str, name: str) -> dict:
    if kind not in _NAME or not _NAME[kind].match(name):
        raise ValueError("bad report name")
    path = (REPORTS if kind == "daily" else WEEKLY) / name
    text = path.read_text(encoding="utf-8", errors="replace")
    body = re.sub(r"^<!--.*?-->\s*", "", text, flags=re.S)
    return {**_meta(path, kind, _feishu_map()), "markdown": body}


# ---------------- schedule ----------------

def get_schedule() -> dict:
    try:
        raw = json.loads(SCHEDULE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    return normalize({**DEFAULT_SCHEDULE, **raw})


def normalize(s: dict) -> dict:
    days = sorted({int(d) for d in s.get("daily_weekdays", []) if 1 <= int(d) <= 7})
    return {"daily_weekdays": days, "weekly_enabled": bool(s.get("weekly_enabled", True)),
            "feishu_push": bool(s.get("feishu_push", True))}


def set_schedule(s: dict) -> dict:
    s = normalize({**get_schedule(), **s})
    SCHEDULE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SCHEDULE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SCHEDULE)
    return s


# ---------------- OS scheduler + manual runs ----------------

def timer_status() -> dict:
    out = {}
    for kind in UNITS:
        st = config.osutil.schedule_status(kind)
        out[kind] = {**st, "running": st["running"] or _manual_running(kind), "manual": _manual(kind)}
    return out


def _manual(kind: str):
    r = _RUNS.get(kind)
    if not r:
        return None
    log = Path(r["log"])
    tail = log.read_text(encoding="utf-8", errors="replace")[-1200:] if log.exists() else ""
    return {"started_at": r["started_at"], "running": _manual_running(kind),
            "exit_code": r["proc"].poll(), "log_tail": tail}


def _manual_running(kind: str) -> bool:
    r = _RUNS.get(kind)
    return bool(r) and r["proc"].poll() is None


def run_now(kind: str) -> dict:
    """Start daily.py / weekly.py now (ignores the weekday schedule; pushes to
    Feishu if the pipeline is set to). Detached; output goes to a log file."""
    if kind not in UNITS:
        raise ValueError("kind must be daily or weekly")
    if _manual_running(kind):
        return {"started": False, "reason": "already running"}
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = LOG_DIR / f"manual-{kind}.log"
    script = config.REPO_ROOT / ("daily.py" if kind == "daily" else "weekly.py")
    env = {k: v for k, v in os.environ.items() if k != "INVOCATION_ID"}
    with log.open("w", encoding="utf-8") as fh:
        proc = subprocess.Popen([sys.executable, str(script)], cwd=str(config.REPO_ROOT),
                                stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env=env, **config.osutil.detach_kwargs())
    _RUNS[kind] = {"proc": proc, "log": str(log), "started_at": dt.datetime.now().isoformat(timespec="seconds")}
    return {"started": True}


def stop_all():  # used by tests
    for r in _RUNS.values():
        config.osutil.terminate(r["proc"])
