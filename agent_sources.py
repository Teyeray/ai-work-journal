#!/usr/bin/env python3
"""Optional agent-session sources beyond Claude Code and pi.

    codex     ~/.codex/sessions/**/rollout-*.jsonl   (Codex CLI rollout logs)
    opencode  ~/.local/share/opencode/opencode.db   (read-only SQLite)

Both are opt-in: set `codex_dir` / `opencode_db` in config.local.json, or leave
them empty to skip the source. Skipping is always reported, never silent - see
`status()`.

What these readers promise
--------------------------
* Native session ids, the native parent id and whether a session is a fork are
  carried through as separate fields (`native_id`, `native_parent_id`,
  `native_kind`). A fork is never silently treated as a continuation.
* Timestamps come from the record that *created* the message, so a session that
  runs past midnight splits correctly across days.
* A message that merely arrives on the user channel is labelled `typed: False`;
  it is not evidence that a human typed it.
* Nothing is ever written back: the JSONL is opened read-only and the SQLite
  file is opened with `mode=ro`, so the agent's own log and database are never
  modified. Auth files (`auth.json`) are never opened.
* Reads are bounded: at most `max_files` files per source, at most `max_bytes`
  per JSONL file, and the SQLite query is limited by an explicit
  `time_updated >=` cutoff plus a row cap.
"""
from __future__ import annotations

import contextlib
import hashlib
import datetime as dt
import json
import os
import sqlite3
from pathlib import Path

import llm_cli

MAX_TEXT = 4000
DEFAULT_MAX_FILES = 400
DEFAULT_MAX_BYTES = 24 * 1024 * 1024


def cfg() -> dict:
    import osutil
    c = osutil.load_config(Path(__file__).resolve().parent, "config.json")
    settings = {
        "codex_dir": c.get("codex_dir") or "",
        "opencode_db": c.get("opencode_db") or "",
        "max_files": DEFAULT_MAX_FILES,
        "max_bytes": DEFAULT_MAX_BYTES,
        "max_messages": 5000,
        "max_sessions": 400,
        **(c.get("agent_sources") or {}),
    }
    if os.environ.get("JOURNAL_DEMO"):
        settings.update(codex_dir="", opencode_db="")
    return settings


def _expand(v):
    return os.path.expanduser(os.path.expandvars(str(v))) if v else ""


def _clip(v, n=MAX_TEXT):
    text = str(v or "").strip()
    if len(text) <= n:
        return text
    half = max(1, (n - 24) // 2)
    return text[:half] + "\n[中段省略，保留首尾]\n" + text[-half:]


def _blank(agent, ext_id, native_id):
    return {"agent": agent, "file": ext_id, "native_id": native_id,
            "native_parent_id": None, "native_kind": "root",
            "cwd": "", "task": None, "user_msgs": 0, "assistant_msgs": 0,
            "tools": {}, "tool_errors": 0, "files": [], "turns": [],
            "user_channel": True, "self_run": False, "first": None, "last": None}


def _parse_ts(v):
    if not v:
        return None
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def _within(ts, start, end):
    return ts is not None and start <= ts < end


def _finish(s, first, last):
    s["first"], s["last"] = first, last
    s["span"] = round((last - first).total_seconds())
    s["turns"] = sorted(s["turns"], key=lambda t: t["t"])
    return s


# ---------------------------------------------------------------- codex JSONL

def _codex_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text") or "") for p in content
                         if isinstance(p, dict)
                         and p.get("type") in ("input_text", "output_text", "text"))
    return ""


