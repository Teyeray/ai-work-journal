"""Work-bound and legacy-ID regressions; native sources are synthetic only."""
import datetime as dt
import json
import sqlite3
from pathlib import Path


def record(text, timestamp, kind="response_item"):
    return json.dumps({"type": kind, "timestamp": timestamp, "payload": {
        "type": "message", "role": "user", "content": text}}).encode() + b"\n"


def test_codex_chunks_parser_dedup_partial_and_file_failure(tmp_path, monkeypatch):
    import agent_sources as a
    now = dt.datetime.now(dt.timezone.utc)
    lo, hi = now - dt.timedelta(days=2), now + dt.timedelta(minutes=1)
    header = b'{"type":"session_meta","payload":{"id":"native","cwd":"/synthetic"}}\n'
    first = record("duplicate", now.isoformat())
    f = tmp_path / "active.jsonl"
    f.write_bytes(header + first)
    bad = tmp_path / "unreadable.jsonl"
    bad.write_bytes(first)
    original_open = Path.open
    reads = []
    def opened(path, *args, **kwargs):
        if path == bad:
            raise PermissionError("synthetic unreadable")
        reads.append(str(path))
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", opened)
    limits = {"codex_dir": str(tmp_path), "max_bytes": 10000, "sync_chunk_bytes": 300}
    found, cursor = a.collect_incremental("codex", lo, hi, limits, {})
    assert found[0]["cwd"] == "/synthetic"
    assert str(bad) not in cursor and str(bad) in limits["_errors"]
    committed = cursor[str(f)]["offset"]
    # Cross-offset event_msg duplicate uses exactly the reporting parser's last-two rule.
    with original_open(f, "ab") as fh:
        fh.write(json.dumps({"type": "event_msg", "timestamp": now.isoformat(),
                             "payload": {"type": "user_message", "message": "duplicate"}}).encode() + b"\n")
        fh.write(b'{"partial":')
    found, cursor = a.collect_incremental("codex", lo, hi, limits, cursor)
    assert not [t for s in found for t in s["turns"]]
    assert cursor[str(f)]["offset"] > committed
    safe = cursor[str(f)]["offset"]
    reads.clear()
    _, quiet = a.collect_incremental("codex", lo, hi, limits, cursor)
    assert str(f) not in reads and quiet[str(f)]["offset"] == safe
    with original_open(f, "ab") as fh:
        fh.write(b'true}\n' + record("old append", "2019-01-01T00:00:00Z"))
    found, cursor = a.collect_incremental("codex", lo, hi, limits, cursor)
    assert [t["x"] for s in found for t in s["turns"]] == ["old append"]
    assert cursor[str(f)]["offset"] == f.stat().st_size


def test_codex_record_larger_than_chunk_can_finish_and_resume(tmp_path):
    import agent_sources as a
    now = dt.datetime.now(dt.timezone.utc)
    lo, hi = now - dt.timedelta(days=2), now + dt.timedelta(minutes=1)
    f = tmp_path / "live.jsonl"
    header = b'{"type":"session_meta","payload":{"id":"native","cwd":"/synthetic"}}\n'
    f.write_bytes(header)
    limits = {"codex_dir": str(tmp_path), "max_bytes": 6000, "sync_chunk_bytes": 256}
    _, cursor = a.collect_incremental("codex", lo, hi, limits, {})
    with f.open("ab") as stream:
        stream.write(record("long evidence " + "x" * 3300, now.isoformat()))
        stream.write(record("next turn", now.isoformat()))
    found, cursor = a.collect_incremental("codex", lo, hi, limits, cursor)
    assert found[0]["turns"][0]["x"].startswith("long evidence ")
    assert not limits.get("_errors")
    found, cursor = a.collect_incremental("codex", lo, hi, limits, cursor)
    assert [turn["x"] for session in found for turn in session["turns"]] == ["next turn"]
    assert cursor[str(f)]["offset"] == f.stat().st_size


