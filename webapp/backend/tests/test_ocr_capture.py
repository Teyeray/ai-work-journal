"""OCR engines (OpenAI-compatible vision API, generic HTTP), the built-in
screen sampler, and screen search across screenpipe + built-in captures."""
import base64
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from app import capture, chat, config, db, ocr
from app.api import app
import osutil

JPG = b"\xff\xd8\xff\xe0fake-jpeg"


@pytest.fixture()
def server():
    """Local HTTP server; the test sets .reply (dict) and reads .seen."""
    seen = []

    class H(BaseHTTPRequestHandler):
        reply = {}

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            seen.append({"path": self.path, "headers": dict(self.headers), "body": body})
            out = json.dumps(H.reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.H, srv.seen, srv.url = H, seen, f"http://127.0.0.1:{srv.server_port}"
    yield srv
    srv.shutdown()


def _ocr(monkeypatch, **cfg):
    monkeypatch.setattr(config, "OCR", osutil._merge(config.OCR, cfg))


def test_openai_engine_sends_the_image_and_reads_the_answer(env, server, monkeypatch):
    server.H.reply = {"choices": [{"message": {"content": "  编辑器 main.py\nhello  "}}]}
    _ocr(monkeypatch, engine="openai", openai={"base_url": server.url + "/v1", "model": "qwen3.5-ocr"})
    monkeypatch.setenv("OCR_API_KEY", "k-test")
    r = ocr.recognize(JPG)
    assert r["text"] == "编辑器 main.py\nhello" and r["engine"] == "openai"
    req = server.seen[0]
    assert req["path"] == "/v1/chat/completions" and req["headers"]["Authorization"] == "Bearer k-test"
    body = json.loads(req["body"])
    img = body["messages"][0]["content"][0]["image_url"]["url"]
    assert body["model"] == "qwen3.5-ocr" and base64.b64decode(img.split(",", 1)[1]) == JPG


def test_openai_engine_without_a_key_is_reported_not_called(env, monkeypatch):
    _ocr(monkeypatch, engine="openai", openai={"base_url": "https://api.example.com/v1", "api_key_env": "NOPE_KEY"})
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    monkeypatch.delenv("NOPE_KEY", raising=False)
    ok, why = ocr.configured()
    assert not ok and "KEY" in why
    with pytest.raises(ocr.OcrError):
        ocr.recognize(JPG)


def test_http_engine_json_and_raw_modes(env, server, monkeypatch):
    # PaddleOCR-style answer: a list of boxes, each with text
    server.H.reply = {"result": [{"text": "line one"}, {"text": "第二行"}]}
    _ocr(monkeypatch, engine="http", http={"url": server.url + "/ocr", "mode": "json", "text_path": "result.text",
                                           "headers": {"X-Token": "t"}})
    assert ocr.recognize(JPG)["text"] == "line one\n第二行"
    sent = json.loads(server.seen[0]["body"])
    assert base64.b64decode(sent["image"]) == JPG and server.seen[0]["headers"]["X-Token"] == "t"
    server.H.reply = {"text": "raw ok"}
    _ocr(monkeypatch, http={"mode": "raw", "text_path": "text"})
    assert ocr.recognize(JPG)["text"] == "raw ok"
    assert server.seen[1]["body"] == JPG and server.seen[1]["headers"]["Content-Type"] == "image/jpeg"


def test_sampler_skips_unchanged_screens_and_sensitive_apps(env, tmp_path, monkeypatch, server):
    server.H.reply = {"text": "visible text"}
    _ocr(monkeypatch, engine="http", http={"url": server.url, "mode": "raw", "text_path": "text"})
    monkeypatch.setattr(config, "CAPTURE_DIR", tmp_path / "cap")
    monkeypatch.setattr(config, "CAPTURE", {**config.CAPTURE, "skip_unchanged": True})
    monkeypatch.setattr(config, "SENSITIVE_APPS", ["1password"])
    capture._S["sig"].clear()
    sig = bytes(32 * 18)
    monkeypatch.setattr(capture, "_grab", lambda: [(1, JPG, sig), (2, JPG, sig)])
    monkeypatch.setattr(capture, "_focus", lambda: ("code", "main.py — proj"))
    assert capture.capture_once()["captured"] == 2
    assert capture.capture_once()["captured"] == 0            # nothing changed
    monkeypatch.setattr(capture, "_grab", lambda: [(1, JPG, bytes([200]) * (32 * 18))])
    assert capture.capture_once()["captured"] == 1             # monitor 1 changed
    monkeypatch.setattr(capture, "_focus", lambda: ("1Password", "Vault"))
    assert capture.capture_once() == {"captured": 0, "skipped": "sensitive"}
    total, rows = capture.search("visible")
    assert total == 3 and rows[0]["window"] == "main.py — proj" and rows[0]["engine"] == "http"
    assert capture.image_file(rows[0]["id"]).read_bytes() == JPG
    c = TestClient(app)
    assert c.get(f"/api/v1/screen/capture/{rows[0]['id']}").content == JPG
    assert c.get("/api/v1/screen/capture/99999").status_code == 404


def test_sampler_cleanup_deletes_old_rows_and_files(env, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CAPTURE_DIR", tmp_path / "cap")
    old = tmp_path / "cap" / "2020-01-01" / "000000_m1.jpg"
    old.parent.mkdir(parents=True)
    old.write_bytes(JPG)
    db.ex("INSERT INTO screen_captures (captured_at, monitor, image) VALUES ('2020-01-01T00:00:00+00:00', 1, ?)",
          ("2020-01-01/000000_m1.jpg",))
    assert capture.cleanup() == 1 and not old.exists()
    assert db.q1("SELECT COUNT(*) n FROM screen_captures")["n"] == 0


def test_screen_text_merges_screenpipe_and_builtin_and_reocrs(env, tmp_path, monkeypatch, server):
    # one screenpipe frame (with an on-disk jpg) and one built-in capture
    base = tmp_path / "sp"
    (base / "2026-10-07").mkdir(parents=True)
    jpg = base / "2026-10-07" / "1_m1.jpg"
    jpg.write_bytes(JPG)
    monkeypatch.setattr(chat, "SP_DATA", base)
    monkeypatch.setattr(config, "SP_URL", "http://sp")
    monkeypatch.setattr(chat, "_sp_coverage", lambda: {"frames": 1})
    monkeypatch.setattr(chat, "_sp_get", lambda p: {"pagination": {"total": 1}, "data": [{"content": {
        "timestamp": "2026-10-07T06:00:00Z", "app_name": "Code", "window_name": "a.py",
        "text": "garbled ????", "frame_name": str(jpg)}}]})
    db.ex("INSERT INTO screen_captures (captured_at, monitor, app, window, image, text, engine) "
          "VALUES ('2026-10-07T07:00:00+00:00', 1, 'Chrome', 'docs', 'x.jpg', '飞书文档 季度计划', 'openai')")
    server.H.reply = {"text": "中文识别正确"}
    _ocr(monkeypatch, engine="http", reocr_screenpipe=True, http={"url": server.url, "mode": "raw", "text_path": "text"})
    r = chat.screen_text(start="2026-10-07", end="2026-10-07")
    assert r["total"] == 2 and [f["source"] for f in r["frames"]] == ["builtin", "screenpipe"]
    assert r["frames"][1]["text"] == "中文识别正确" and r["frames"][1]["ocr"] == "http"
    assert "_frame" not in r["frames"][1] and "可信" in r["note"]
    # second call is served from the OCR cache
    chat.screen_text(start="2026-10-07", end="2026-10-07")
    assert len(server.seen) == 1


def test_builtin_status_explains_what_is_missing(env, monkeypatch):
    monkeypatch.setattr(config, "CAPTURE", {**config.CAPTURE, "enabled": True})
    monkeypatch.setattr(capture, "support", lambda: (False, "Wayland 不允许普通程序读屏幕"))
    st = chat.screen_status()
    assert st["source"] == "builtin" and st["state"] == "down" and "Wayland" in st["reason"]
    monkeypatch.setattr(capture, "support", lambda: (True, ""))
    monkeypatch.setattr(capture, "running", lambda: True)
    _ocr(monkeypatch, engine="screenpipe")
    st = chat.screen_status()
    assert st["state"] == "stalled" and "OCR" in st["reason"]


@pytest.mark.parametrize("system,expect", [
    ("linux", ["systemctl", "--user", "restart", "sp"]),
    ("macos", ["launchctl", "kickstart", "-k"]),
    ("windows", ["schtasks", "/Run", "/TN", "sp"]),
])
def test_restart_service_uses_the_platform_service_manager(monkeypatch, system, expect):
    calls = []
    monkeypatch.setattr(osutil, "SYSTEM", system)
    monkeypatch.setattr(osutil.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    ok, _ = osutil.restart_service("sp")
    assert ok and calls[-1][:len(expect)] == expect


def test_schedule_install_writes_systemd_units_and_launchd_plist(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(osutil, "SYSTEM", "linux")
    osutil.install_schedule(("weekly",))
    timer = (tmp_path / "xdg" / "systemd" / "user" / "ai-journal-weekly.timer").read_text()
    svc = (tmp_path / "xdg" / "systemd" / "user" / "ai-journal-weekly.service").read_text()
    assert "OnCalendar=Fri *-*-* 23:30:00" in timer and "weekly.py --scheduled" in svc
    assert ["systemctl", "--user", "enable", "--now", "ai-journal-weekly.timer"] in calls
    monkeypatch.setattr(osutil, "SYSTEM", "macos")
    monkeypatch.setattr(osutil.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(osutil.os, "getuid", lambda: 501, raising=False)
    osutil.install_schedule(("daily",))
    plist = (tmp_path / "Library" / "LaunchAgents" / "org.ai-journal-daily.plist").read_text()
    assert "<key>Hour</key><integer>23</integer>" in plist and "--scheduled" in plist
    monkeypatch.setattr(osutil, "SYSTEM", "windows")
    osutil.install_schedule(("weekly",))
    assert calls[-1][:5] == ["schtasks", "/Create", "/F", "/TN", "ai-journal-weekly"] and "FRI" in calls[-1]


def test_config_local_overlay_and_path_expansion(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({"pi_dir": "~/.pi/s", "project_roots": ["~/P"], "a": {"x": 1, "y": 2}}))
    (tmp_path / "config.local.json").write_text(json.dumps({"a": {"y": 3}, "aw_host": "me"}))
    monkeypatch.setattr(osutil, "REPO_ROOT", tmp_path)
    monkeypatch.delenv("JOURNAL_NO_LOCAL_CONFIG")
    c = osutil.load_config(tmp_path, "config.json")
    assert c["a"] == {"x": 1, "y": 3} and c["aw_host"] == "me"
    assert not c["pi_dir"].startswith("~") and not c["project_roots"][0].startswith("~")


@pytest.mark.parametrize("raw,want", [
    ("196,158,89,301,90,季度计划\n314,294,61,549,90,x = 1, 2, 3, 4", "季度计划\nx = 1, 2, 3, 4"),
    ("```html\n<html><body><h2>标题</h2>\n<ol><li><p>完成 A</p></li><li><p>B &gt; C</p></li></ol></body></html>\n```",
     "标题\n完成 A\nB > C"),
    ("plain  text\n\n\nsecond", "plain text\nsecond"),
])
def test_clean_strips_boxes_fences_and_html_from_model_output(raw, want):
    assert ocr.clean(raw) == want


def test_windows_paths_in_titles_and_claude_dir_collisions(tmp_path, monkeypatch):
    from app import projects
    from app.adapters import sessions
    assert projects._PATH_IN_TITLE.findall(r"notes (C:\Users\a\proj\README.md) ~/p/q") == [r"C:\Users\a\proj\README.md", "~/p/q"]
    # ~/kaggle and ~/.kaggle both encode to "kaggle": the real name wins
    (tmp_path / "kaggle" / "GOOGLE-GEMMA").mkdir(parents=True)
    (tmp_path / ".kaggle").mkdir()
    (tmp_path / "my_proj.v2").mkdir()
    import re
    enc = re.sub(r"[^A-Za-z0-9-]", "-", str(tmp_path))      # '/tmp/x' → '-tmp-x', 'C:\\x' → 'C--x'
    assert sessions._cwd_from_dir(f"{enc}-kaggle-GOOGLE-GEMMA") == str(tmp_path / "kaggle" / "GOOGLE-GEMMA")
    assert sessions._cwd_from_dir(f"{enc}-my-proj-v2") == str(tmp_path / "my_proj.v2")
