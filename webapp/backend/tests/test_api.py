"""API layer tests via FastAPI TestClient (no network; sandbox DB)."""
import json

from fastapi.testclient import TestClient

from conftest import aw_event, load_aw

from app import api

from fastapi.testclient import TestClient as TC

D = "2026-10-07"


def client():
    return TC(api.app)


def test_health_and_days(env):
    load_aw([aw_event(D, "09:00", 60)])
    c = client()
    r = c.get("/api/v1/health")
    assert r.status_code == 200 and r.json()["ok"] is True
    r = c.get("/api/v1/days")
    assert D in r.json()["days"]


def test_timeline_and_today(env):
    load_aw([
        aw_event(D, "09:00", 120),
        aw_event(D, "10:00", 30, kind="aw_afk", status="afk"),
    ])
    c = client()
    r = c.get(f"/api/v1/timeline?day={D}")
    assert r.status_code == 200
    t = r.json()
    assert t["has_observation"] is True
    assert t["metrics"]["human_active_sec"] == 90 * 60
    assert t["metrics"]["afk_sec"] == 30 * 60
    r = c.get(f"/api/v1/today?day={D}")
    assert r.status_code == 200
    assert "quality" in r.json() and "kpis" in r.json()


def test_override_via_api_applies_and_revokes(env):
    load_aw([aw_event(D, "09:00", 60, app="chrome", title="RSI docs")])
    c = client()
    t = c.get(f"/api/v1/timeline?day={D}").json()
    blk = t["blocks"][0]
    r = c.post("/api/v1/overrides", json={
        "day_key": D, "anchor_start": blk["start"], "field": "project",
        "value": "InoLab", "note": "test"})
    assert r.status_code == 200, r.text
    t2 = c.get(f"/api/v1/timeline?day={D}").json()
    assert t2["blocks"][0]["project"] == "InoLab"
    assert t2["blocks"][0]["is_manual_override"] is True
    from app import db as _db
    ovid = _db.q1("SELECT id FROM manual_overrides WHERE day_key=? AND field='project'",
                  (D,))["id"]
    r = c.delete(f"/api/v1/overrides/{ovid}")
    assert r.status_code == 200
    t3 = c.get(f"/api/v1/timeline?day={D}").json()
    assert t3["blocks"][0]["is_manual_override"] is False


def test_sessions_list_and_detail(env):
    from app import db
    d = "2026-10-03"
    f = env.PI_SESSIONS_DIR / "--home-x-Projects-RSI--sess-9" / "s.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"type": "message", "timestamp": f"{d}T09:00:00Z", "message": {
        "role": "user", "content": [{"type": "text", "text": "hello"}]}}) + "\n"
    f.write_text(line)
    from app.adapters import sessions
    sessions.scan("pi")
    c = client()
    r = c.get(f"/api/v1/sessions?day={d}")
    assert r.status_code == 200
    rows = r.json()["sessions"]
    assert len(rows) == 1
    sid = rows[0]["id"]
    r = c.get(f"/api/v1/sessions/{sid}")
    assert r.status_code == 200
    det = r.json()
    assert det["agent"] == "pi"
    assert det["review"]["status"] in ("unreviewed", "llm_disabled", "analyzed")


def test_review_llm_disabled_and_feedback_validation(env):
    load_aw([])
    c = client()
    r = c.post("/api/v1/sessions/999/review")
    assert r.status_code == 200
    assert r.json()["status"] in ("error", "llm_disabled")
    r = c.post("/api/v1/reviews/feedback", json={"session_id": 1, "rating": "bogus"})
    assert r.status_code == 400


def test_analytics_time(env):
    load_aw([aw_event(D, "09:00", 120)])
    c = client()
    r = c.get(f"/api/v1/analytics/time?start={D}&end={D}")
    assert r.status_code == 200, r.text
    rows = {x["day"]: x for x in r.json()["days"]}
    assert rows[D]["human_sec"] == 120 * 60


def test_evidence_redaction(env):
    load_aw([])
    from app import db, timeutil
    ts = timeutil.iso(timeutil.parse_ts(f"{D}T09:05:00+08:00"))
    db.ex("INSERT INTO source_events(source_key,event_key,event_type,occurred_at,"
          "observed_at,day_key,payload,created_at) VALUES(?,?,?,?,?,?,?,?)",
          ("sessions:pi", "e1", "msg_user", ts, ts, D,
           json.dumps({"text": "key sk-ant-abc123def456 done", "file": "f"}), ts))
    c = client()
    ev = c.get("/api/v1/evidence/e1") if False else None
    # evidence lookup uses numeric id; query it back
    row = db.q1("SELECT id FROM source_events WHERE event_key='e1'")
    r = c.get(f"/api/v1/evidence/{row['id']}")
    assert r.status_code == 200
    assert "sk-ant-abc123def456" not in json.dumps(r.json())
    assert "REDACTED" in json.dumps(r.json())
