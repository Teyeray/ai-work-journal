"""Ingest orchestrator: run all adapters, report affected days.

Idempotent by construction: every adapter upserts with stable event keys.
Re-running the whole pipeline never grows the event set.
"""
from . import db
from .adapters import aw, sessions, git


def run_all() -> dict:
    results = []
    for name, scan in (("activitywatch", aw.scan), ("sessions:pi", lambda: sessions.scan("pi")),
                       ("sessions:claude", lambda: sessions.scan("claude")), ("git", git.scan),
                       ("sessions:optional", sessions.scan_optional)):
        try:
            results.append(scan())
        except Exception as exc:
            results.append({"source": name, "new": 0, "days": [], "error": str(exc)})
    days = sorted(set(d for r in results for d in r.get("days", [])))
    return {"results": results, "affected_days": days,
            "optional_agent_sources": sessions.optional_sources_status(),
            "total_new": sum(r.get("new", 0) for r in results)}


def _link_session_projects(keys) -> None:
    """Classify changed unassigned sessions within their ingestion transaction."""
    from . import projects
    for source, external_id in keys:
        s = db.q1("SELECT id, source, cwd FROM agent_sessions WHERE source=? AND external_id=? AND project_id IS NULL",
                   (source, external_id))
        if not s:
            continue
        label = projects.classify_for_session(s["cwd"] or "")
        if label == "Unassigned":
            continue
        projects.ensure_project(label)
        pid = projects.resolve_project_id(label)
        db.ex("UPDATE agent_sessions SET project_id=?, project_label=? WHERE id=?",
              (pid, label, s["id"]))


def status() -> dict:
    rows = db.q("SELECT source_key, revision, last_scan_at "
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
