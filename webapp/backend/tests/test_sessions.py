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
