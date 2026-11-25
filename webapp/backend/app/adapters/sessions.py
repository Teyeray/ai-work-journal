"""Agent session JSONL adapters (pi + claude-code), byte-offset incremental.

Idempotency: event_key = sha256(file_path|line_no|sha256(line)).
- append-only files: cursor stores byte offset; trailing partial line is held back
- truncation (size < offset): reset offset and rescan; content keys dedupe
- running session: file mtime within RUNNING_THRESHOLD_SEC

One source_event per JSONL line that belongs to a session and carries a
timestamp. Session rows are derived (upserted) from their events.
"""
import datetime as dt
import hashlib
import json
import os
import re
import time
from pathlib import Path

import agent_sources

from .. import config, db
from ..timeutil import iso, now_utc, local_day_key, parse_ts


def optional_sources_status() -> dict:
    """Which optional agent sources are configured/readable, and why not."""
    return agent_sources.status()


def scan_optional(days_back: int = 2) -> dict:
    """Incrementally ingest optional sources; days_back is retained for callers.

    Background checkpoints cover changed content irrespective of event age.
    Reporting collectors keep their separate time-bounded interfaces.
    """
    # Independent commits: a broken source/projection cannot roll back its sibling.
    results, errors = [], {}
    for agent in ("codex", "opencode"):
        try:
            with db.Tx():
                results.append(_scan_optional_locked(days_back, (agent,)))
        except Exception as exc:
            errors[agent] = str(exc)
    return {"source": "sessions:optional", "new": sum(r["new"] for r in results),
            "days": sorted({d for r in results for d in r["days"]}),
            "sessions": sum(r["sessions"] for r in results),
            "agents": sorted({a for r in results for a in r["agents"]}),
            "session_keys": [k for r in results for k in r.get("session_keys", [])],
            "skipped": {k: v for r in results for k, v in r["skipped"].items()},
            "errors": {**{k: v for r in results for k, v in r["errors"].items()}, **errors}}


