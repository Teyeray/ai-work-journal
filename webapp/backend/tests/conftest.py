"""Shared fixtures: temp DB + sandboxed source dirs, no network, no real sessions.

JOURNAL_WEB_DB must point at the sandbox BEFORE any app.* import (test modules
import app at top level, which happens at collection time).
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
_SANDBOX = Path(tempfile.mkdtemp(prefix="ai-journal-test-"))
os.environ["JOURNAL_WEB_DB"] = str(_SANDBOX / "test.db")
os.environ["JOURNAL_LLM_DISABLE"] = "1"   # tests never call the network
os.environ["JOURNAL_AUTOSYNC"] = "0"           # no background sync thread work in tests
os.environ["JOURNAL_SCHEDULE_FILE"] = str(_SANDBOX / "schedule.json")
os.environ["TZ"] = "Asia/Singapore"
os.environ["JOURNAL_TZ"] = "Asia/Singapore"
os.environ["JOURNAL_CONFIG_DIR"] = str(_SANDBOX / "config")
os.environ["JOURNAL_CACHE_DIR"] = str(_SANDBOX / "cache")
os.environ["JOURNAL_NO_LOCAL_CONFIG"] = "1"   # hermetic: never this machine's *.local.json
if hasattr(time, "tzset"):   # POSIX; on Windows JOURNAL_TZ alone sets the zone
    time.tzset()
sys.path.insert(0, str(BACKEND))

from app import config, db, timeutil  # noqa: E402

# the classification rules the tests are written against (RSI, CASMI, paperfacts …)
config.CLASSIFICATION = {**config.CLASSIFICATION, **json.loads(
    (Path(__file__).parent / "fixtures" / "classification.json").read_text(encoding="utf-8"))}

_TABLES = ("screen_captures", "ocr_cache", "source_events", "source_cursors", "agent_sessions", "session_event_links",
           "projects", "manual_overrides", "session_reviews", "review_feedback",
           "analysis_jobs", "daily_projections", "sync_log", "outcomes",
           "planned_blocks", "llm_title_labels", "chat_messages", "chat_threads")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Fresh DB content + source dirs pointed at the sandbox."""
    for t in _TABLES:
        db.ex(f"DELETE FROM {t}")
    monkeypatch.setattr(config, "PI_SESSIONS_DIR", tmp_path / "pi-sessions")
    monkeypatch.setattr(config, "CLAUDE_SESSIONS_DIR", tmp_path / "claude-projects")
    monkeypatch.setattr(config, "PROJECT_ROOTS", [])
    Path(config.PI_SESSIONS_DIR).mkdir(exist_ok=True)
    Path(config.CLAUDE_SESSIONS_DIR).mkdir(exist_ok=True)
    return config


def aw_event(day, hhmm, dur_min, app="ghostty", title="bash", kind="aw_window",
             status=None):
    """One synthetic AW event; local time, +08:00."""
    ts = f"{day}T{hhmm}:00+08:00"
    data = {"status": status} if kind == "aw_afk" else {"app": app, "title": title}
    return {"event_type": kind, "occurred_at": ts,
            "payload": json.dumps({"duration": dur_min * 60, "data": data})}


_KEY = [0]


def load_aw(rows):
    """Insert synthetic AW events; timestamps normalized to UTC (storage rule)."""
    for r in rows:
        _KEY[0] += 1
        ts = timeutil.parse_ts(r["occurred_at"])
        utc = timeutil.iso(ts)
        db.ex("INSERT INTO source_events(source_key,event_key,event_type,occurred_at,"
              "observed_at,day_key,payload,created_at) VALUES(?,?,?,?,?,?,?,?)",
              ("activitywatch", f"t{_KEY[0]}", r["event_type"], utc, utc,
               timeutil.local_day_key(ts), r["payload"], utc))
