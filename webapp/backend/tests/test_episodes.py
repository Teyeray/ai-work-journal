"""Work episodes: narrative grouping of human time with agent work attached."""
import json

from conftest import aw_event, load_aw

from app import db, timeline, timeutil
from app.adapters import sessions as sessions_mod

D = "2026-10-07"


def _ev(etype, hhmm, payload, source="sessions:pi", key=[0]):
    key[0] += 1
    ts = timeutil.parse_ts(f"{D}T{hhmm}:00+08:00")
    db.ex("INSERT INTO source_events(source_key,event_key,event_type,occurred_at,"
          "observed_at,day_key,payload,created_at) VALUES(?,?,?,?,?,?,?,?)",
          (source, f"ep{key[0]}", etype, timeutil.iso(ts), timeutil.iso(ts), D,
           json.dumps(payload), timeutil.iso(ts)))


def _pi_session(cwd, prompts, file="sess-a"):
    _ev("session_meta", prompts[0][0], {"cwd": cwd, "file": file})
    for hhmm, text in prompts:
        _ev("msg_user", hhmm, {"text": text, "file": file})
    sessions_mod._refresh_sessions("pi", [])


def eps():
    return timeline.day_timeline(D)["episodes"]


def test_brief_switch_stays_inside_episode(env):
    load_aw([
        aw_event(D, "09:00", 40, app="zotero", title="rsi survey paper"),
        aw_event(D, "09:40", 4, app="google-chrome", title="news site"),
        aw_event(D, "09:44", 36, app="zotero", title="rsi survey paper"),
    ])
    e = eps()
    assert len(e) == 1, e
    assert e[0]["label"] == "RSI" and e[0]["activity"] == "阅读"
    # the 4-min news tab has no evidence of its own: it joins the RSI lane by context
    assert {m["label"] for m in e[0]["mix"]} == {"RSI"}
    assert e[0]["human_sec"] == 80 * 60


def test_long_gap_splits_episodes(env):
    load_aw([
        aw_event(D, "09:00", 30, app="zotero", title="rsi survey paper"),
        aw_event(D, "10:00", 30, app="zotero", title="rsi survey paper"),
    ])
    assert len(eps()) == 2


def test_terminal_time_follows_concurrent_prompt_project(env):
    # terminal title is a tmux name; the user is prompting an RSI agent meanwhile
    load_aw([aw_event(D, "09:00", 30, app="com.mitchellh.ghostty", title="t main")])
    _pi_session("/home/yangrm/Projects/RSI", [("09:05", "run the extraction again"),
                                              ("09:20", "now verify the numbers")])
    t = timeline.day_timeline(D)
    assert t["blocks"][0]["project"] == "RSI"
    assert t["blocks"][0]["attributed_by"] == "context"
    e = t["episodes"]
    assert len(e) == 1 and e[0]["label"] == "RSI"
    assert e[0]["n_prompts"] == 2
    assert e[0]["summary"] == "run the extraction again"
    # prompt evidence carries its session project
    assert {p.get("project") for p in t["evidence"] if p["kind"] == "agent_user"} == {"RSI"}


def test_terminal_without_prompts_keeps_title_label(env):
    load_aw([aw_event(D, "09:00", 30, app="com.mitchellh.ghostty", title="t main")])
    assert timeline.day_timeline(D)["blocks"][0]["attributed_by"] is None


def test_commits_attach_only_to_matching_project(env):
    load_aw([aw_event(D, "09:00", 60, app="zotero", title="rsi survey paper")])
    _ev("git_commit", "09:30", {"repo": "RSI", "subject": "feat: extraction"}, source="git")
    _ev("git_commit", "09:31", {"repo": "paperfacts", "subject": "fix: other"}, source="git")
    e = eps()
    assert [c["subject"] for c in e[0]["commits"]] == ["feat: extraction"]
    assert e[0]["summary"] == "feat: extraction"


def test_human_time_not_inflated_by_agents(env):
    load_aw([aw_event(D, "09:00", 30, app="zotero", title="rsi survey paper")])
    _pi_session("/home/yangrm/Projects/RSI", [("08:00", "start long job"), ("12:00", "status?")])
    e = eps()
    assert e[0]["human_sec"] == 30 * 60