def _scan_optional_locked(days_back, agents):
    sessions, checkpoints = [], {}
    settings = agent_sources.cfg()
    end = now_utc()
    start = end - dt.timedelta(days=days_back)
    errors = {}
    for agent in agents:
        key = "sessions:" + agent + ":incremental-v2"
        previous = db.q1("SELECT cursor_json FROM source_cursors WHERE source_key=?", (key,))
        previous = json.loads(previous["cursor_json"]) if previous else {}
        try:
            found, checkpoint = agent_sources.collect_incremental(agent, start, end, settings, previous)
        except Exception as exc:
            errors[agent] = str(exc)
            continue
        errors.update(settings.pop("_errors", {}))
        sessions.extend(found)
        if checkpoint != previous:
            checkpoints[key] = checkpoint
    status = agent_sources.status()
    skipped = {k: v["skip_reason"] for k, v in status.items()
               if isinstance(v, dict) and v.get("skip_reason")}
    if not sessions:
        for key, checkpoint in checkpoints.items():
            _save_cursor(key, checkpoint)
        return {"source": "sessions:optional", "new": 0, "days": [], "skipped": skipped,
                "sessions": 0, "agents": [], "errors": errors}
    by_source: dict = {}
    for s in sessions:
        by_source.setdefault(s["agent"], []).append(s)
    total_new, affected = 0, set()
    changed = {}
    with db.Tx():
        for agent, group in by_source.items():
            prefix = f"sessions:{agent}"
            for s in group:
                material = not db.q1("SELECT id FROM agent_sessions WHERE source=? AND external_id=?", (agent, s["file"]))
                native = s.get("native_id") or s["file"]
                meta = {"cwd": s.get("cwd"), "session_id": native, "native_id": native,
                        "native_parent_id": s.get("native_parent_id"),
                        "native_kind": s.get("native_kind") or "root",
                        "forked_from_id": s.get("forked_from_id"),
                        "user_channel": bool(s.get("user_channel", True)),
                        "self_run": bool(s.get("self_run", False)), "file": s["file"]}
                meta_key = _sha(f"{prefix}|meta|{native}")
                meta_body = json.dumps(meta, ensure_ascii=False)
                prior_meta = db.q1("SELECT id,payload,day_key FROM source_events WHERE source_key=? AND event_key=?", (prefix, meta_key))
                if not prior_meta:
                    db.ex("INSERT INTO source_events(source_key,event_key,event_type,occurred_at,observed_at,day_key,payload,created_at) VALUES(?,?,?,?,?,?,?,?)",
                          (prefix, meta_key, "session_meta", iso(s["first"]), iso(now_utc()), local_day_key(s["first"]), meta_body, iso(now_utc())))
                    total_new += 1
                    material = True
                    affected.add(local_day_key(s["first"]))
                elif prior_meta["payload"] != meta_body:
                    db.ex("UPDATE source_events SET payload=? WHERE id=?", (meta_body, prior_meta["id"]))
                    material = True
                    affected.add(prior_meta["day_key"])
                # Key lookups preserve legacy IDs without reading old session payloads.
                for turn in s.get("turns") or []:
                    ts = parse_ts(turn.get("t"))
                    if ts is None:
                        continue
                    role = turn.get("role") or "user"
                    event_type = "msg_" + (role if role in ("user", "assistant", "tool") else "assistant")
                    ev = _sha(f"{prefix}|{native}|{turn.get('native_event_id') or ''}|{turn.get('t')}|{role}|{turn.get('x', '')}")
                    body = json.dumps({"role": role, "success": turn.get("success"),
                        "text": (turn.get("x") or "")[:4000], "typed": bool(turn.get("typed", False)),
                        "file": s["file"], "native_event_id": turn.get("native_event_id"),
                        "message_id": turn.get("message_id")}, ensure_ascii=False)
                    old = db.q1("SELECT id,payload,day_key,occurred_at FROM source_events WHERE source_key=? AND event_key=?", (prefix, ev))
                    if not old and turn.get("native_event_id"):
                        old = db.q1("SELECT id,payload,day_key,occurred_at FROM source_events WHERE source_key=? "
                                     "AND json_extract(payload,'$.native_event_id')=?",
                                     (prefix, turn["native_event_id"]))
                    if old:
                        if old["payload"] != body or old["occurred_at"] != iso(ts):
                            db.ex("UPDATE source_events SET payload=?,occurred_at=?,day_key=?,event_type=? WHERE id=?",
                                  (body, iso(ts), local_day_key(ts), event_type, old["id"]))
                            previous_body = json.loads(old["payload"])
                            current_body = json.loads(body)
                            for field in ("native_event_id", "message_id"):
                                previous_body.pop(field, None)
                                current_body.pop(field, None)
                            if previous_body != current_body or old["occurred_at"] != iso(ts):
                                material = True
                                db.ex("DELETE FROM daily_projections WHERE day_key IN (?,?)",
                                  (old["day_key"], local_day_key(ts)))
                            affected.update((old["day_key"], local_day_key(ts)))
                        continue
                    r = db.ex("INSERT INTO source_events(source_key,event_key,event_type,"
                              "occurred_at,observed_at,day_key,payload,created_at) "
                              "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source_key,event_key) DO NOTHING",
                              (prefix, ev, event_type, iso(ts), iso(now_utc()),
                               local_day_key(ts),
                               body,
                               iso(now_utc())))
                    total_new += r.rowcount
                    material = material or bool(r.rowcount)
                    affected.add(local_day_key(ts))
                if material:
                    changed.setdefault(agent, set()).add(s["file"])
                # Bounded/incremental reads never authorize deletion of prior evidence.
        for agent in by_source:
            db.log_sync(f"sessions:{agent}", 0)
    for agent in by_source:
        _refresh_sessions(agent, [], changed.get(agent, set()))
    for key, checkpoint in checkpoints.items():
        _save_cursor(key, checkpoint)
    return {"source": "sessions:optional", "new": total_new, "days": sorted(affected),
            "skipped": skipped, "errors": errors, "sessions": len(sessions), "agents": sorted(by_source),
            "session_keys": [(agent, f) for agent, files in changed.items() for f in files]}


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()[:16]


def _iter_files(root):
    if not root:
        return
    p = Path(root)
    if p.exists():
        for f in sorted(p.rglob("*.jsonl")):
            yield f


def _cursors(prefix) -> dict:
    return {r["source_key"]: json.loads(r["cursor_json"])
            for r in db.q("SELECT source_key, cursor_json FROM source_cursors "
                          "WHERE source_key LIKE ?", (prefix + ":%",))}


def _save_cursor(source_key, cursor):
    db.ex("INSERT INTO source_cursors(source_key,cursor_json,revision,last_scan_at) "
          "VALUES(?,?,1,?) ON CONFLICT(source_key) DO UPDATE SET "
          "cursor_json=excluded.cursor_json, revision=revision+1, "
          "last_scan_at=excluded.last_scan_at",
          (source_key, json.dumps(cursor), iso(now_utc())))


