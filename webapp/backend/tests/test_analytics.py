"""Analytics definitions: stretches, switches, focus bins, summaries, insights."""
import datetime as dt

from app import analytics
from app.timeutil import parse_ts

D0 = parse_ts("2026-10-07T00:00:00+08:00")


def w(a, b, project):
    return {"start": f"2026-10-07T{a}+08:00", "end": f"2026-10-07T{b}+08:00", "project": project}


def test_stray_glance_does_not_break_or_switch():
    runs = analytics.stretches([w("09:00:00", "09:20:00", "RSI"), w("09:20:00", "09:20:30", "网页浏览"),
                                w("09:20:30", "09:40:00", "RSI")])
    assert [r[0] for r in runs] == ["RSI"]
    assert (runs[0][2] - runs[0][1]) == dt.timedelta(minutes=40)


def test_switches_and_away_break():
    runs = analytics.stretches([w("09:00:00", "09:10:00", "RSI"), w("09:10:00", "09:20:00", "CASMI"),
                                w("09:40:00", "09:50:00", "CASMI")])
    assert [r[0] for r in runs] == ["RSI", "CASMI", "CASMI"]   # 20-min away splits the CASMI run


def _tl(windows, blocks=None):
    return {"day_key": "2026-10-07", "has_observation": True,
            "metrics": {"human_active_sec": 3600, "unknown_sec": 0, "agent_exec_union_sec": 0},
            "blocks": blocks or [{"start": x["start"], "end": x["end"], "project": x["project"]} for x in windows],
            "agent": [], "evidence": [], "windows": windows}


def test_day_stats_focus_bins_and_hourly():
    st = analytics.day_stats(_tl([w("09:00:00", "09:45:00", "RSI"), w("09:45:00", "09:50:00", "CASMI")]), D0)
    assert st["switches"] == 1 and st["hourly_switches"][9] == 1
    assert st["focus_min"][3] == 45 and st["focus_min"][1] == 5     # 30–60 bin, 5–15 bin
    assert st["hourly_human"][9] == 50
    assert st["longest"]["project"] == "RSI"


def test_summary_uses_only_observed_days_for_human_measures():
    a = analytics.day_stats(_tl([w("09:00:00", "10:00:00", "RSI")]), D0)
    b = {**a, "has_observation": False, "human_sec": 0, "allocation": {}, "focus_min": [0] * 5, "switches": 0}
    s = analytics.summarize([a, b])
    assert s["observed_days"] == 1 and s["human_per_day"] == 3600
    ins = analytics.insights([a, b], s, None, analytics.project_table([a, b], {}))
    assert ins[0]["kind"] == "quality" and "1 天" in ins[0]["title"]
    assert any(i["kind"] == "priority" and "还没有设置" in i["title"] for i in ins)


def test_top_project_ranks_by_your_time_not_agent_time():
    table = [{"project": "paperfacts", "human_sec": 3 * 3600, "agent_sec": 20 * 3600, "priority": None},
             {"project": "RSI", "human_sec": 5 * 3600, "agent_sec": 10 * 3600, "priority": None}]
    s = {"days": 1, "observed_days": 1, "focus_share": None}
    ins = analytics.insights([{"hourly_switches": [0] * 24}], s, None, table)
    assert next(i for i in ins if i["kind"] == "projects")["title"].startswith("投入最多:RSI")