def test_codex_oversize_tail_resumes_bounded_work(tmp_path, monkeypatch):
    import agent_sources as a
    now = dt.datetime.now(dt.timezone.utc)
    lo, hi = now - dt.timedelta(days=2), now + dt.timedelta(minutes=1)
    f = tmp_path / "large.jsonl"
    header = b'{"type":"session_meta","payload":{"id":"native","cwd":"/synthetic"}}\n'
    f.write_bytes(header + b"".join(record(str(i), now.isoformat()) for i in range(100)))
    limits = {"codex_dir": str(tmp_path), "max_bytes": 1800, "sync_chunk_bytes": 450}
    opened, spent = Path.open, []
    class Meter:
        def __init__(self, fh):
            self.fh = fh
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.fh.close()
        def __getattr__(self, name):
            return getattr(self.fh, name)
        def read(self, count=-1):
            assert count >= 0
            data = self.fh.read(count)
            spent.append(len(data))
            return data
    monkeypatch.setattr(Path, "open", lambda path, *args, **kwargs: Meter(opened(path, *args, **kwargs)))
    cursor, turns = {}, []
    for _ in range(20):
        spent.clear()
        found, cursor = a.collect_incremental("codex", lo, hi, limits, cursor)
        assert sum(spent) <= limits["max_bytes"]
        turns.extend(t for s in found for t in s["turns"])
        assert all(s["native_id"] == "native" and s["cwd"] == "/synthetic" for s in found)
        if cursor[str(f)]["offset"] == f.stat().st_size:
            break
    assert cursor[str(f)]["offset"] == f.stat().st_size
    assert 0 < len(turns) < 20  # only the bounded tail, not all 100 records
    with f.open("ab") as fh:
        fh.write(record("future", now.isoformat()))
    found, _ = a.collect_incremental("codex", lo, hi, limits, cursor)
    assert [t["x"] for s in found for t in s["turns"]] == ["future"]


def test_legacy_pi_warmup_and_status_skip_payload_and_cursor_reads(env, monkeypatch):
    from app import db, ingest
    from app.adapters import sessions
    f = env.PI_SESSIONS_DIR / "s.jsonl"
    f.write_text(json.dumps({"type": "message", "timestamp": "2020-01-01T00:00:00Z",
                            "message": {"role": "user", "content": "legacy"}}) + "\n")
    sessions.scan("pi")
    sid = db.q1("SELECT id FROM agent_sessions")[0]
    eid = db.q1("SELECT id FROM source_events")[0]
    db.ex("DELETE FROM meta WHERE key='sessions:pi:projection-v2'")
    original = db.q
    def checked(sql, params=()):
        assert not ("source_events" in sql and ("payload" in sql or "json_extract" in sql))
        return original(sql, params)
    monkeypatch.setattr(db, "q", checked)
    sessions.scan("pi")
    assert db.q1("SELECT id FROM agent_sessions")[0] == sid
    assert db.q1("SELECT id FROM source_events")[0] == eid
    def status_checked(sql, params=()):
        assert "cursor_json" not in sql
        return original(sql, params)
    monkeypatch.setattr(db, "q", status_checked)
    ingest.status()


def native_db(path):
    con = sqlite3.connect(path)
    con.executescript('''PRAGMA journal_mode=WAL;
        CREATE TABLE session(id TEXT PRIMARY KEY,parent_id,directory,title,time_created,time_updated);
        CREATE TABLE message(id TEXT PRIMARY KEY,session_id,time_created,time_updated,data);
        CREATE INDEX message_session_time_id ON message(session_id,time_created,id);
        CREATE TABLE part(id TEXT PRIMARY KEY,session_id,message_id,time_created,time_updated,data);
        CREATE INDEX part_message ON part(message_id,id);
        INSERT INTO session VALUES('s',NULL,'/synthetic','work',1000,1000);''')
    return con