# ---------------- line parsers (one per agent flavor) ----------------

def _enc(name: str) -> str:
    """Claude Code's directory naming: every non-alphanumeric character → '-'
    (compared with runs of '-' collapsed, since '/.' and ':\\' both make '--')."""
    return re.sub(r"-+", "-", re.sub(r"[^A-Za-z0-9-]", "-", name)).strip("-")


def _cwd_from_dir(name: str) -> str:
    """sanitized dir name → path: '-home-x-Projects-ai-work-journal' →
    /home/x/Projects/ai-work-journal; 'C--Users-x-proj' → C:\\Users\\x\\proj.
    The encoding is lossy ('-', '_', '.' all became '-'), so walk the disk:
    at each level take the longest run of parts that names an existing
    directory once encoded the same way."""
    root = Path("/")
    drive = re.match(r"^([A-Za-z])--(.*)$", name) if os.name == "nt" else None
    if drive:
        root, name = Path(drive.group(1) + ":\\"), drive.group(2)
    parts = [p for p in name.split("-") if p]
    path, i = root, 0
    while i < len(parts):
        try:
            kids = {_enc(c.name): c for c in path.iterdir() if c.is_dir()}
        except OSError:
            kids = {}
        for j in range(len(parts), i, -1):
            joined = "-".join(parts[i:j])
            exact = path / joined          # a real name beats an encoding collision (~/x vs ~/.x)
            cand = exact if exact.is_dir() else kids.get(joined)
            if cand is not None:
                path, i = cand, j
                break
        else:
            return str(path.joinpath(*parts[i:]))
    return str(path)


def _session_dir(f: str, root) -> str:
    """Directory directly under the sessions root (subagent transcripts live
    deeper, in <project-dir>/<session-uuid>/subagents/)."""
    p = Path(f)
    if not root:
        return p.parent.name
    root = Path(root)
    for parent in p.parents:
        if parent.parent == root:
            return parent.name
    return p.parent.name


def _first_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                return p.get("text") or ""
    return ""


def _parse_pi_line(d: dict):
    """→ (event_type, payload) or None."""
    t = d.get("type")
    ts = parse_ts(d.get("timestamp"))
    if t == "session":
        return "session_meta", {"cwd": d.get("cwd"), "session_id": d.get("sessionId")}
    if t == "message":
        msg = d.get("message") or {}
        role = msg.get("role")
        content = msg.get("content")
        payload = {"role": role, "model": msg.get("model"),
                   "usage": msg.get("usage"), "text": _first_text(content)[:4000]}
        parts = []
        if isinstance(content, list):
            for p in content:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "toolCall":
                    parts.append({"kind": "tool_call", "name": p.get("name"),
                                  "arguments": _short_args(p.get("arguments"))})
                elif p.get("type") == "thinking":
                    parts.append({"kind": "thinking", "len": len(p.get("thinking") or "")})
        if parts:
            payload["parts"] = parts
        return f"msg_{role}", payload
    return None


def _short_args(a):
    if not isinstance(a, dict):
        return {}
    out = {}
    for k, v in a.items():
        s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
        out[k] = s[:300]
    return out


def _tool_result_block(p):
    if isinstance(p, dict) and p.get("type") == "tool_result":
        return True
    return False


def _parse_claude_line(d: dict):
    t = d.get("type")
    ts = parse_ts(d.get("timestamp"))
    if t not in ("user", "assistant"):
        return None
    msg = d.get("message") or {}
    content = msg.get("content")
    if t == "assistant":
        payload = {"role": "assistant", "model": msg.get("model"),
                   "usage": msg.get("usage"), "text": _first_text(content)[:4000]}
        parts = []
        if isinstance(content, list):
            for p in content:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "tool_use":
                    parts.append({"kind": "tool_call", "name": p.get("name"),
                                  "arguments": _short_args(p.get("input"))})
                elif p.get("type") == "thinking":
                    parts.append({"kind": "thinking", "len": len(p.get("thinking") or "")})
        if parts:
            payload["parts"] = parts
        return "msg_assistant", payload
    # user line: real user text or tool_result blocks
    if isinstance(content, str):
        return "msg_user", {"role": "user", "text": content[:4000], "cwd": d.get("cwd")}
    if isinstance(content, list):
        texts, results = [], []
        for p in content:
            if not isinstance(p, dict):
                continue
            if p.get("type") == "text":
                texts.append(p.get("text") or "")
            elif _tool_result_block(p):
                c = p.get("content")
                txt = c if isinstance(c, str) else _first_text(c)
                results.append({"kind": "tool_result", "is_error": bool(p.get("is_error")),
                                "text": (txt or "")[:1500]})
        if results:
            return "msg_user", {"role": "user", "tool_results": results,
                                "text": ("".join(texts))[:1000]}
        if texts:
            return "msg_user", {"role": "user", "text": "".join(texts)[:4000]}
    return None


