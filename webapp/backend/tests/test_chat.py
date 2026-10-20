"""问 AI: tools over local data, the tool loop, history replay and frame serving."""
import json
import shutil
import sys

import pytest
from fastapi.testclient import TestClient

from app import chat
from app.api import app
from conftest import aw_event, load_aw

DAY = "2026-10-07"


def _seed():
    load_aw([aw_event(DAY, "09:00", 30, title="t paperfacts"),
             aw_event(DAY, "09:30", 20, app="google-chrome", title="PaperFacts · 双路事实溯源 - Google Chrome"),
             aw_event(DAY, "10:00", 15, title="vim notes.md")])


def test_get_day_and_windows_are_bounded_and_filtered(env):
    _seed()
    d = chat.call_tool("get_day", {"day": DAY})
    assert d["day"] == DAY and d["observed_windows"] is True and "summary" in d
    w = chat.call_tool("get_windows", {"day": DAY, "start": "09:20", "end": "09:45"})
    assert len(w["windows"]) == 1 and "PaperFacts" in w["windows"][0]
    q = chat.call_tool("get_windows", {"day": DAY, "query": "vim"})
    assert len(q["windows"]) == 1 and q["windows"][0].startswith("10:00:00")


def test_bad_arguments_come_back_as_errors_not_exceptions(env):
    assert "error" in chat.call_tool("get_day", {"day": "10月7号"})
    assert "error" in chat.call_tool("get_day", {"nope": 1})
    assert "error" in chat.call_tool("no_such_tool", {})
    assert "error" in chat.call_tool("read_session", {"id": 999999})


def test_search_counts_window_hits(env, monkeypatch):
    _seed()
    monkeypatch.setattr(chat, "_sp_get", lambda p: (_ for _ in ()).throw(OSError("down")))
    r = chat.call_tool("search", {"query": "paperfacts", "start": DAY, "end": DAY})
    assert r["hits"]["windows"] == 2 and r["hits"]["screen_ocr"] is None


def test_large_results_are_truncated_with_a_hint():
    s = chat._dump({"x": "a" * (chat.RESULT_CHARS * 2)})
    assert len(s) < chat.RESULT_CHARS + 200 and "offset" in s


def _fake_stream(script):
    """Replace the model: each call pops the next scripted assistant message."""
    def f(messages, tools=True, model_id=None):
        msg = script.pop(0)
        if msg.get("content"):
            yield "delta", msg["content"]
        yield "message", msg
    return f


def test_ask_runs_tools_then_answers_and_persists(env, monkeypatch):
    _seed()
    call = {"id": "c1", "type": "function", "function": {"name": "get_day", "arguments": json.dumps({"day": DAY})}}
    monkeypatch.setattr(chat, "_stream", _fake_stream([
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "assistant", "content": "那天主要在 paperfacts。"}]))
    evs = list(chat.ask("10-07 在干嘛", context={"tab": "今日", "day": DAY}))
    kinds = [e["type"] for e in evs]
    assert kinds[0] == "thread" and "step" in kinds and "step_done" in kinds and kinds[-1] == "done"
    tid = evs[0]["id"]
    v = chat.thread_view(tid)
    t = v["turns"][0]
    assert t["question"] == "10-07 在干嘛" and t["answer"] == "那天主要在 paperfacts。"
    assert t["steps"][0]["name"] == "get_day" and t["steps"][0]["summary"].startswith(DAY)


