"""ActivityWatch adapter: window + afk buckets, incremental by time window.

Idempotency: AW events carry stable integer ids; event_key = 'aw-<bucket>-<id>'.
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
        last_end = parse_ts(json.loads(cur["cursor_json"]).get("last_end"))
    start = (last_end or (now - MAX_BACK)) - OVERLAP
    if start < now - MAX_BACK:
        start = now - MAX_BACK

    total_new = 0
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
            for e in evs:
                ts = parse_ts(e.get("timestamp"))
                dur = float(e.get("duration") or 0)
                if ts is None:
                    continue
                key = f"aw-{bucket}-{e.get('id')}"
                payload = json.dumps({"bucket": bucket, "data": e.get("data"),
                                      "duration": dur, "aw_id": e.get("id")},
                                     ensure_ascii=False)
                days = _days_of(ts, dur)
                r = db.ex("INSERT INTO source_events(source_key,event_key,event_type,"
                          "occurred_at,observed_at,day_key,payload,created_at) "
                          "VALUES(?,?,?,?,?,?,?,?) "
                          "ON CONFLICT(source_key,event_key) DO NOTHING",
                          ("activitywatch", key, etype, iso(ts), iso(now), sorted(days)[0],
                           payload, iso(now)))
                if r.rowcount:
                    total_new += 1
                    affected_days.update(days)
                end = ts + dt.timedelta(seconds=dur)
                if end > max_end:
                    max_end = end
        db.ex("INSERT INTO source_cursors(source_key,cursor_json,revision,last_scan_at) "
              "VALUES(?,?,1,?) ON CONFLICT(source_key) DO UPDATE SET "
              "cursor_json=excluded.cursor_json, revision=revision+1, "
              "last_scan_at=excluded.last_scan_at",
              ("activitywatch", json.dumps({"last_end": iso(max_end), "host": host}), iso(now)))
        db.log_sync("activitywatch", total_new)
    return {"source": "activitywatch", "new": total_new, "days": sorted(affected_days)}