def scan(source: str) -> dict:
    """source: 'pi' | 'claude-code'. Incremental byte-offset scan."""
    if source == "pi":
        root, parser, kind = config.PI_SESSIONS_DIR, _parse_pi_line, "pi"
    else:
        root, parser, kind = config.CLAUDE_SESSIONS_DIR, _parse_claude_line, "claude"
    prefix = f"sessions:{source}"
    total_new, affected_days = 0, set()
    running_sessions = []
    changed = set()
    with db.Tx():
        cursors = _cursors(prefix)
        for f in _iter_files(root):
            key = f"{prefix}:{f}"
            try:
                st = f.stat()
            except OSError:
                continue
            cur = cursors.get(key) or {}
            offset = int(cur.get("offset", 0))
            fsize = st.st_size
            signature = [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]
            if cur.get("signature") == signature:
                if time.time() - st.st_mtime < config.RUNNING_THRESHOLD_SEC:
                    running_sessions.append(str(f))
                continue
            if (fsize < offset or (cur.get("signature") and
                    (cur["signature"][:2] != signature[:2] or
                     fsize <= cur.get("size", 0))) or
                    (cur and not cur.get("signature") and fsize <= cur.get("size", offset)
                     and cur.get("mtime") != st.st_mtime)):
                # truncated/rotated: drop stale events for this file, rescan whole
                stale = db.q("SELECT day_key FROM source_events WHERE source_key=? "
                             "AND json_extract(payload,'$.file')=?", (prefix, str(f)))
                for sr in stale:
                    if sr["day_key"]:
                        affected_days.add(sr["day_key"])
                db.ex("DELETE FROM session_event_links WHERE event_id IN (SELECT id FROM source_events WHERE source_key=? AND json_extract(payload,'$.file')=?)", (prefix, str(f)))
                db.ex("DELETE FROM source_events WHERE source_key=? "
                      "AND json_extract(payload,'$.file')=?", (prefix, str(f)))
                offset = 0
                changed.add(str(f))
            if fsize == offset and fsize > 0:
                # Upgrade a legacy byte cursor without reopening unchanged content.
                _save_cursor(key, {"offset": offset, "size": fsize,
                                   "mtime": st.st_mtime, "signature": signature})
                # no new bytes; still check running status
                if time.time() - st.st_mtime < config.RUNNING_THRESHOLD_SEC:
                    running_sessions.append(str(f))
                continue
            new_events, days, last_offset = 0, set(), offset
            with f.open("rb") as fh:
                fh.seek(offset)
                data = fh.read()
            if data:
                segments = data.split(b"\n")
                # the final segment has no trailing newline: an in-progress line,
                # hold it back (do not parse, do not advance the offset)
                complete, trailing = segments[:-1], segments[-1]
                pos = 0
                for raw in complete:
                    line_start = offset + pos
                    pos += len(raw) + 1
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # malformed line: skip, content keys stay unique
                    ts = parse_ts(d.get("timestamp"))
                    parsed = parser(d)
                    if parsed is None or ts is None:
                        continue
                    etype, payload = parsed
                    payload["file"] = str(f)
                    ek = _sha(f"{f}|{line_start}|{_sha(line)}")
                    r = db.ex("INSERT INTO source_events(source_key,event_key,event_type,"
                              "occurred_at,observed_at,day_key,payload,created_at) "
                              "VALUES(?,?,?,?,?,?,?,?) "
                              "ON CONFLICT(source_key,event_key) DO NOTHING",
                              (prefix, ek, etype, iso(ts), iso(now_utc()),
                               local_day_key(ts),
                               json.dumps(payload, ensure_ascii=False), iso(now_utc())))
                    if r.rowcount:
                        new_events += 1
                        affected_days.add(local_day_key(ts))
                last_offset = offset + pos
                commit_offset = last_offset  # trailing fragment stays uncommitted
            else:
                commit_offset = offset
            _save_cursor(key, {"offset": commit_offset, "size": st.st_size,
                               "mtime": st.st_mtime, "signature": signature})
            if new_events:
                changed.add(str(f))
            total_new += new_events
            if time.time() - st.st_mtime < config.RUNNING_THRESHOLD_SEC:
                running_sessions.append(str(f))
        # Status can revive on an unfinished append before it yields an event.
        for f in running_sessions:
            row = db.q1("SELECT status FROM agent_sessions WHERE source=? AND external_id=?", (source, f))
            if row and row["status"] != "running":
                changed.add(f)
        # Only running projections need expiry checks; no historical payload walk.
        for row in db.q("SELECT external_id FROM agent_sessions WHERE source=? AND status='running'", (source,)):
            if row["external_id"] not in running_sessions:
                changed.add(row["external_id"])
        marker = prefix + ":projection-v2"
        if not db.get_meta(marker):
            # Existing projections/IDs are authoritative during a legacy warmup.
            # Only repair files that have events but no projection at all.
            if not db.q1("SELECT id FROM agent_sessions WHERE source=? LIMIT 1", (source,)):
                changed.update(r["file"] for r in db.q(
                    "SELECT DISTINCT json_extract(payload,'$.file') AS file FROM source_events WHERE source_key=?", (prefix,)))
            db.set_meta(marker, "1")
        _refresh_sessions(source, running_sessions, changed)
        db.log_sync(prefix, total_new)
    return {"source": prefix, "new": total_new, "days": sorted(affected_days),
            "session_keys": [(source, f) for f in changed]}