def _codex_file(path: Path, start, end, limits, *, state=None, lines=None) -> list:
    """Sessions in one rollout file that recorded at least one item on this day."""
    sessions: dict = {}
    order: list = []
    active_sid = (state or {}).get("active_sid", path.stem)
    seeded = len((state or {}).get("tails", []))
    if state and state.get("meta"):
        sessions[active_sid] = {**_blank("codex", f"codex:{path}#{active_sid}", active_sid),
                                **state["meta"], "turns": list(state.get("tails", []))}
        order.append(active_sid)
    try:
        size = path.stat().st_size if lines is None else 0
    except OSError:
        return []
    with (path.open("rb") if lines is None else contextlib.nullcontext(lines)) as fh:
        for raw in fh:
            # Incremental callers can supply a per-record lower bound at bootstrap's boundary.
            line_start = start
            if isinstance(raw, tuple):
                raw, line_start = raw
            if lines is None and size > limits["max_bytes"]:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = _parse_ts(d.get("timestamp"))
            t = d.get("type")
            p = d.get("payload") or {}
            if t == "session_meta":
                sid = str(p.get("id") or p.get("session_id") or path.stem)
                active_sid = sid
                s = sessions.get(sid) or _blank("codex", f"codex:{path}#{sid}", sid)
                if p.get("cwd"):
                    s["cwd"] = str(p["cwd"])
                s["forked_from_id"] = p.get("forked_from_id")
                src = p.get("source")
                if isinstance(src, dict):
                    spawn = ((src.get("subagent") or {}).get("thread_spawn") or {})
                    s["native_parent_id"] = spawn.get("parent_thread_id")
                s["native_kind"] = "child" if s["native_parent_id"] else ("fork" if s["forked_from_id"] else "root")
                sessions[sid] = s
                if sid not in order:
                    order.append(sid)
                continue
            if not _within(ts, line_start, end):
                continue
            sid = active_sid
            s = sessions.get(sid) or _blank("codex", f"codex:{path}#{sid}", sid)
            if sid not in order:
                order.append(sid)
            if s["first"] is None or ts < s["first"]:
                s["first"] = ts
            if s["last"] is None or ts > s["last"]:
                s["last"] = ts
            if not s["cwd"] and p.get("cwd"):
                s["cwd"] = str(p["cwd"])
            if t == "event_msg" and p.get("type") in ("user_message", "user_turn") and not any(v.get("role") == "user" and v.get("x", "")[:300] == _clip(_codex_text(p.get("message") or p.get("text")))[:300] for v in s["turns"][-2:]):
                txt = _clip(_codex_text(p.get("message") or p.get("text")))
                if txt:
                    s["turns"].append({"t": ts.isoformat(), "x": txt[:300], "typed": False, "role": "user"})
            if t == "response_item":
                rt, role = p.get("type"), p.get("role")
                if rt == "message" and role in ("user", "assistant"):
                    txt = _clip(_codex_text(p.get("content")))
                    if not txt:
                        continue
                    if role == "user":
                        s["user_msgs"] += 1
                        if s["task"] is None:
                            s["task"] = txt[:200]
                    else:
                        s["assistant_msgs"] += 1
                    s["turns"].append({"t": ts.isoformat(), "x": _clip(txt, 1200), "typed": False, "role": role})
                elif rt in ("function_call", "local_shell_call", "custom_tool_call"):
                    name = str(p.get("name") or rt)
                    s["tools"][name] = s["tools"].get(name, 0) + 1
                    args = p.get("arguments") or p.get("input") or ""
                    try:
                        a = json.loads(args) if isinstance(args, str) else args
                    except ValueError:
                        a = {}
                    if isinstance(a, dict):
                        for key in ("file_path", "path", "workdir", "cwd"):
                            if isinstance(a.get(key), str):
                                s["files"].append(a[key])
                elif rt in ("function_call_output", "local_shell_call_output"):
                    out = p.get("output")
                    body = out if isinstance(out, str) else json.dumps(out or "", ensure_ascii=False)
                    s["turns"].append({"t": ts.isoformat(), "x": _clip(body, 1200), "typed": False,
                                       "role": "tool", "call_id": p.get("call_id")})
                    if body.lstrip().startswith('{"success":false') or '"success": false' in body[:200]:
                        s["tool_errors"] += 1
    if state is not None:
        active = sessions.get(active_sid) or _blank("codex", f"codex:{path}#{active_sid}", active_sid)
        state.update(active_sid=active_sid,
                     meta={k: active.get(k) for k in ("cwd", "native_parent_id", "native_kind", "forked_from_id")},
                     tails=active["turns"][-2:])
        if seeded and order:
            sessions[order[0]]["turns"] = sessions[order[0]]["turns"][seeded:]
    return [_finish(sessions[sid], sessions[sid]["first"], sessions[sid]["last"])
            for sid in order if sessions[sid].get("first") is not None]


def collect_codex(start, end, limits=None) -> list:
    limits = {**cfg(), **(limits or {})}
    root = _expand(limits.get("codex_dir"))
    if not root or not Path(root).is_dir():
        return []
    # Long-running sessions can live under an old creation-date directory.
    # Select recently modified files, not lexicographically newest filenames.
    def modified(path):
        try:
            return path.stat().st_mtime
        except OSError:
            return 0
    files = sorted(Path(root).rglob("*.jsonl"), key=lambda path: (modified(path), str(path)))
    if len(files) > limits["max_files"]:
        files = files[-limits["max_files"]:]
    out = []
    for f in files:
        try:
            if dt.datetime.fromtimestamp(f.stat().st_mtime, start.tzinfo) < start:
                continue
        except OSError:
            continue
        out.extend(_codex_file(f, start, end, limits))
    return out


