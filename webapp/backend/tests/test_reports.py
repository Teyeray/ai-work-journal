"""Reports page: listing with Feishu links, safe reading, schedule round-trip."""
import json

import pytest
from fastapi.testclient import TestClient

from app import reports
from app.api import app


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    rep = tmp_path / "reports"
    (rep / "weekly").mkdir(parents=True)
    (rep / "2026-10-10.md").write_text("<!-- generated 2026-10-10T23:02:11+08:00 by x -->\n\n# 工作日报 2026-10-10\n\n## 一、今日完成\n- a\n", encoding="utf-8")
    (rep / "2026-10-09.md").write_text("> 警告: LLM 生成失败,以下为原始数据。\n# raw\n", encoding="utf-8")
    (rep / "weekly" / "2026-W41.md").write_text("# 工作周报 2026-W41\n", encoding="utf-8")
    (rep / "notes.md").write_text("not a report", encoding="utf-8")
    fmap = tmp_path / "feishu-map.json"
    fmap.write_text(json.dumps({"2026-10-10.md": {"url": "https://my.feishu.cn/docx/abc", "pushed_at": "x"}}))
    monkeypatch.setattr(reports, "REPORTS", rep)
    monkeypatch.setattr(reports, "WEEKLY", rep / "weekly")
    monkeypatch.setattr(reports, "FEISHU_MAP", fmap)
    return rep


def test_list_reports_with_feishu_links(repo):
    r = reports.list_reports()
    assert [d["name"] for d in r["daily"]] == ["2026-10-10.md", "2026-10-09.md"]
    d0 = r["daily"][0]
    assert d0["title"] == "工作日报 2026-10-10" and d0["feishu_url"].endswith("/abc") and d0["weekday"] == 6
    assert r["daily"][1]["fallback"] is True and r["daily"][1]["feishu_url"] is None
    assert [w["key"] for w in r["weekly"]] == ["2026-W41"]


def test_read_report_strips_header_and_rejects_traversal(repo):
    c = TestClient(app)
    body = c.get("/api/v1/reports/daily/2026-10-10.md").json()
    assert body["markdown"].startswith("# 工作日报")
    assert c.get("/api/v1/reports/daily/..%2Fconfig.json").status_code in (400, 404)
    assert c.get("/api/v1/reports/daily/notes.md").status_code == 400
    assert c.get("/api/v1/reports/daily/2026-01-01.md").status_code == 404


def test_schedule_roundtrip_and_validation(env):
    c = TestClient(app)
    assert c.get("/api/v1/reports/status").json()["schedule"]["daily_weekdays"] == [1, 2, 3, 4, 5, 6, 7]
    s = c.put("/api/v1/reports/schedule", json={"daily_weekdays": [1, 2, 3, 4, 5, 9], "feishu_push": False}).json()["schedule"]
    assert s == {"daily_weekdays": [1, 2, 3, 4, 5], "weekly_enabled": True, "feishu_push": False}
    assert json.loads(reports.SCHEDULE.read_text())["feishu_push"] is False


def test_run_rejects_unknown_kind(env):
    assert TestClient(app).post("/api/v1/reports/run/monthly").status_code == 400
