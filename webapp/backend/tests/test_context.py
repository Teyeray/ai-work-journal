"""Context attribution: windows with no evidence of their own join the lane of
what surrounds them; direct evidence is never overridden."""
import json

from conftest import aw_event, load_aw

from app import db, timeline, timeutil
from app.adapters import sessions as sessions_mod

D = "2026-10-07"


def _ev(etype, hhmm, payload, key=[0]):
    key[0] += 1
    ts = timeutil.parse_ts(f"{D}T{hhmm}:00+08:00")
    db.ex("INSERT INTO source_events(source_key,event_key,event_type,occurred_at,observed_at,"
          "day_key,payload,created_at) VALUES(?,?,?,?,?,?,?,?)",
          ("sessions:pi", f"cx{key[0]}", etype, timeutil.iso(ts), timeutil.iso(ts), D,
           json.dumps(payload), timeutil.iso(ts)))


def prompts_to(cwd, times, file):
    _ev("session_meta", times[0], {"cwd": cwd, "file": file})
    for t in times:
        _ev("msg_user", t, {"text": "keep going on this", "file": file})
    sessions_mod._refresh_sessions("pi", [])


def visits():
    return [(w["title"], w["project"], w["reason"]) for w in timeline.day_timeline(D)["windows"]]


def test_title_evidence_beats_nearby_prompts(env):
    """The bug: 'π - paperfacts' was filed under RSI because an RSI prompt was nearby."""
    load_aw([aw_event(D, "09:00", 10, app="com.mitchellh.ghostty", title="π - paperfacts")])
    prompts_to("/home/yangrm/Projects/RSI", ["09:02", "09:05"], "r1")
    (title, project, why), = visits()
    assert project == "paperfacts" and why.startswith("关键词")


def test_short_evidence_free_window_joins_surrounding_lane(env):
    load_aw([
        aw_event(D, "09:00", 10, app="com.mitchellh.ghostty", title="π - paperfacts"),
        aw_event(D, "09:10", 1, app="google-chrome", title="New Tab - Google Chrome"),
        aw_event(D, "09:11", 10, app="com.mitchellh.ghostty", title="π - paperfacts"),
    ])
    mid = visits()[1]
    assert mid[1] == "paperfacts" and mid[2].startswith("上下文")


def test_bare_tmux_tab_follows_the_prompt_sent_during_it(env):
    load_aw([aw_event(D, "09:00", 8, app="com.mitchellh.ghostty", title="t")])
    prompts_to("/home/yangrm/Projects/RSI", ["09:03"], "r2")
    assert visits()[0][1] == "RSI"


def test_long_silent_stretch_is_not_swallowed(env):
    load_aw([
        aw_event(D, "09:00", 10, app="com.mitchellh.ghostty", title="π - paperfacts"),
        aw_event(D, "09:10", 40, app="google-chrome", title="Some video - Google Chrome"),
    ])
    assert visits()[1][1] == "网页浏览"


def test_ambiguous_context_stays_in_category(env):
    load_aw([
        aw_event(D, "09:00", 5, app="com.mitchellh.ghostty", title="π - paperfacts"),
        aw_event(D, "09:05", 2, app="google-chrome", title="New Tab - Google Chrome"),
        aw_event(D, "09:07", 5, app="com.mitchellh.ghostty", title="π - RSI"),
    ])
    assert visits()[1][1] == "网页浏览"


def test_deliberate_category_is_kept(env):
    load_aw([
        aw_event(D, "09:00", 10, app="com.mitchellh.ghostty", title="π - paperfacts"),
        aw_event(D, "09:10", 2, app="google-chrome", title="四象限 - 滴答清单 - Google Chrome"),
        aw_event(D, "09:12", 10, app="com.mitchellh.ghostty", title="π - paperfacts"),
    ])
    assert visits()[1][1] == "个人事务"


def test_each_window_judged_by_its_own_context(env):
    """A long run of bare tabs is not relabelled as one block: each window
    follows the prompts around it."""
    load_aw([
        aw_event(D, "09:00", 5, app="com.mitchellh.ghostty", title="t"),
        aw_event(D, "09:30", 5, app="com.mitchellh.ghostty", title="t"),
    ])
    prompts_to("/home/yangrm/Projects/RSI", ["09:02"], "r3")
    prompts_to("/home/yangrm/Projects/CASMI", ["09:32"], "c3")
    assert [v[1] for v in visits()] == ["RSI", "CASMI"]
