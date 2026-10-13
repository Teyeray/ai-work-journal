"""Project classification: data-driven rules, signal priority, explanations."""
import json

import pytest

from app import config, db, projects


@pytest.fixture()
def roots(env, tmp_path, monkeypatch):
    """A project root with a few real directories."""
    root = tmp_path / "Projects"
    for d in ("ai-work-journal", "harnesslens", "paperfacts", "paperfacts-dev", "agent_sources", "tmp"):
        (root / d).mkdir(parents=True)
    monkeypatch.setattr(config, "PROJECT_ROOTS", [str(root)])
    projects.invalidate()
    projects._STATE["key"] = None
    yield root
    projects.invalidate()
    projects._STATE["key"] = None


def ex(app, title):
    return projects.explain(app, title)


def test_path_in_title_beats_keywords(roots):
    label, why = ex("org.gnome.TextEditor", f"notes on rsi ({roots}/ai-work-journal/README.md)")
    assert label == "ai-work-journal" and why.startswith("路径")


def test_longest_keyword_wins(roots):
    # RSI has the seed alias "harness"; the directory harnesslens is more specific
    assert ex("google-chrome", "HarnessLens results - Google Chrome")[0] == "harnesslens"
    assert ex("google-chrome", "harness benchmark survey - Google Chrome")[0] == "RSI"


def test_tmux_names_split_on_separators(roots):
    label, why = ex("com.mitchellh.ghostty", "t rsi_cc")
    assert label == "RSI" and "rsi" in why


def test_cjk_alias_is_substring(roots):
    assert ex("google-chrome", "设计每日工作看板 - Google Chrome")[0] == "ai-work-journal"


def test_unknown_app_is_never_a_project(roots):
    label, why = ex("apport-gtk", "Ubuntu")
    assert label == projects.OTHER_APP and projects.is_category(label)
    projects.ensure_project(label)
    assert not db.q1("SELECT 1 FROM projects WHERE name=?", (label,))


def test_app_category_fallback(roots):
    assert ex("google-chrome", "News front page - Google Chrome") == ("网页浏览", "按应用分类")


def test_parent_folds_and_skip_and_ignore(roots):
    assert projects.classify_for_session(f"{roots}/paperfacts-dev/src") == "paperfacts"
    assert projects.classify_for_session(f"{roots}/tmp/x") == "Unassigned"          # skip_dirs
    assert ex("google-chrome", "agent_sources index - Google Chrome")[0] == "网页浏览"  # ignored


def test_session_longest_path_prefix(roots):
    sub = roots / "ai-work-journal" / "webapp"
    sub.mkdir()
    db.ex("INSERT INTO projects(name, rules_json, created_at, updated_at) VALUES(?,?,?,?)",
          ("journal-webapp", json.dumps({"paths": [str(sub)]}), "2026-10-01", "2026-10-01"))
    projects.invalidate()
    assert projects.classify_for_session(str(sub / "backend")) == "journal-webapp"
    assert projects.classify_for_session(str(roots / "ai-work-journal" / "data")) == "ai-work-journal"


def test_user_pattern_via_api_reclassifies(roots):
    from fastapi.testclient import TestClient
    from app.api import app
    c = TestClient(app)
    assert ex("google-chrome", "设计每日总结看板 - Google Chrome")[0] == "网页浏览"
    pid = next(p["id"] for p in c.get("/api/v1/projects").json()["projects"] if p["name"] == "ai-work-journal")
    prev = c.post("/api/v1/classify/preview",
                  json={"project": "ai-work-journal", "kind": "pattern", "value": "总结看板"}).json()
    assert prev["windows"] == 0  # no window events in the sandbox; preview runs without saving
    rules = next(p["rules"] for p in c.get("/api/v1/projects").json()["projects"] if p["id"] == pid)
    rules["patterns"].append("总结看板")
    assert c.patch(f"/api/v1/projects/{pid}", json={"rules": rules}).status_code == 200
    label, why = ex("google-chrome", "设计每日总结看板 - Google Chrome")
    assert label == "ai-work-journal" and why.startswith("规则")
    assert c.patch(f"/api/v1/projects/{pid}", json={"rules": {"patterns": ["("]}}).status_code == 400


def test_seed_applies_once(roots):
    from fastapi.testclient import TestClient
    from app.api import app
    c = TestClient(app)
    rsi = next(p for p in c.get("/api/v1/projects").json()["projects"] if p["name"] == "RSI")
    rules = {**rsi["rules"], "aliases": [a for a in rsi["rules"]["aliases"] if a != "harness"]}
    c.patch(f"/api/v1/projects/{rsi['id']}", json={"rules": rules})
    projects._STATE["key"] = None   # force a re-seed pass
    assert ex("google-chrome", "harness benchmark - Google Chrome")[0] == "网页浏览"


def test_app_id_stub_rows_hidden(roots):
    db.ex("INSERT INTO source_events(source_key,event_key,event_type,occurred_at,observed_at,day_key,"
          "payload,created_at) VALUES('activitywatch','k1','aw_window','2026-10-07T01:00:00+00:00',"
          "'2026-10-07T01:00:00+00:00','2026-10-07',?, '2026-10-07')",
          (json.dumps({"duration": 5, "data": {"app": "apport-gtk", "title": "Ubuntu"}}),))
    db.ex("INSERT INTO projects(name, created_at, updated_at) VALUES('apport-gtk','x','x')")
    projects._STATE["key"] = None
    projects.compiled()
    assert db.q1("SELECT active FROM projects WHERE name='apport-gtk'")["active"] == 0


def test_numeric_suffix_and_abbreviated_name(roots):
    assert ex("google-chrome", "CASMI26 | v4n Fusion - Google Chrome")[0] == "CASMI"
    assert ex("com.mitchellh.ghostty", "t paperfact")[0] == "paperfacts"
    assert ex("google-chrome", "pape - Google Chrome")[0] == "网页浏览"   # too short to trust