def test_history_stubs_old_tool_output_and_drops_orphan_calls(env, monkeypatch):
    call = lambda i: {"id": f"c{i}", "type": "function", "function": {"name": "list_reports", "arguments": "{}"}}
    script = []
    for i in range(3):
        script += [{"role": "assistant", "content": "", "tool_calls": [call(i)]},
                   {"role": "assistant", "content": f"答{i}"}]
    monkeypatch.setattr(chat, "_stream", _fake_stream(script))
    tid = None
    for i in range(3):
        tid = [e for e in chat.ask(f"问{i}", tid)][0]["id"]
    # a turn cut off after the model asked for a tool but before it ran
    chat._save(tid, 4, {"role": "user", "content": "问3"})
    chat._save(tid, 4, {"role": "assistant", "content": "", "tool_calls": [call(9)]})
    hist, last = chat._history(tid)
    assert last == 4
    tools = [m for m in hist if m["role"] == "tool"]
    assert tools[0]["content"].startswith("(早前")          # turn 1: stubbed
    assert not tools[-1]["content"].startswith("(早前")     # turn 3: kept
    assert all(c["id"] != "c9" for m in hist for c in m.get("tool_calls") or [])


def test_api_threads_and_unavailable_llm(env):
    c = TestClient(app)
    assert c.get("/api/v1/chat/status").json()["available"] is False   # tests disable the LLM
    assert c.post("/api/v1/chat", json={"text": "hi"}).status_code == 503
    # the browser sends explicit nulls for a new thread; that must validate, not 422
    assert c.post("/api/v1/chat", json={"text": "hi", "thread_id": None, "context": None}).status_code == 503
    assert c.get("/api/v1/chat/threads").json() == {"threads": []}
    assert c.get("/api/v1/chat/threads/42").status_code == 404


def test_frame_route_only_serves_images_inside_screenpipe(tmp_path, monkeypatch):
    base = tmp_path / "sp"
    (base / "2026-10-05").mkdir(parents=True)
    jpg = base / "2026-10-05" / "1_m1.jpg"
    jpg.write_bytes(b"\xff\xd8jpg")
    (tmp_path / "secret.jpg").write_bytes(b"x")
    (base / "notes.txt").write_text("x")
    monkeypatch.setattr(chat, "SP_DATA", base)
    c = TestClient(app)
    assert c.get("/api/v1/screen/frame", params={"f": "2026-10-05/1_m1.jpg"}).status_code == 200
    assert c.get("/api/v1/screen/frame", params={"f": "../secret.jpg"}).status_code == 404
    assert c.get("/api/v1/screen/frame", params={"f": "notes.txt"}).status_code == 404
    assert c.get("/api/v1/screen/frame", params={"f": "notes.txt", "i": 3}).status_code == 404
    assert chat.frame_url({"frame_name": str(jpg)}).endswith("f=2026-10-05%2F1_m1.jpg")
    assert chat.frame_url({"frame_name": "/etc/passwd"}) is None
    # frame gone, but it lives inside a compacted video at index 25
    mp4 = base / "2026-10-05" / "compact_m1_1.mp4"
    mp4.write_bytes(b"\0")
    u = chat.frame_url({"frame_name": str(base / "gone.jpg"), "file_path": str(mp4), "offset_index": "25"})
    assert u.endswith("compact_m1_1.mp4&i=25")
    assert chat.frame_url({"file_path": str(mp4), "offset_index": None}) is None


def test_screen_status_flags_a_stalled_wayland_capture(tmp_path, monkeypatch):
    monkeypatch.setattr(chat.config, "SP_URL", "http://sp")
    monkeypatch.setattr(chat, "SP_LOG_DIR", tmp_path)
    (tmp_path / "screenpipe.2026-10-12.0.log").write_text(
        "WARN monitor 33 capture failed: waiting for Wayland screen-share approval or first PipeWire frame\n")
    monkeypatch.setattr(chat, "_sp_health", lambda: {
        "last_frame_timestamp": "2026-10-05T17:47:31+08:00", "vision_db_write_stalled": True,
        "monitors": ["Display 33", "Display 1210"], "pipeline": {"frame_drop_rate": 0.97}})
    chat._SP_STATUS.update(at=0, v=None)
    st = chat.screen_status()
    assert st["state"] == "stalled" and "Wayland" in st["reason"] and len(st["monitors"]) == 2

    def refuse():
        raise ConnectionRefusedError()
    monkeypatch.setattr(chat, "_sp_health", refuse)
    chat._SP_STATUS.update(at=0, v=None)
    assert chat.screen_status()["state"] == "down"
    chat._SP_STATUS.update(at=0, v=None)