# ---------------------------------------------------------------- opencode SQLite

def _opencode_ro(db_path: str) -> sqlite3.Connection:
    uri = "file:" + db_path.replace("?", "%3f").replace("#", "%23") + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def collect_opencode(start, end, limits=None) -> list:
    limits = {**cfg(), **(limits or {})}
    db_path = _expand(limits.get("opencode_db"))
    if not db_path or not os.path.isfile(db_path):
        return []
    lo, hi = start.timestamp() * 1000, end.timestamp() * 1000
    self_ids = llm_cli.self_run_ids()
    marker = llm_cli.self_run_marker()
    try:
        con = limits.get("connection") or _opencode_ro(db_path)
    except sqlite3.Error:
        if limits.get("strict"):
            raise
        return []
    out = []
    try:
        rows = []
        if limits.get("session_ids") is not None:
            for sid in sorted(limits["session_ids"]):
                rows.extend(con.execute("SELECT id,parent_id,directory,title,time_created,time_updated "
                                        "FROM session WHERE id=?", (sid,)).fetchall())
        else:
            rows = con.execute(
                "SELECT s.id, s.parent_id, s.directory, s.title, s.time_created, s.time_updated "
                "FROM session s WHERE EXISTS (SELECT 1 FROM message m WHERE m.session_id=s.id "
                "AND m.time_created>=? AND m.time_created<?) "
                "ORDER BY s.time_updated DESC LIMIT ?", (lo, hi, int(limits.get("max_sessions", 400)))).fetchall()
        for r in rows:
            sid = str(r["id"])
            if limits.get("session_ids") is not None and sid not in limits["session_ids"]:
                continue
            if sid in self_ids or str(r["title"] or "").startswith(marker):
                continue                      # the journal's own report run
            s = _blank("opencode", f"opencode:{sid}", sid)
            s["cwd"] = str(r["directory"] or "")
            s["native_parent_id"] = str(r["parent_id"]) if r["parent_id"] else None
            s["native_kind"] = "child" if r["parent_id"] else "root"
            msgs = con.execute(
                "SELECT id, time_created, data FROM message WHERE session_id = ? "
                "AND time_created >= ? AND time_created < ? ORDER BY time_created, id LIMIT ?",
                (sid, lo, hi, int(limits.get("max_messages", 5000)))).fetchall()
            first = last = None
            for m in msgs:
                ts = dt.datetime.fromtimestamp((m["time_created"] or 0) / 1000, start.tzinfo)
                first = ts if first is None else min(first, ts)
                last = ts if last is None else max(last, ts)
                try:
                    md = json.loads(m["data"] or "{}")
                except json.JSONDecodeError:
                    continue
                role = md.get("role")
                if role not in ("user", "assistant"):
                    continue
                s["user_msgs" if role == "user" else "assistant_msgs"] += 1
                if md.get("modelID"):
                    s["model"] = md["modelID"]
                for pr in con.execute(
                        "SELECT id, data FROM part WHERE message_id = ? ORDER BY time_created, id LIMIT ?",
                        (m["id"], int(limits.get("max_parts", 500)))).fetchall():
                    try:
                        pd = json.loads(pr["data"] or "{}")
                    except json.JSONDecodeError:
                        continue
                    kind = pd.get("type")
                    if kind == "text":
                        txt = _clip(pd.get("text"), 1200)
                        if txt:
                            s["turns"].append({"t": ts.isoformat(), "x": txt, "typed": False,
                                               "role": role, "native_event_id": str(pr["id"]), "message_id": str(m["id"])})
                    elif kind in ("tool", "tool-invocation"):
                        s["tools"][str(pd.get("tool") or pd.get("name") or "tool")] = \
                            s["tools"].get(str(pd.get("tool") or pd.get("name") or "tool"), 0) + 1
                        st = pd.get("state") or {}
                        if isinstance(st, dict):
                            if st.get("status") == "error":
                                s["tool_errors"] += 1
                            if st.get("output"):
                                s["turns"].append({"t": ts.isoformat(), "x": _clip(st["output"], 1200),
                                                   "role": "tool", "typed": False,
                                                   "native_event_id": str(pr["id"]), "success": st.get("status") == "completed"})
                            a = st.get("input")
                            if isinstance(a, dict):
                                for key in ("filePath", "path", "file"):
                                    if isinstance(a.get(key), str):
                                        s["files"].append(a[key])
            if first is None:
                continue
            s["task"] = (str(r["title"] or "")[:200]) or None
            s["files"] = sorted(set(s["files"]))[:15]
            out.append(_finish(s, first, last))
    except sqlite3.Error:
        if limits.get("strict"):
            raise
        return out
    finally:
        if not limits.get("connection"):
            con.close()
    return out