def _refresh_sessions(source: str, running_files: list, changed=None):
    """(Re)build agent_sessions rows from stored events of this source."""
    prefix = f"sessions:{source}"
    if changed is None:
        changed = {r["file"] for r in db.q(
            "SELECT DISTINCT json_extract(payload,'$.file') AS file FROM source_events WHERE source_key=?",
            (prefix,))}
    if not changed:
        return
    rows = []
    for f in changed:
        events = db.q("SELECT id, event_type, occurred_at, payload FROM source_events "
                      "WHERE source_key=? AND json_extract(payload,'$.file')=? ORDER BY occurred_at,id",
                      (prefix, f))
        if not events:
            db.ex("DELETE FROM session_event_links WHERE session_id IN (SELECT id FROM agent_sessions WHERE source=? AND external_id=?)", (source, f))
            db.ex("DELETE FROM agent_sessions WHERE source=? AND external_id=?", (source, f))
        rows.extend(events)
    by_file = {}
    for r in rows:
        p = json.loads(r["payload"])
        f = p.get("file")
        if not f:
            continue
        s = by_file.setdefault(f, {"cwd": None, "events": []})
        if p.get("cwd") and (r["event_type"] == "session_meta" or not s["cwd"]):
            s["cwd"] = p["cwd"]
        s["events"].append((r["id"], r["event_type"], r["occurred_at"], p))
    for f, s in by_file.items():
        evs = s["events"]
        first, last = evs[0][2], evs[-1][2]
        status = "running" if f in running_files else "ended"
        cwd = s["cwd"]
        if not cwd:
            root = config.PI_SESSIONS_DIR if source == "pi" else config.CLAUDE_SESSIONS_DIR
            cwd = _cwd_from_dir(_session_dir(f, root))
        existing = db.q1("SELECT id, status, cwd, start_at, end_at FROM agent_sessions WHERE source=? AND external_id=?",
                         (source, f))
        if existing:
            if (existing["cwd"], existing["start_at"], existing["end_at"], existing["status"]) != (cwd, first, last, status):
                db.ex("UPDATE agent_sessions SET cwd=?, start_at=?, end_at=?, status=? WHERE id=?",
                      (cwd, first, last, status, existing["id"]))
            sid = existing["id"]
        else:
            db.ex("INSERT INTO agent_sessions(source,external_id,cwd,start_at,end_at,status) "
                  "VALUES(?,?,?,?,?,?)", (source, f, cwd, first, last, status))
            sid = db.q1("SELECT id FROM agent_sessions WHERE source=? AND external_id=?",
                        (source, f))["id"]
        # link events to session (idempotent)
        for i, (eid, etype, ts, p) in enumerate(evs):
            db.ex("INSERT INTO session_event_links(session_id,event_id,ordinal) "
                  "VALUES(?,?,?) ON CONFLICT(session_id,event_id) DO UPDATE SET ordinal=excluded.ordinal WHERE ordinal != excluded.ordinal",
                  (sid, eid, i))
    from ..ingest import _link_session_projects
    _link_session_projects([(source, f) for f in by_file])