def test_opencode_empty_recent_scope_index_work_and_quiet(tmp_path, monkeypatch):
    import agent_sources as a
    path = tmp_path / "native.db"
    con = native_db(path)
    con.executemany("INSERT INTO message VALUES(?,'s',1000,1000,?)",
                    [(str(i), '{"role":"user","padding":"' + 'x' * 1000 + '"}') for i in range(5000)])
    con.commit()
    now = dt.datetime.now(dt.timezone.utc)
    lo, hi = now - dt.timedelta(days=2), now
    original = a._opencode_ro
    statements, work = [], []
    def opened(path):
        c = original(path)
        c.set_trace_callback(statements.append)
        c.set_progress_handler(lambda: work.append(1) or 0, 100)
        return c
    monkeypatch.setattr(a, "_opencode_ro", opened)
    found, cursor = a.collect_incremental("opencode", lo, hi, {"opencode_db": str(path)}, {})
    assert found == [] and cursor["digests"] == {}
    assert len(work) < 30  # no traversal of the 5,000 native message rows
    assert not any("FROM part" in sql or " data" in sql for sql in statements)
    assert len(json.dumps(cursor)) < 600
    monkeypatch.setattr(a, "_opencode_ro", lambda _: (_ for _ in ()).throw(AssertionError("quiet SQL")))
    assert a.collect_incremental("opencode", lo, hi, {"opencode_db": str(path)}, cursor)[0] == []
    con.close()


def test_opencode_stream_edit_preserves_legacy_ids_and_dates(env, tmp_path, monkeypatch):
    import agent_sources as a
    from app import db
    from app.adapters import sessions
    path = tmp_path / "native.db"
    con = native_db(path)
    # Newer session metadata without in-window messages must not crowd out the
    # active session whose own time_updated remains old.
    con.executemany("INSERT INTO session VALUES(?,NULL,'/synthetic','empty',2000,2000)",
                    [(f"empty-{i}",) for i in range(10)])
    now = sessions.now_utc()
    stamp = now.timestamp() * 1000
    con.execute("INSERT INTO message VALUES('m','s',?,1,?)", (stamp, '{"role":"assistant"}'))
    con.execute("INSERT INTO part VALUES('p','s','m',?,1,?)", (stamp, '{"type":"text","text":"draft"}'))
    con.commit()
    monkeypatch.setattr(a, "cfg", lambda: {"opencode_db": str(path), "codex_dir": "", "max_sessions": 1,
        "max_messages": 5000, "max_bytes": 100000, "max_files": 400})
    monkeypatch.setattr(a.llm_cli, "self_run_ids", lambda: set())
    monkeypatch.setattr(a.llm_cli, "self_run_marker", lambda: "self-journal-")
    sessions.scan_optional()
    sid = db.q1("SELECT id FROM agent_sessions")[0]
    event = db.q1("SELECT id,event_key,day_key FROM source_events WHERE event_type='msg_assistant'")
    # Emulate a live legacy row: same content key but no new native-part metadata.
    body = json.loads(db.q1("SELECT payload FROM source_events WHERE id=?", (event['id'],))[0])
    body.pop("native_event_id")
    body.pop("message_id")
    db.ex("UPDATE source_events SET payload=? WHERE id=?", (json.dumps(body), event['id']))
    db.ex("DELETE FROM source_cursors WHERE source_key LIKE 'sessions:opencode:%'")
    refresh = sessions._refresh_sessions
    def no_rebuild(source, running, changed):
        assert not changed, "unchanged legacy warmup must not rebuild historical projections"
        return refresh(source, running, changed)
    with monkeypatch.context() as patch:
        patch.setattr(sessions, "_refresh_sessions", no_rebuild)
        assert not sessions.scan_optional()["errors"]
    assert db.q1("SELECT id FROM source_events WHERE event_type='msg_assistant'")[0] == event['id']
    yesterday = stamp - 86400000
    con.execute("UPDATE message SET time_created=?,time_updated=2 WHERE id='m'", (yesterday,))
    con.execute("UPDATE part SET time_updated=2,data=?", ('{"type":"text","text":"final"}',))
    con.commit()
    result = sessions.scan_optional()
    assert db.q1("SELECT id FROM agent_sessions")[0] == sid
    assert db.q1("SELECT count(*) FROM source_events WHERE event_type='msg_assistant'")[0] == 1
    updated = db.q1("SELECT id,day_key,payload FROM source_events WHERE event_type='msg_assistant'")
    assert updated['id'] == event['id'] and json.loads(updated['payload'])['text'] == 'final'
    assert {event['day_key'], updated['day_key']} <= set(result['days'])
    # Moving out of the native time window does not delete historical evidence.
    con.execute("UPDATE message SET time_created=1000,time_updated=3")
    con.commit()
    sessions.scan_optional()
    assert db.q1("SELECT id FROM source_events WHERE event_type='msg_assistant'")[0] == event['id']
    con.close()
