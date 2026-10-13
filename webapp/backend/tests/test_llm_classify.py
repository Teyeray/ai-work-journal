"""LLM layer: only evidence-free titles are asked, answers are cached per title
and project set, rules still win, low-confidence/unknown answers are ignored."""
from conftest import aw_event, load_aw

from app import db, llm_classify, projects, timeline

D = "2026-10-07"


def fake(answers):
    """A stand-in model: title substring → (project, confidence)."""
    calls = []

    def call(model, cards, titles):
        calls.append(titles)
        out = []
        for x in titles:
            t, base = x["title"], {"id": x["id"], "head": x["title"][:8]}
            for sub, (proj, conf) in answers.items():
                if sub in t:
                    out.append({**base, "project": proj, "confidence": conf, "why": "test"})
                    break
            else:
                out.append({**base, "project": None, "confidence": 0.9, "why": "generic"})
        return out
    return call, calls


def run(answers):
    call, calls = fake(answers)
    r = llm_classify.run(days=100000, call=call)
    projects.invalidate()
    return r, calls


def test_semantic_label_for_evidence_free_title(env):
    load_aw([aw_event(D, "09:00", 10, app="google-chrome", title="CATL260913 超声扫描数据 EDA 报告 - Google Chrome")])
    r, calls = run({"超声": ("RSI", 0.95)})
    assert r["titles"] == 1 and r["labelled"] == 1
    label, why = projects.explain("google-chrome", "CATL260913 超声扫描数据 EDA 报告 - Google Chrome")
    assert label == "RSI" and why.startswith("语义判断")
    # and it shows up in the day's lanes
    assert timeline.day_timeline(D)["windows"][0]["project"] == "RSI"


def test_titles_with_rule_evidence_are_never_sent(env):
    load_aw([aw_event(D, "09:00", 10, app="google-chrome", title="MemoHarness report - Google Chrome"),
             aw_event(D, "09:10", 5, app="google-chrome", title="New Tab - Google Chrome")])
    r, calls = run({})
    sent = [(x["app"], x["title"]) for batch in calls for x in batch]
    assert sent == [("google-chrome", "New Tab")]


def test_cached_per_title_and_project_set(env):
    load_aw([aw_event(D, "09:00", 10, app="google-chrome", title="理解单一黑箱提示词 - Google Chrome")])
    run({"黑箱": ("RSI", 0.9)})
    r2, calls2 = run({"黑箱": ("RSI", 0.9)})
    assert r2["titles"] == 0 and calls2 == []          # asked once
    projects.ensure_project("newproj")                  # project set changes → re-ask
    r3, _ = run({"黑箱": ("RSI", 0.9)})
    assert r3["titles"] == 1


def test_low_confidence_and_unknown_projects_ignored(env):
    load_aw([aw_event(D, "09:00", 10, app="google-chrome", title="vague page - Google Chrome"),
             aw_event(D, "09:10", 10, app="google-chrome", title="other page - Google Chrome")])
    run({"vague": ("RSI", 0.4), "other": ("NotAProject", 0.99)})
    assert projects.classify("google-chrome", "vague page - Google Chrome") == "网页浏览"
    assert projects.classify("google-chrome", "other page - Google Chrome") == "网页浏览"
    assert db.q1("SELECT project FROM llm_title_labels WHERE title='other page'")["project"] is None


def test_model_failure_is_contained(env):
    load_aw([aw_event(D, "09:00", 10, app="google-chrome", title="some page - Google Chrome")])

    def boom(model, cards, titles):
        raise RuntimeError("endpoint down")
    r = llm_classify.run(days=100000, call=boom)
    assert r["labelled"] == 0 and llm_classify.status()["error"].startswith("endpoint down")
    assert projects.classify("google-chrome", "some page - Google Chrome") == "网页浏览"


def test_misaligned_answers_are_dropped(env):
    """An answer whose quoted head is not its title (the model shifted rows) is ignored."""
    load_aw([aw_event(D, "09:00", 10, app="google-chrome", title="ubuntu 输入法 - Google Chrome"),
             aw_event(D, "09:10", 10, app="google-chrome", title="超声析锂项目 评审 - Google Chrome")])

    def shifted(model, cards, titles):
        a, b = titles
        return [{"id": a["id"], "head": b["title"][:8], "project": "RSI", "confidence": 0.95, "why": "x"},
                {"id": b["id"], "head": b["title"][:8], "project": "RSI", "confidence": 0.95, "why": "x"}]
    llm_classify.run(days=100000, call=shifted)
    projects.invalidate()
    assert projects.classify("google-chrome", "ubuntu 输入法 - Google Chrome") == "网页浏览"
    assert projects.classify("google-chrome", "超声析锂项目 评审 - Google Chrome") == "RSI"