def test_screen_restart_runs_systemctl_and_marks_awaiting_approval(monkeypatch):
    import subprocess
    calls = []
    monkeypatch.setattr(chat.config, "SP_URL", "http://sp")
    monkeypatch.setattr(chat.config, "SP_SERVICE", "screenpipe.service")
    monkeypatch.setattr(chat.config.osutil, "SYSTEM", "linux")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    chat._SP_STATUS.update(at=0, v=None, restarted_at=0)
    r = TestClient(app).post("/api/v1/screen/restart")
    assert r.status_code == 200 and calls == [["systemctl", "--user", "restart", "screenpipe.service"]]
    # a second click right away is refused instead of restarting again
    assert TestClient(app).post("/api/v1/screen/restart").status_code == 409
    monkeypatch.setattr(chat, "_sp_health", lambda: {"last_frame_timestamp": None, "monitors": ["a", "b"]})
    st = chat.screen_status()
    assert st["state"] == "stalled" and st["awaiting_approval"] and st["restartable"]
    chat._SP_STATUS.update(at=0, v=None, restarted_at=0)


def test_missing_screencast_portal_is_named_and_restart_fixes_the_portal_first(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(chat.config, "SP_URL", "http://sp")
    monkeypatch.setattr(chat.config, "SP_SERVICE", "screenpipe.service")
    monkeypatch.setattr(chat.config.osutil, "SYSTEM", "linux")
    monkeypatch.setattr(chat, "SP_LOG_DIR", tmp_path)
    (tmp_path / "screenpipe.2026-10-13.0.log").write_text(
        "INFO requesting one-time Wayland screen-share approval for all selected monitors\n"
        "WARN Wayland PipeWire capture failed: failed to connect to xdg-desktop-portal ScreenCast: "
        "A portal frontend implementing `org.freedesktop.portal.ScreenCast` was not found\n")
    monkeypatch.setattr(chat, "_sp_health", lambda: {"last_frame_timestamp": "2026-10-13T01:10:34+00:00",
                                                     "vision_db_write_stalled": True, "monitors": ["a", "b"]})
    chat._SP_STATUS.update(at=0, v=None, restarted_at=0)
    st = chat.screen_status()
    assert st["state"] == "stalled" and st["portal_broken"] and "xdg-desktop-portal" in st["reason"]
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    assert chat.restart_screen()["ok"]
    assert calls == [["systemctl", "--user", "restart", "xdg-desktop-portal.service"],
                     ["systemctl", "--user", "restart", "screenpipe.service"]]
    chat._SP_STATUS.update(at=0, v=None, restarted_at=0)


# Windows runners resolve "bash" to the WSL stub, which can't run commands
@pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash"), reason="run_bash needs a POSIX bash")
def test_read_only_bash_runs_but_writes_wait_for_approval(env, monkeypatch, tmp_path):
    from app import localtools
    monkeypatch.setattr(localtools, "sandbox_available", lambda: True)
    assert localtools.classify("git log --oneline | head -3") == ""
    assert localtools.classify("sort -oout x") and localtools.classify("git --exec-path=/x log")
    assert localtools.classify("ls && rm -rf x")
    assert localtools.classify("echo x > f")
    assert localtools.classify("cat ~/.ssh/id_rsa")
    target = tmp_path / "made"
    calls = [{"id": "r", "type": "function", "function": {"name": "run_bash", "arguments": json.dumps(
        {"command": "touch made", "cwd": str(tmp_path)})}}]
    for allow in (False, True):
        monkeypatch.setattr(chat, "_stream", _fake_stream([
            {"role": "assistant", "content": "", "tool_calls": calls}, {"role": "assistant", "content": "好"}]))
        gen = chat.ask("建个文件")
        evs = []
        for e in gen:
            evs.append(e)
            if e["type"] == "approval":
                assert e["command"].startswith("touch") and e["reason"]
                assert chat.approve(e["token"], allow)
        done = next(e for e in evs if e["type"] == "step_done")
        assert target.exists() is allow
        assert bool(done["error"]) is not allow
    assert not chat.approve("nope", True)


def test_selected_days_go_into_the_prompt(env):
    _seed()
    p = chat.system_prompt({"tab": "今日", "day": DAY, "days": [DAY, "2026-10-05", "bad"]})
    assert "选中的 2 天" in p and f"- {DAY}" in p and "- 2026-10-05" in p
    assert f"选中日期 {DAY}" not in p


def test_web_after_local_reads_needs_approval_and_sandbox_flag_is_ignored(env, monkeypatch, tmp_path):
    from app import localtools
    monkeypatch.setattr(localtools, "sandbox_available", lambda: True)
    seen = {}
    monkeypatch.setattr(localtools, "run_bash", lambda **a: seen.update(a) or {"summary": "ok"})
    monkeypatch.setattr(localtools, "web_fetch", lambda **a: {"summary": "fetched"})
    monkeypatch.setitem(chat.IMPL, "run_bash", localtools.run_bash)
    monkeypatch.setitem(chat.IMPL, "web_fetch", localtools.web_fetch)
    bash = {"id": "b", "type": "function", "function": {"name": "run_bash", "arguments": json.dumps(
        {"command": "ls", "sandbox": False})}}
    fetch = {"id": "w", "type": "function", "function": {"name": "web_fetch", "arguments": json.dumps(
        {"url": "https://example.com/?d=secret"})}}
    monkeypatch.setattr(chat, "_stream", _fake_stream([
        {"role": "assistant", "content": "", "tool_calls": [bash]},
        {"role": "assistant", "content": "", "tool_calls": [fetch]}, {"role": "assistant", "content": "完"}]))
    evs = []
    for e in chat.ask("看看"):
        evs.append(e)
        if e["type"] == "approval":
            chat.approve(e["token"], False)
    assert "sandbox" not in seen                                   # read-only ls ran sandboxed (default)
    ap = [e for e in evs if e["type"] == "approval"]
    assert len(ap) == 1 and ap[0]["command"].startswith("web_fetch")


def test_model_must_be_one_of_the_offered(env, monkeypatch):
    monkeypatch.setitem(chat.config.CHAT, "models", ["kimi-k3", "kimi-k3", ""])
    assert chat.models() == [chat.model(), "kimi-k3"]
    used = []
    monkeypatch.setattr(chat, "_stream", lambda m, tools=True, model_id=None: (
        used.append(model_id), (yield "message", {"role": "assistant", "content": "好"}))[0])
    assert [e["type"] for e in chat.ask("hi", model_id="kimi-k3")][-1] == "done" and used == ["kimi-k3"]
    evs = list(chat.ask("hi", model_id="gpt-x"))
    assert evs[0]["type"] == "error" and len(used) == 1


def test_set_default_model_persists_locally(env, monkeypatch, tmp_path):
    local = tmp_path / "config.local.json"
    local.write_text(json.dumps({"classification": {"x": 1}}), encoding="utf-8")
    monkeypatch.setattr(chat.config, "WEB_LOCAL_CONFIG", local)
    monkeypatch.setattr(chat.config, "CHAT", {**chat.config.CHAT, "models": ["kimi-k3"]})
    base = chat.base_model()
    assert chat.model() == base
    assert not chat.set_default("gpt-x")
    assert chat.set_default("kimi-k3") and chat.model() == "kimi-k3"
    saved = json.loads(local.read_text(encoding="utf-8"))
    assert saved == {"classification": {"x": 1}, "chat": {"default": "kimi-k3"}}
    chat.config.CHAT["models"] = []            # no longer offered: falls back
    assert chat.model() == base