def test_folded_repo_commit_maps_to_project(env):
    """'paperfacts-dev' folds to paperfacts, so its commits match paperfacts episodes."""
    load_aw([aw_event(D, "09:00", 60, app="zotero", title="rsi survey paper")])
    _ev("git_commit", "09:30", {"repo": "paperfacts-dev", "path": "/home/x/Projects/paperfacts-dev",
                                "subject": "fix: figure"}, source="git")
    c = [p for p in timeline.day_timeline(D)["evidence"] if p["kind"] == "commit"][0]
    assert c["project"] == "paperfacts"
    assert eps()[0]["commits"] == []  # RSI episode: not attached


def test_override_clears_prompt_attribution(env):
    load_aw([aw_event(D, "09:00", 30, app="com.mitchellh.ghostty", title="t main")])
    _pi_session("/home/yangrm/Projects/RSI", [("09:05", "run the extraction again")])
    db.ex("INSERT INTO manual_overrides(day_key,target_type,anchor_start,field,value,"
          "original_value,note,created_at) VALUES(?,?,?,?,?,?,?,?)",
          (D, "activity_segment", f"{D}T09:10:00+08:00", "project", "CASMI", "RSI", "", D))
    b = timeline.day_timeline(D)["blocks"][0]
    assert b["project"] == "CASMI" and b["attributed_by"] is None


def test_block_ending_at_midnight_stays_on_day(env):
    load_aw([aw_event(D, "23:20", 40, app="zotero", title="rsi survey paper")])
    t = timeline.day_timeline(D)
    assert t["episodes"][-1]["end"].startswith("2026-10-08T00:00")  # frontend maps to 1440


def test_browsing_with_project_evidence_is_named_by_project(env):
    """Watching the app you build in Chrome while prompting its agent and
    committing = project work, not 网页浏览."""
    load_aw([aw_event(D, "09:00", 40, app="google-chrome", title="localhost dashboard")])
    _pi_session("/home/yangrm/Projects/RSI", [("09:05", "tweak the layout"), ("09:15", "fix spacing")])
    _ev("git_commit", "09:20", {"repo": "RSI", "path": "/home/x/Projects/RSI", "subject": "feat: layout"}, source="git")
    e = eps()[0]
    # prompts sent during the stretch file the window itself under RSI
    assert e["label"] == "RSI" and not e["is_category"]
    assert e["activity"] == "浏览" and e["summary"] == "feat: layout"


def test_browsing_without_evidence_stays_category(env):
    load_aw([aw_event(D, "09:00", 40, app="google-chrome", title="news site front page")])
    e = eps()[0]
    assert e["label"] == "网页浏览" and e["context"] is None


def test_noise_titles_not_used_as_summary(env):
    load_aw([aw_event(D, "09:00", 30, app="google-chrome", title="New Tab - Google Chrome"),
             aw_event(D, "09:30", 5, app="google-chrome", title="Deterministic gates paper - Google Chrome")])
    assert eps()[0]["summary"].startswith("Deterministic gates paper")


def test_agent_exec_union_and_peak(env):
    from app import timeline as tlm
    load_aw([aw_event(D, "09:00", 10, app="zotero", title="rsi survey paper")])
    _pi_session("/home/yangrm/Projects/RSI", [("09:00", "job one"), ("09:05", "go on")], file="s1")
    _pi_session("/home/yangrm/Projects/RSI", [("09:02", "job two"), ("09:06", "go on")], file="s2")
    mm = tlm.day_timeline(D)["metrics"]
    assert mm["agent_peak_parallel"] == 2
    assert mm["agent_exec_union_sec"] == 6 * 60 and mm["agent_exec_sec"] == 9 * 60


def test_collaborator_commits_are_not_your_output(env, monkeypatch):
    from app.adapters import git as git_mod
    monkeypatch.setattr(git_mod, "my_authors", lambda paths: {"yangrm", "Teyeray"})
    load_aw([aw_event(D, "09:00", 60, app="zotero", title="rsi survey paper")])
    _ev("git_commit", "09:20", {"repo": "RSI", "subject": "mine", "author": "Teyeray"}, source="git")
    _ev("git_commit", "09:30", {"repo": "RSI", "subject": "theirs", "author": "XiJW"}, source="git")
    labels = [p["label"] for p in timeline.day_timeline(D)["evidence"] if p["kind"] == "commit"]
    assert labels == ["mine"]
