"""Synthetic-only regression tests for persistent incremental dashboard ingest."""
import datetime as dt
import json
import sqlite3

import pytest


def line(text, ts="2026-01-01T00:00:00Z"):
    return json.dumps({"type": "message", "timestamp": ts,
                       "message": {"role": "user", "content": text}}) + "\n"


def test_quiet_partial_and_restart(env, monkeypatch):
    from app import db
    from app.adapters import sessions
    f = env.PI_SESSIONS_DIR / "s.jsonl"
    f.write_text(line("one") + '{"partial":')
    sessions.scan("pi")
    cursor = db.q1("SELECT cursor_json FROM source_cursors")[0]
    original = sessions._parse_pi_line
    monkeypatch.setattr(sessions, "_parse_pi_line", lambda _: pytest.fail("quiet parse"))
    original_refresh = sessions._refresh_sessions
    monkeypatch.setattr(sessions, "_refresh_sessions", lambda source, running, changed: (
        pytest.fail("quiet projection") if changed else None))
    db.connect().close()
    db._LOCAL.conn = None
    sessions.scan("pi")
    assert db.q1("SELECT cursor_json FROM source_cursors")[0] == cursor
    monkeypatch.setattr(sessions, "_parse_pi_line", original)
    monkeypatch.setattr(sessions, "_refresh_sessions", original_refresh)
    with f.open("a") as fh:
        fh.write('true}\n' + line("older", "2025-12-31T00:00:00Z"))
    assert sessions.scan("pi")["new"] == 1
    assert [r[0] for r in db.q("SELECT ordinal FROM session_event_links ORDER BY ordinal")] == [0, 1]


def test_single_append_refreshes_only_changed_session(env, monkeypatch):
    from app.adapters import sessions
    f = env.PI_SESSIONS_DIR / "a.jsonl"
    other = env.PI_SESSIONS_DIR / "b.jsonl"
    f.write_text(line("one"))
    other.write_text(line("unchanged"))
    sessions.scan("pi")
    original = sessions._refresh_sessions
    calls = []
    def checked(source, running, changed):
        calls.append(set(changed))
        return original(source, running, changed)
    monkeypatch.setattr(sessions, "_refresh_sessions", checked)
    with f.open("a") as fh:
        fh.write(line("two"))
    assert sessions.scan("pi")["new"] == 1
    assert calls == [{str(f)}]


def test_same_size_rewrite_and_expiry(env, monkeypatch):
    from app import db
    from app.adapters import sessions
    f = env.PI_SESSIONS_DIR / "s.jsonl"
    f.write_text(line("one"))
    sessions.scan("pi")
    f.write_text(line("two"))
    sessions.scan("pi")
    payloads = db.q("SELECT payload FROM source_events")
    assert len(payloads) == 1 and json.loads(payloads[0][0])["text"] == "two"
    monkeypatch.setattr(sessions.time, "time", lambda: f.stat().st_mtime + env.RUNNING_THRESHOLD_SEC + 1)
    sessions.scan("pi")
    assert db.q1("SELECT status FROM agent_sessions")[0] == "ended"


def test_projection_failure_rolls_back_cursor(env, monkeypatch):
    from app import db
    from app.adapters import sessions
    f = env.PI_SESSIONS_DIR / "s.jsonl"
    f.write_text(line("one"))
    original = sessions._refresh_sessions
    monkeypatch.setattr(sessions, "_refresh_sessions", lambda *a: (_ for _ in ()).throw(RuntimeError()))
    with pytest.raises(RuntimeError):
        sessions.scan("pi")
    assert not db.q("SELECT * FROM source_cursors")
    assert not db.q("SELECT * FROM source_events")
    monkeypatch.setattr(sessions, "_refresh_sessions", original)
    assert sessions.scan("pi")["new"] == 1


def test_codex_quiet_and_old_append(tmp_path, monkeypatch):
    import agent_sources as a
    f = tmp_path / "old.jsonl"
    f.write_text(json.dumps({"type": "session_meta", "payload": {"id": "s", "cwd": "/synthetic"}}) + "\n" +
                 json.dumps({"type": "response_item", "timestamp": "2020-01-01T00:00:00Z", "payload": {
                     "type": "message", "role": "user", "content": "one"}}) + "\n")
    lo, hi = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc), dt.datetime(9999, 1, 1, tzinfo=dt.timezone.utc)
    limits = {"codex_dir": str(tmp_path), "max_bytes": 100000}
    found, checkpoint = a.collect_incremental("codex", lo, hi, limits, {})
    assert found[0]["turns"][0]["x"] == "one"
    original = a._codex_file
    monkeypatch.setattr(a, "_codex_file", lambda *args: pytest.fail("quiet Codex content"))
    assert a.collect_incremental("codex", lo, hi, limits, checkpoint)[0] == []
    monkeypatch.setattr(a, "_codex_file", original)
    with f.open("a") as fh:
        fh.write(json.dumps({"type": "response_item", "timestamp": "2019-01-01T00:00:00Z", "payload": {
            "type": "message", "role": "assistant", "content": "two"}}) + "\n")
    assert [t["x"] for t in a.collect_incremental("codex", lo, hi, limits, checkpoint)[0][0]["turns"]] == ["two"]