# ---------------------------------------------------------------- incremental dashboard collection

def collect_incremental(agent, start, end, limits, previous):
    """Bounded dashboard reader; commit returned checkpoint with ingested events."""
    if agent == "codex":
        return _codex_incremental(start, end, limits, previous)
    return _opencode_incremental(start, end, limits, previous)


def _codex_incremental(start, end, limits, previous):
    import copy
    root = _expand(limits.get("codex_dir"))
    if not root or not Path(root).is_dir():
        return [], previous
    inventory, out = copy.deepcopy(previous), []
    candidates = []
    for path in Path(root).rglob("*.jsonl"):
        try:
            st = path.stat()
            cur = previous.get(str(path), {})
            if st.st_mtime >= start.timestamp() or isinstance(cur, dict) and cur.get("offset", 0) < cur.get("bootstrap_end", 0):
                candidates.append((st.st_mtime, str(path), st))
        except OSError as exc:
            limits.setdefault("_errors", {})[str(path)] = str(exc)
    budget = int(limits["max_bytes"])
    for _, name, st in sorted(candidates, reverse=True)[:int(limits.get("max_files", 400))]:
        if budget <= 0:
            break
        path = Path(name)
        cur = inventory.get(name, {})
        if not isinstance(cur, dict):  # upgrade the obsolete stat-only inventory
            cur = {}
        identity = [st.st_dev, st.st_ino]
        sig = [*identity, st.st_size, st.st_mtime_ns, st.st_ctime_ns]
        if cur.get("signature") == sig and cur.get("offset", 0) >= st.st_size:
            continue
        if cur.get("signature") == sig and cur.get("partial"):
            continue
        if (cur.get("identity") != identity or st.st_size < cur.get("offset", 0)
                or cur.get("signature") and sig != cur["signature"] and st.st_size <= cur["signature"][2]):
            cur = {}
        cap = int(limits["max_bytes"])
        header_allowance = min(65536, max(1, cap // 4))
        tail_start = st.st_size - (cap - header_allowance) if st.st_size > cap else 0
        offset = cur.get("offset", max(0, tail_start))
        initial_end = cur.get("bootstrap_end", st.st_size)
        count = min(budget, int(limits.get("sync_chunk_bytes", 8 * 1024 * 1024)))
        # A chunk may contain an incomplete last line: only complete bytes commit.
        try:
            state = copy.deepcopy(cur.get("parser", {}))
            with path.open("rb") as fh:
                if not cur and offset:
                    header = fh.read(min(65536, max(1, budget // 4)))
                    budget -= len(header)
                    meta_lines = []
                    for raw in header.split(b"\n")[:-1]:
                        try:
                            if json.loads(raw).get("type") == "session_meta":
                                meta_lines.append(raw)
                        except (ValueError, AttributeError):
                            pass
                    if not meta_lines:
                        raise ValueError("Oversized Codex file has no session_meta in bounded header; no checkpoint advanced")
                    _codex_file(path, start, end, limits, state=state, lines=meta_lines)
                align = bool(not cur and offset)
                if align:
                    fh.seek(offset - 1)
                    align = fh.read(1) != b"\n"
                    budget -= 1
                fh.seek(offset)
                data = fh.read(min(count, budget))
                # If the first record is wider than a normal chunk, finish that
                # record within the remaining round budget instead of stalling.
                if b"\n" not in data and offset + len(data) < st.st_size and len(data) < budget:
                    data += fh.readline(budget - len(data))
            budget -= len(data)
            if align:
                # Tail starts mid-record; discard only that first incomplete record.
                skip = data.find(b"\n") + 1
                if not skip:
                    raise ValueError("No complete record boundary in bounded Codex tail")
                offset += skip
                data = data[skip:]
            complete = data.rfind(b"\n") + 1
            if not complete and data and offset + len(data) < st.st_size:
                # Retry with a fresh round budget. No partial record is consumed.
                raise ValueError("Codex record did not fit this round's remaining byte budget")
            epoch = dt.datetime(1970, 1, 1, tzinfo=start.tzinfo)
            records, pos = [], offset
            for raw in data[:complete].splitlines(keepends=True):
                records.append((raw, start if pos < initial_end else epoch))
                pos += len(raw)
            found = _codex_file(path, start, end, limits, state=state, lines=records)
            out.extend(found)
            inventory[name] = {"identity": identity, "signature": sig,
                "offset": offset + complete, "bootstrap_end": initial_end, "parser": state,
                "partial": complete < len(data) and offset + len(data) >= st.st_size}
        except (OSError, ValueError) as exc:
            limits.setdefault("_errors", {})[name] = str(exc)
    return out, inventory


def _source_signature(path):
    result = []
    for name in (path, path + "-wal"):
        try:
            st = os.stat(name)
            result.append([st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns])
        except FileNotFoundError:
            result.append(None)
    return result


def _opencode_incremental(start, end, limits, previous):
    path = _expand(limits.get("opencode_db"))
    if not path or not os.path.isfile(path):
        return [], previous
    signature = _source_signature(path)
    # The lower bound only moves forward. A quiet source cannot gain new evidence.
    if (previous.get("signature") == signature and previous.get("start", "") <= start.isoformat()
            and (previous.get("next_at") is None or previous["next_at"] >= end.timestamp() * 1000)):
        return [], previous
    con = _opencode_ro(path)
    try:
        con.execute("BEGIN")
        digests, changed, future = {}, set(), []
        lo, hi = start.timestamp() * 1000, end.timestamp() * 1000
        # Only the small session table is enumerated. Never scan message/part tables.
        rows = con.execute("SELECT s.id,s.parent_id,s.directory,s.title,s.time_updated FROM session s "
                           "WHERE EXISTS (SELECT 1 FROM message m WHERE m.session_id=s.id "
                           "AND m.time_created>=? AND m.time_created<?) "
                           "ORDER BY s.time_updated DESC LIMIT ?",
                           (lo, hi, int(limits.get("max_sessions", 400)))).fetchall()
        # Future rows are also located by per-session covering-index seeks, not a
        # global time scan. Needed to make the stat-only quiet fast path sound.
        next_row = con.execute("SELECT MIN((SELECT m.time_created FROM message m "
                               "WHERE m.session_id=s.id AND m.time_created>=? "
                               "ORDER BY m.time_created LIMIT 1)) FROM session s", (hi,)).fetchone()
        if next_row and next_row[0] is not None:
            future.append(next_row[0])
        for row in rows:
            sid = str(row["id"])
            h = hashlib.sha256(json.dumps(list(row)[:4]).encode())
            msgs = con.execute("SELECT id,time_created,time_updated FROM message WHERE session_id=? "
                               "AND time_created>=? AND time_created<? ORDER BY time_created,id LIMIT ?",
                               (sid, lo, hi, int(limits.get("max_messages", 5000)))).fetchall()
            if not msgs:
                continue
            for msg in msgs:
                h.update(json.dumps(list(msg)).encode())
                for part in con.execute("SELECT id,time_created,time_updated FROM part WHERE message_id=? "
                                        "ORDER BY time_created,id LIMIT ?", (msg["id"], int(limits.get("max_parts", 500)))):
                    h.update(json.dumps(list(part)).encode())
            digests[sid] = h.hexdigest()
            if previous.get("digests", {}).get(sid) != digests[sid]:
                changed.add(sid)
        out = collect_opencode(start, end, {**limits, "session_ids": changed,
                               "strict": True, "connection": con}) if changed else []
        checkpoint = {"signature": signature, "start": start.isoformat(), "digests": digests,
                      "next_at": min(future) if future else None}
        # A writer racing the snapshot must force another read next poll.
        if _source_signature(path) != signature:
            checkpoint.pop("signature")
        return out, checkpoint
    finally:
        con.close()



# ---------------------------------------------------------------- orchestration

def collect(start, end, limits=None) -> list:
    """Every enabled optional source, in the shape collect.py's digest expects."""
    limits = {**cfg(), **(limits or {})}
    out = []
    for fn in (collect_codex, collect_opencode):
        try:
            out.extend(fn(start, end, limits))
        except Exception:  # noqa: BLE001 — one broken source must not stop the rest
            continue
    return out


def status() -> dict:
    """Why each source was skipped, or what it would read."""
    c = cfg()
    codex = _expand(c.get("codex_dir"))
    db = _expand(c.get("opencode_db"))
    codex_ok = bool(codex) and Path(codex).is_dir()
    db_ok = bool(db) and os.path.isfile(db)
    return {
        "codex": {"configured": bool(codex), "path": codex or None, "present": codex_ok,
                  "skip_reason": None if codex_ok else ("not configured" if not codex
                                                       else "directory not found")},
        "opencode": {"configured": bool(db), "path": db or None, "present": db_ok,
                     "read_only": True,
                     "skip_reason": None if db_ok else ("not configured" if not db
                                                        else "database not found")},
        "limits": {k: c[k] for k in ("max_files", "max_bytes", "max_messages", "max_sessions")},
    }


if __name__ == "__main__":
    print(json.dumps(status(), ensure_ascii=False, indent=2))
