"""Session JSONL adapter: incremental scan, idempotency, truncation recovery."""
import json
import time


def _pi_line(ts, role, text):
    return json.dumps({"type": "message", "timestamp": ts, "message": {
        "role": role, "content": [{"type": "text", "text": text}]}}) + "\n"


def _write(f, lines):
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("w") as fh:
        fh.writelines(lines)


def test_scan_twice_no_growth(env):
    from app import db
    from app.adapters import sessions
    d = "2026-10-03"
    f = env.PI_SESSIONS_DIR / "--home-x-Projects-RSI--sess-1" / "s.jsonl"
    _write(f, [_pi_line(f"{d}T09:00:00Z", "user", "fix the bug"),
               _pi_line(f"{d}T09:05:00Z", "assistant", "done")])
    r1 = sessions.scan("pi")
    r2 = sessions.scan("pi")
    n = db.q1("SELECT COUNT(*) AS n FROM source_events WHERE source_key LIKE 'sessions:%' OR source_key='pi'")["n"]
    assert r1["new"] >= 2  # messages (+ session meta if the parser emits it)
    assert r2["new"] == 0
    assert n == r1["new"]


def test_append_grows_by_new_lines_only(env):
    from app import db
    from app.adapters import sessions
    d = "2026-10-03"
    f = env.PI_SESSIONS_DIR / "--home-x-Projects-RSI--sess-2" / "s.jsonl"
    _write(f, [_pi_line(f"{d}T09:00:00Z", "user", "one")])
    sessions.scan("pi")
    _write(f, [_pi_line(f"{d}T09:00:00Z", "user", "one"),
               _pi_line(f"{d}T09:10:00Z", "user", "two")])
    r = sessions.scan("pi")
    assert r["new"] == 1
    assert r["days"] == [d]


def test_truncated_file_rescans(env):
    from app import db
    from app.adapters import sessions
    d = "2026-10-03"
    f = env.PI_SESSIONS_DIR / "--home-x-Projects-RSI--sess-3" / "s.jsonl"
    _write(f, [_pi_line(f"{d}T09:00:00Z", "user", "old-old-old")])
    sessions.scan("pi")
    # rotated: file replaced by a shorter different one
    _write(f, [_pi_line(f"{d}T10:00:00Z", "user", "new")])
    sessions.scan("pi")
    txt = db.q("SELECT payload FROM source_events WHERE source_key LIKE 'sessions:%' OR source_key='pi' "
               "AND event_type='msg_user'")
    all_text = " ".join(json.loads(r["payload"]).get("text", "") for r in txt)
    assert "old-old-old" not in all_text  # stale event gone via key change
    assert "new" in all_text


def test_trailing_partial_line_held_back(env):
    from app import db
    from app.adapters import sessions
    d = "2026-10-03"
    f = env.PI_SESSIONS_DIR / "--home-x-Projects-RSI--sess-4" / "s.jsonl"
    _write(f, [_pi_line(f"{d}T09:00:00Z", "user", "complete")])
    # simulate a file being written concurrently: append half a line
    with f.open("a") as fh:
        fh.write('{"type":"message","timesta')
    sessions.scan("pi")
    rows = db.q("SELECT payload FROM source_events WHERE source_key LIKE 'sessions:%' OR source_key='pi' "
                "AND event_type='msg_user'")
    texts = [json.loads(r["payload"]).get("text") for r in rows]
    assert "complete" in texts
    # the partial line must not have produced an event
    assert all(t in ("complete", None) for t in texts)


def test_secret_redaction(env):
    import os
    from app import reviews
    out = reviews._redact("key sk-ant-abc123def456 and ghp_abcdef123456 here")
    assert "sk-ant-abc123" not in out
    assert "ghp_abcdef" not in out
    assert "sk-ant***" in out and "ghp_***" in out


def test_review_parse(env):
    from app import reviews
    good = ('前言 {"prompt_goal":20,"prompt_context":18,"prompt_constraints":15,'
            '"prompt_acceptance":10,"exec_planning":12,"exec_tools":18,'
            '"exec_error_recovery":16,"exec_verification":25,"exec_efficiency":13,'
            '"summary":"ok","suggestions":["a","b"],"evidence_refs":[3]} 后记')
    d = reviews._parse(good)
    assert d and d["prompt_goal"] == 20 and d["exec_verification"] == 25
    assert d["suggestions"] == ["a", "b"]
    assert reviews._parse("no json here") is None
    assert reviews._parse('{"prompt_goal":999}') is None  # out of range rejected


def _claude_line(ts, text, cwd=None):
    d = {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}}
    if cwd:
        d["cwd"] = cwd
    return json.dumps(d) + "\n"


