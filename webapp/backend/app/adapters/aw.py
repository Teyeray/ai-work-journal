"""ActivityWatch adapter: window + afk buckets, incremental by time window.

Idempotency: AW events carry stable integer ids; event_key = 'aw-<bucket>-<id>'.
AW extends an open event in place (heartbeats grow its duration, same id), so a
re-seen id updates the stored payload instead of keeping the first snapshot.
Cursor: last event end; each scan re-queries with overlap and relies on id dedup.
"""
import json
import urllib.parse
import urllib.request
import datetime as dt

from .. import config, db
from ..timeutil import now_utc, iso, local_day_key, parse_ts

OVERLAP = dt.timedelta(hours=2)
MAX_BACK = dt.timedelta(days=40)
# v2: events are upserted. Older cursors stored truncated durations, so the first
# v2 scan re-reads the whole MAX_BACK window to repair them.
CURSOR_V = 2


def _get(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def _buckets():
    info = _get(f"{config.AW_URL}/api/0/info")
    host = info.get("hostname", config.AW_HOST)
    return host, f"aw-watcher-window_{host}", f"aw-watcher-afk_{host}"


def _days_of(ts, dur):
    days = {local_day_key(ts)}
    if dur > 0:
        days.add(local_day_key(ts + dt.timedelta(seconds=dur)))
    return days


def scan() -> dict:
    try:
        host, win_bucket, afk_bucket = _buckets()
    except Exception as e:
        db.log_sync("activitywatch", 0, f"unreachable: {e}")
        return {"source": "activitywatch", "new": 0, "error": str(e)}

    now = now_utc()
    cur = db.q1("SELECT cursor_json FROM source_cursors WHERE source_key='activitywatch'")
    last_end = None
    if cur:
        c = json.loads(cur["cursor_json"])
        if c.get("v") == CURSOR_V:
            last_end = parse_ts(c.get("last_end"))
    start = (last_end or (now - MAX_BACK)) - OVERLAP
    if start < now - MAX_BACK:
        start = now - MAX_BACK

    total_new = total_updated = 0
    affected_days = set()
    max_end = start
    with db.Tx():
        for bucket, etype in ((win_bucket, "aw_window"), (afk_bucket, "aw_afk")):
            try:
                evs = _get(f"{config.AW_URL}/api/0/buckets/{bucket}/events?"
                           + urllib.parse.urlencode({"start": iso(start), "end": iso(now)}))
            except Exception as e:
                db.log_sync(f"activitywatch/{bucket}", 0, f"query failed: {e}")
                continue
            stored = _stored([f"aw-{bucket}-{e.get('id')}" for e in evs])
            for e in evs:
                ts = parse_ts(e.get("timestamp"))
                dur = float(e.get("duration") or 0)
                if ts is None:
                    continue
                key = f"aw-{bucket}-{e.get('id')}"
                end = ts + dt.timedelta(seconds=dur)
                # AW clips events to the query range: merge with what we stored
                # before (earliest start, latest end) so neither side is lost
                old = stored.get(key)
                if old:
                    o_ts = parse_ts(old["occurred_at"])
                    o_dur = float(json.loads(old["payload"]).get("duration") or 0)
                    ts, end = min(ts, o_ts), max(end, o_ts + dt.timedelta(seconds=o_dur))
                    dur = (end - ts).total_seconds()
                payload = json.dumps({"bucket": bucket, "data": e.get("data"),
                                      "duration": dur, "aw_id": e.get("id")},
                                     ensure_ascii=False)
                days = _days_of(ts, dur)
                r = db.ex("INSERT INTO source_events(source_key,event_key,event_type,"
                          "occurred_at,observed_at,day_key,payload,created_at) "
                          "VALUES(?,?,?,?,?,?,?,?) "
                          "ON CONFLICT(source_key,event_key) DO UPDATE SET "
                          "occurred_at=excluded.occurred_at, day_key=excluded.day_key, "
                          "payload=excluded.payload, observed_at=excluded.observed_at "
                          "WHERE source_events.payload <> excluded.payload "
                          "OR source_events.occurred_at <> excluded.occurred_at",
                          ("activitywatch", key, etype, iso(ts), iso(now), sorted(days)[0],
                           payload, iso(now)))
                if r.rowcount:
                    if old:
                        total_updated += 1
                        affected_days.update(_days_of(o_ts, o_dur))
                    else:
                        total_new += 1
                    affected_days.update(days)
                if end > max_end:
                    max_end = end
        db.ex("INSERT INTO source_cursors(source_key,cursor_json,revision,last_scan_at) "
              "VALUES(?,?,1,?) ON CONFLICT(source_key) DO UPDATE SET "
              "cursor_json=excluded.cursor_json, revision=revision+1, "
              "last_scan_at=excluded.last_scan_at",
              ("activitywatch", json.dumps({"last_end": iso(max_end), "host": host, "v": CURSOR_V}), iso(now)))
        db.log_sync("activitywatch", total_new)
    return {"source": "activitywatch", "new": total_new, "updated": total_updated, "days": sorted(affected_days)}


def _stored(keys: list) -> dict:
    """event_key → stored row, for the events a scan is about to upsert."""
    out = {}
    for i in range(0, len(keys), 500):
        part = keys[i:i + 500]
        for r in db.q("SELECT event_key, occurred_at, payload FROM source_events WHERE source_key='activitywatch' "
                      f"AND event_key IN ({','.join('?' * len(part))})", part):
            out[r["event_key"]] = r
    return out