def test_optional_reconcile_and_quiet(env, monkeypatch):
    import agent_sources as a
    from app import db
    from app.adapters import sessions
    f = env.PI_SESSIONS_DIR / "codex.jsonl"
    def write(text):
        f.write_text(json.dumps({"type": "session_meta", "payload": {"id": "s"}}) + "\n" +
                     json.dumps({"type": "response_item", "timestamp": sessions.now_utc().isoformat(), "payload": {
                         "type": "message", "role": "assistant", "content": text}}) + "\n")
    monkeypatch.setattr(a, "cfg", lambda: {"codex_dir": str(f.parent), "opencode_db": "",
        "max_bytes": 100000, "max_files": 400, "max_sessions": 400, "max_messages": 5000})
    write("draft")
    sessions.scan_optional()
    sid = db.q1("SELECT id FROM agent_sessions")[0]
    write("final")
    sessions.scan_optional()
    assert db.q1("SELECT id FROM agent_sessions")[0] == sid
    # Rewrites archive earlier evidence rather than deleting unseen history.
    assert db.q1("SELECT count(*) FROM source_events WHERE event_type='msg_assistant'")[0] == 2
    assert db.q1("SELECT count(*) FROM session_event_links")[0] == 3
    monkeypatch.setattr(a, "_codex_file", lambda *args: pytest.fail("quiet body"))
    monkeypatch.setattr(sessions, "_refresh_sessions", lambda *args: pytest.fail("quiet projection"))
    assert sessions.scan_optional()["new"] == 0


def test_optional_failure_does_not_advance_or_rollback_sibling(env, monkeypatch):
    import agent_sources as a
    from app import db
    from app.adapters import sessions
    monkeypatch.setattr(a, "cfg", lambda: {})
    monkeypatch.setattr(a, "status", lambda: {})
    def collect(agent, *args):
        if agent == "codex":
            raise RuntimeError("synthetic source failure")
        return [], {"synthetic": "safe"}
    monkeypatch.setattr(a, "collect_incremental", collect)
    result = sessions.scan_optional()
    assert "codex" in result["errors"]
    assert not db.q("SELECT * FROM source_cursors WHERE source_key LIKE 'sessions:codex:%'")
    assert db.q1("SELECT cursor_json FROM source_cursors WHERE source_key LIKE 'sessions:opencode:%'")[0] == '{"synthetic": "safe"}'


def test_git_quiet_skips_log(env, tmp_path, monkeypatch):
    import subprocess
    from app.adapters import git
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Synthetic", "-c", "user.email=test@example.invalid",
                    "commit", "--allow-empty", "-m", "synthetic"], check=True, capture_output=True)
    monkeypatch.setattr(git, "_repos", lambda: [repo])
    assert git.scan()["new"] == 1
    run = subprocess.run
    def checked(args, **kwargs):
        assert "log" not in args
        return run(args, **kwargs)
    monkeypatch.setattr(subprocess, "run", checked)
    assert git.scan()["new"] == 0


def test_opencode_part_edit_without_session_update(tmp_path, monkeypatch):
    import agent_sources as a
    path = tmp_path / "oc.db"
    con = sqlite3.connect(path)
    con.executescript('''CREATE TABLE session(id, parent_id, directory, title, time_created, time_updated);
        CREATE TABLE message(id, session_id, time_created, time_updated, data);
        CREATE TABLE part(id, message_id, time_created, time_updated, data);
        INSERT INTO session VALUES('s',NULL,'/synthetic','task',1000,1000);
        INSERT INTO message VALUES('m','s',1000,1000,'{"role":"assistant"}');
        INSERT INTO part VALUES('p','m',1000,1000,'{"type":"text","text":"draft"}');''')
    con.commit()
    monkeypatch.setattr(a.llm_cli, "self_run_ids", lambda: set())
    monkeypatch.setattr(a.llm_cli, "self_run_marker", lambda: "journal-self-")
    monkeypatch.setattr(a, "cfg", lambda: {})
    limits = {"opencode_db": str(path)}
    lo, hi = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc), dt.datetime(9999, 1, 1, tzinfo=dt.timezone.utc)
    found, checkpoint = a.collect_incremental("opencode", lo, hi, limits, {})
    assert found[0]["turns"][0]["x"] == "draft"
    original = a.collect_opencode
    monkeypatch.setattr(a, "collect_opencode", lambda *args: pytest.fail("quiet OpenCode body query"))
    assert a.collect_incremental("opencode", lo, hi, limits, checkpoint)[0] == []
    monkeypatch.setattr(a, "collect_opencode", original)
    con.execute("UPDATE part SET time_updated=2000,data=?", ('{"type":"text","text":"final"}',))
    con.commit()
    found, _ = a.collect_incremental("opencode", lo, hi, limits, checkpoint)
    assert found[0]["turns"][0]["x"] == "final"
    con.close()
