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
import time
from pathlib import Path

from .. import config, db
from ..timeutil import iso, now_utc, local_day_key, parse_ts


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()[:16]


def _iter_files(root):
    if not root:
        return
    p = Path(root)
    if p.exists():
        for f in sorted(p.rglob("*.jsonl")):
            yield f


def _cursors() -> dict:
    return {r["source_key"]: json.loads(r["cursor_json"])
            for r in db.q("SELECT source_key, cursor_json FROM source_cursors "
                          "WHERE source_key LIKE 'sessions:%'")}


def _save_cursor(source_key, cursor):
    db.ex("INSERT INTO source_cursors(source_key,cursor_json,revision,last_scan_at) "
          "VALUES(?,?,1,?) ON CONFLICT(source_key) DO UPDATE SET "
          "cursor_json=excluded.cursor_json, revision=revision+1, "
          "last_scan_at=excluded.last_scan_at",
          (source_key, json.dumps(cursor), iso(now_utc())))


# ---------------- line parsers (one per agent flavor) ----------------

def _cwd_from_dir(name: str) -> str:
    """sanitized dir name → path guess: '-home-x-Projects-RSI' → /home/x/Projects/RSI."""
    parts = [p for p in name.replace("--", "-").split("-") if p]
    return "/" + "/".join(parts)


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
        return "msg_user", {"role": "user", "text": content[:4000]}
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
    with db.Tx():
        cursors = _cursors()
        for f in _iter_files(root):
            key = f"{prefix}:{f}"
            try:
                st = f.stat()
            except OSError:
                continue
            cur = cursors.get(key) or {}
            offset = int(cur.get("offset", 0))
            fsize = st.st_size
            if fsize < offset:  # truncated/rotated: rescan whole file
                offset = 0
            if fsize == offset and fsize > 0:
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
                               "mtime": st.st_mtime})
            total_new += new_events
            if time.time() - st.st_mtime < config.RUNNING_THRESHOLD_SEC:
                running_sessions.append(str(f))
        db.log_sync(prefix, total_new)
    _refresh_sessions(source, running_sessions)
    return {"source": prefix, "new": total_new, "days": sorted(affected_days)}


def _refresh_sessions(source: str, running_files: list):
    """(Re)build agent_sessions rows from stored events of this source."""
    prefix = f"sessions:{source}"
    rows = db.q("SELECT id, event_type, occurred_at, payload FROM source_events "
                "WHERE source_key=? ORDER BY occurred_at", (prefix,))
    by_file = {}
    for r in rows:
        p = json.loads(r["payload"])
        f = p.get("file")
        if not f:
            continue
        s = by_file.setdefault(f, {"cwd": None, "events": []})
        if r["event_type"] == "session_meta" and p.get("cwd"):
            s["cwd"] = p["cwd"]
        s["events"].append((r["id"], r["event_type"], r["occurred_at"], p))
    now = iso(now_utc())
    for f, s in by_file.items():
        evs = s["events"]
        first, last = evs[0][2], evs[-1][2]
        status = "running" if f in running_files else "ended"
        cwd = s["cwd"]
        if not cwd and source != "pi":
            cwd = _cwd_from_dir(Path(f).parent.name)
        if not cwd and source == "pi":
            cwd = _cwd_from_dir(Path(f).parent.name.replace("--", "-"))
        existing = db.q1("SELECT id, status FROM agent_sessions WHERE source=? AND external_id=?",
                         (source, f))
        if existing:
            db.ex("UPDATE agent_sessions SET cwd=?, start_at=?, end_at=?, status=? WHERE id=?",
                  (s["cwd"], first, last, status, existing["id"]))
            sid = existing["id"]
        else:
            db.ex("INSERT INTO agent_sessions(source,external_id,cwd,start_at,end_at,status) "
                  "VALUES(?,?,?,?,?,?)", (source, f, s["cwd"], first, last, status))
            sid = db.q1("SELECT id FROM agent_sessions WHERE source=? AND external_id=?",
                        (source, f))["id"]
        # link events to session (idempotent)
        for i, (eid, etype, ts, p) in enumerate(evs):
            db.ex("INSERT INTO session_event_links(session_id,event_id,ordinal) "
                  "VALUES(?,?,?) ON CONFLICT(session_id,event_id) DO NOTHING",
                  (sid, eid, i))
