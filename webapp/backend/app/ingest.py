"""Ingest orchestrator: run all adapters, report affected days.

Idempotent by construction: every adapter upserts with stable event keys.
Re-running the whole pipeline never grows the event set.
"""
from . import db
from .adapters import aw, sessions, git


def run_all() -> dict:
    results = [aw.scan(), sessions.scan("pi"), sessions.scan("claude"), git.scan(),
               sessions.scan_optional()]
    days = sorted(set(d for r in results for d in r.get("days", [])))
    _link_session_projects()
    return {"results": results, "affected_days": days,
            "optional_agent_sources": sessions.optional_sources_status(),
            "total_new": sum(r.get("new", 0) for r in results)}


def _link_session_projects() -> None:
    """Classify each session cwd once per scan; set agent label + project_id."""
    from . import projects
    for s in db.q("SELECT id, source, cwd FROM agent_sessions WHERE project_id IS NULL"):
        label = projects.classify_for_session(s["cwd"] or "")
        if label == "Unassigned":
            continue
        projects.ensure_project(label)
        pid = projects.resolve_project_id(label)
        db.ex("UPDATE agent_sessions SET project_id=?, project_label=? WHERE id=?",
              (pid, label, s["id"]))


def status() -> dict:
    rows = db.q("SELECT source_key, revision, last_scan_at, cursor_json "
                "FROM source_cursors ORDER BY source_key")
    counts = {r["source_key"]: r["n"] for r in db.q(
        "SELECT source_key, COUNT(*) AS n FROM source_events GROUP BY source_key")}
    last = db.q1("SELECT at, new_events FROM sync_log ORDER BY id DESC LIMIT 1")
    return {
        "sources": [{"source": r["source_key"], "revision": r["revision"],
                     "last_scan_at": r["last_scan_at"], "events": counts.get(r["source_key"], 0)}
                    for r in rows],
        "last_sync": dict(last) if last else None,
        "total_events": sum(counts.values()),
    }
