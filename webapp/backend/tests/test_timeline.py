"""Timeline math: human vs away vs unknown, midnight clipping, no double counting."""
import json

from conftest import aw_event, load_aw

from app import timeline

D = "2026-10-07"


def m(day=D):
    return timeline.day_timeline(day)["metrics"]


def test_human_afk_and_unknown_sum_to_observed(env):
    # window 09:00-12:00 (180m); afk 10:00-11:00 (60m inside);
    # gap 12:00-12:10 with no window and no afk -> unknown (10m)
    load_aw([
        aw_event(D, "09:00", 180, app="ghostty", title="bash"),
        aw_event(D, "10:00", 60, kind="aw_afk", status="afk"),
        aw_event(D, "12:10", 30, app="ghostty", title="bash"),
    ])
    mm = m()
    assert mm["observed_span_sec"] == 220 * 60, mm  # 09:00..12:40
    assert mm["human_active_sec"] == 150 * 60  # 09-10 + 11-12 + 12:10-12:40
    assert mm["afk_sec"] == 60 * 60
    assert mm["unknown_sec"] == 10 * 60
    assert abs((mm["human_active_sec"] + mm["afk_sec"] + mm["unknown_sec"])
               - mm["observed_span_sec"]) < 1


def test_agent_runtime_counted_once_not_added_to_human(env):
    load_aw([aw_event(D, "09:00", 60, app="ghostty", title="pi")])
    from app import db
    from app.adapters import sessions as sessions_mod
    # a 5h agent session overlapping the observed window (two events: 08:00, 13:00)
    for ek, ts in (("s1a", f"{D}T08:00:00+08:00"), ("s1b", f"{D}T13:00:00+08:00")):
        db.ex("INSERT INTO source_events(source_key,event_key,event_type,occurred_at,"
              "observed_at,day_key,payload,created_at) VALUES(?,?,?,?,?,?,?,?)",
              ("sessions:pi", ek, "session_meta" if ek == "s1a" else "msg_user", ts, ts,
               D, json.dumps({"cwd": "/home/yangrm/Projects/RSI", "file": "sess-1",
                              **({"text": "hi"} if ek == "s1b" else {})}), ts))
    sessions_mod._refresh_sessions("pi", [])
    mm = m()
    assert mm["human_active_sec"] == 60 * 60  # unchanged by agent presence
    assert mm["agent_runtime_union_sec"] == 300 * 60  # 5h span, counted once


def test_midnight_event_clipped_to_day(env):
    # window 23:50 → next day 00:20 (30 min): 10m on D, 20m on D+1
    load_aw([
        {"event_type": "aw_window", "occurred_at": f"{D}T23:50:00+08:00",
         "payload": json.dumps({"duration": 1800, "data": {"app": "chrome", "title": "x"}})},
    ])
    assert m(D)["observed_span_sec"] == 600
    assert m("2026-10-08")["observed_span_sec"] == 1200


def test_missing_hour_is_unknown_not_work(env):
    # observed 09:00-17:00 but watcher gap 12:00-13:00 (no window, no afk)
    load_aw([
        aw_event(D, "09:00", 180, app="ghostty", title="a"),
        aw_event(D, "13:00", 240, app="ghostty", title="b"),
    ])
    mm = m()
    assert mm["unknown_sec"] == 60 * 60
    assert mm["human_active_sec"] == 420 * 60  # no invented work in the gap


def test_no_observation_day(env):
    t = timeline.day_timeline(D)
    assert t["has_observation"] is False
    assert t["metrics"]["human_active_sec"] == 0
    assert t["metrics"]["unknown_sec"] == 0
    assert t["blocks"] == []


def test_dedupe_overlapping_afk(env):
    # awatcher restart artifacts: duplicate/overlapping afk events
    load_aw([
        aw_event(D, "09:00", 180),  # window 09:00-12:00
        aw_event(D, "10:00", 60, kind="aw_afk", status="afk"),
        aw_event(D, "10:00", 120, kind="aw_afk", status="afk"),
    ])
    assert m()["afk_sec"] == 120 * 60  # union, not sum


def test_override_survives_reprojection(env):
    load_aw([aw_event(D, "09:00", 60, app="chrome", title="RSI paper reading")])
    from app import db, timeutil
    t = timeline.day_timeline(D)
    assert t["blocks"], "expected a block"
    blk = t["blocks"][0]
    inferred = blk["project"]
    db.ex("INSERT INTO manual_overrides(target_type,day_key,anchor_start,field,"
          "value,note,created_at) VALUES(?,?,?,?,?,?,?)",
          ("activity_segment", D, blk["start"], "project", "InoLab",
           "test correction", timeutil.now_utc().isoformat()))
    t2 = timeline.day_timeline(D)
    b2 = t2["blocks"][0]
    assert b2["project"] == "InoLab"
    assert b2["is_manual_override"] is True
    assert b2["confidence"] == 1.0
    # revoke: tombstone row + point old row's superseded_by at it
    ov = db.q1("SELECT id FROM manual_overrides WHERE day_key=? AND field='project'", (D,))
    db.ex("INSERT INTO manual_overrides(target_type,day_key,anchor_start,field,"
          "value,note,created_at) VALUES(?,?,?,?,?,?,?)",
          ("activity_segment", D, blk["start"], "__revoke__", "", "revoke",
           timeutil.now_utc().isoformat()))
    tomb = db.q1("SELECT id FROM manual_overrides WHERE field='__revoke__'")
    db.ex("UPDATE manual_overrides SET superseded_by=? WHERE id=?", (tomb["id"], ov["id"]))
    t3 = timeline.day_timeline(D)
    assert t3["blocks"][0]["project"] == inferred
