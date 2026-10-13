"""Git adapter: commits from all repos under configured project_roots.

Idempotency: event_key = 'git-<repo>-<commit_sha>'.
Cursor: none needed (commits immutable); full log since the first tracked day,
dedupe by sha. Cheap enough for local repos.
"""
import datetime as dt
import json
import subprocess
from pathlib import Path

from .. import config, db
from ..timeutil import iso, now_utc, local_day_key, parse_ts

SINCE = "2026-09-01"


def _repos():
    seen = set()
    for root in config.PROJECT_ROOTS:
        p = Path(root).expanduser()
        if not p.exists():
            continue
        for d in sorted(p.iterdir()):
            if d.is_dir() and (d / ".git").exists() and str(d) not in seen:
                seen.add(str(d))
                yield d


def scan() -> dict:
    total_new, affected_days = 0, set()
    now = iso(now_utc())
    with db.Tx():
        for repo in _repos():
            try:
                r = subprocess.run(["git", "-C", str(repo), "log", f"--since={SINCE}",
                                    "--pretty=%H%x1f%aI%x1f%s%x1f%an"],
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
                if r.returncode != 0:
                    continue
            except Exception:
                continue
            name = repo.name
            for line in r.stdout.splitlines():
                parts = line.split("\x1f")
                if len(parts) != 4:
                    continue
                sha, aiso, subj, author = parts
                ts = parse_ts(aiso)
                if ts is None:
                    continue
                payload = json.dumps({"repo": name, "path": str(repo), "sha": sha,
                                      "subject": subj[:200], "author": author},
                                     ensure_ascii=False)
                rr = db.ex("INSERT INTO source_events(source_key,event_key,event_type,"
                           "occurred_at,observed_at,day_key,payload,created_at) "
                           "VALUES(?,?,?,?,?,?,?,?) "
                           "ON CONFLICT(source_key,event_key) DO NOTHING",
                           ("git", f"git-{name}-{sha[:12]}", "git_commit", iso(ts), now,
                            local_day_key(ts), payload, now))
                if rr.rowcount:
                    total_new += 1
                    affected_days.add(local_day_key(ts))
        db.log_sync("git", total_new)
    return {"source": "git", "new": total_new, "days": sorted(affected_days)}


_AUTHORS = {}


def my_authors(paths) -> set:
    """Who 'you' are in git: the global user.name plus each repo's local
    user.name (e.g. a GitHub identity set only in some repos), plus any
    extra names in webapp/config.json "git_authors". Commits by anyone else
    (collaborators' work pulled into your clones) are not your output."""
    key = frozenset(p for p in paths if p)
    if key in _AUTHORS:
        return _AUTHORS[key]
    names = {n for n in config.GIT_AUTHORS}

    def get(args):
        try:
            r = subprocess.run(["git", *args, "config", "user.name"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
            return r.stdout.strip()
        except Exception:
            return ""
    names.add(get([]))
    for p in key:
        names.add(get(["-C", p]))
    names.discard("")
    _AUTHORS[key] = names
    return names


def is_mine(payload: dict, paths) -> bool:
    author = payload.get("author")
    return not author or author in my_authors(paths)