def test_claude_session_cwd_from_project_dir_incl_subagents(env, tmp_path):
    """Lines without cwd (and subagent transcripts nested two levels deeper)
    still resolve the project from the sanitized dir under the sessions root;
    hyphenated directory names are resolved against the filesystem."""
    from app import db
    from app.adapters import sessions
    proj = tmp_path / "Projects" / "ai-work-journal"
    proj.mkdir(parents=True)
    dirname = str(proj).replace("/", "-")
    d = "2026-10-03"
    main = env.CLAUDE_SESSIONS_DIR / dirname / "s1.jsonl"
    sub = env.CLAUDE_SESSIONS_DIR / dirname / "s1" / "subagents" / "agent-a.jsonl"
    _write(main, [_claude_line(f"{d}T09:00:00Z", "hi")])
    _write(sub, [_claude_line(f"{d}T09:01:00Z", "extract")])
    sessions.scan("claude")
    cwds = {r["cwd"] for r in db.q("SELECT cwd FROM agent_sessions WHERE source='claude'")}
    assert cwds == {str(proj)}


def test_claude_line_cwd_wins_over_dir(env):
    from app import db
    from app.adapters import sessions
    d = "2026-10-03"
    f = env.CLAUDE_SESSIONS_DIR / "-home-x-elsewhere" / "s2.jsonl"
    _write(f, [_claude_line(f"{d}T09:00:00Z", "hi", cwd="/home/x/Projects/RSI")])
    sessions.scan("claude")
    assert db.q1("SELECT cwd FROM agent_sessions WHERE source='claude'")["cwd"] == "/home/x/Projects/RSI"


def test_session_list_titles_and_folds_subagents(env, tmp_path):
    from fastapi.testclient import TestClient
    from app.api import app
    from app.adapters import sessions
    d = "2026-10-03"
    proj = env.CLAUDE_SESSIONS_DIR / "-home-x-Projects-RSI"
    _write(proj / "u1.jsonl", [
        _claude_line(f"{d}T01:00:00Z", "<command-name>/x</command-name>"),     # injected, not a title
        _claude_line(f"{d}T01:00:05Z", "复现 HarnessLens 的 BIRD 四格"),
        _claude_line(f"{d}T01:03:00Z", "再跑一遍验证"),
    ])
    _write(proj / "u1" / "subagents" / "agent-a.jsonl", [_claude_line(f"{d}T01:01:00Z", "extract papers 1-10")])
    sessions.scan("claude")
    c = TestClient(app)
    rows = c.get(f"/api/v1/sessions?day={d}").json()["sessions"]
    assert len(rows) == 1
    top = rows[0]
    assert top["title"] == "复现 HarnessLens 的 BIRD 四格" and top["prompts"] == 2
    assert len(top["children"]) == 1 and top["exec_sec"] == 180
    allr = c.get(f"/api/v1/sessions?day={d}&subagents=true").json()["sessions"]
    sub = next(r for r in allr if r["subagent"])
    assert sub["parent_id"] == top["id"] and sub["prompts"] == 0


def test_session_times_are_local(env):
    from fastapi.testclient import TestClient
    from app.api import app
    from app.adapters import sessions
    _write(env.CLAUDE_SESSIONS_DIR / "-home-x-Projects-RSI" / "u9.jsonl",
           [_claude_line("2026-10-03T05:20:00Z", "hello there agent")])
    sessions.scan("claude")
    r = TestClient(app).get("/api/v1/sessions?day=2026-10-03").json()["sessions"][0]
    assert r["start_at"][11:16] == "13:20"   # TZ in tests is Asia/Singapore (+08:00)
    d = TestClient(app).get(f"/api/v1/sessions/{r['id']}").json()
    assert d["events"][0]["at"][11:16] == "13:20"


def test_long_running_session_is_described_by_the_selected_day(env):
    """A session opened days ago and still going is listed by that day's work."""
    from fastapi.testclient import TestClient
    from app.api import app
    from app.adapters import sessions
    _write(env.CLAUDE_SESSIONS_DIR / "-home-x-Projects-RSI" / "long.jsonl", [
        _claude_line("2026-10-01T02:00:00Z", "first task from monday"),
        _claude_line("2026-10-03T01:00:00Z", "morning follow-up please"),
        _claude_line("2026-10-03T01:05:00Z", "latest instruction today"),
    ])
    sessions.scan("claude")
    r = TestClient(app).get("/api/v1/sessions?day=2026-10-03").json()["sessions"][0]
    assert r["title"] == "first task from monday"
    assert r["carried"] is True
    g = r["range"]
    assert g["prompts"] == 2 and g["last_prompt"] == "latest instruction today"
    assert g["first_at"][11:16] == "09:00" and g["last_at"][11:16] == "09:05"
    assert g["exec_sec"] == 300
    r1 = TestClient(app).get("/api/v1/sessions?day=2026-10-01").json()["sessions"][0]
    assert r1["carried"] is False and r1["range"]["prompts"] == 1
