"""macOS adaptation acceptance: local Vision OCR, the local-origin guard, the
opencode report-LLM event parser, and the native Codex / OpenCode readers.

Every input here is synthetic: no real screenshot, no real session log, no real
opencode database, no network. The Vision path is exercised with a stub helper,
so these tests run identically on Linux, macOS and in CI.
"""
import datetime as dt
import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import agent_sources
import llm_cli
from app import api, config, ocr

DAY = "2026-10-07"
TZ8 = dt.timezone(dt.timedelta(hours=8))
DAY_START = dt.datetime(2026, 10, 7, 0, 0, 0, tzinfo=TZ8)
DAY_END = dt.datetime(2026, 10, 8, 0, 0, 0, tzinfo=TZ8)

PNG = b"\x89PNG\r\n\x1a\nfake-png"


# ============================================================ Vision (local OCR)

def _vision_env(monkeypatch, **extra):
    """ocr.engine=vision with no API credentials of any kind."""
    monkeypatch.setattr(config, "OCR", {**config.OCR, "engine": "vision", **extra})
    monkeypatch.delenv("OCR_API_KEY", raising=False)
    monkeypatch.delenv("OCR_BASE_URL", raising=False)


def _fake_helper(monkeypatch, stdout="", returncode=0, captured=None):
    """Replace the Vision helper process with a stub: no swiftc, no real binary."""
    monkeypatch.setattr(ocr, "vision_binary", lambda: "/nonexistent/vision-ocr")

    def _run(cmd, **kwargs):
        if captured is not None:
            captured.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout, "")

    monkeypatch.setattr(ocr.subprocess, "run", _run)


def _no_network(monkeypatch):
    """Any HTTP attempt from the OCR path fails the test."""
    def _boom(*a, **k):
        raise AssertionError("the local Vision path must not touch the network")

    monkeypatch.setattr(ocr, "_post", _boom)
    monkeypatch.setattr(ocr.urllib.request, "urlopen", _boom)


def test_vision_is_a_local_engine_not_an_api_engine(env, monkeypatch):
    _vision_env(monkeypatch)
    assert ocr.engine() == "vision"
    assert ocr.is_api() is False
    assert "vision" in ocr.LOCAL_ENGINES
    assert "vision" not in ocr.API_ENGINES


@pytest.mark.parametrize("alias", ["vision", "macos-vision", "apple-vision", "local-vision"])
def test_vision_aliases_normalize_to_one_engine(env, monkeypatch, alias):
    monkeypatch.setattr(config, "OCR", {**config.OCR, "engine": alias})
    assert ocr.engine() == "vision"
    assert ocr.is_api() is False


def test_vision_recognize_reads_the_local_helper_and_makes_no_http_call(env, monkeypatch):
    _vision_env(monkeypatch)
    calls = []
    payload = json.dumps({"text": "196,158,89,301,90,hello\n```\nworld\n```",
                          "lines": 2, "engine": "macos-vision"})
    _fake_helper(monkeypatch, stdout=payload, captured=calls)
    _no_network(monkeypatch)

    out = ocr.recognize(PNG, "image/png")

    assert out["engine"] == "vision"
    assert isinstance(out["ms"], int)
    # box coordinates and the code fence are stripped, the words survive
    assert out["text"] == "hello\nworld"
    # the helper received the image path plus the default languages, nothing else
    assert calls and calls[0][0] == "/nonexistent/vision-ocr"
    assert "zh-Hans" in calls[0] and "en-US" in calls[0]


def test_vision_uses_the_configured_languages(env, monkeypatch):
    _vision_env(monkeypatch, vision={"languages": ["zh-Hans", "en-US"], "timeout": 5})
    calls = []
    _fake_helper(monkeypatch, stdout=json.dumps({"text": "hi"}), captured=calls)
    _no_network(monkeypatch)
    ocr.recognize(PNG, "image/png")
    assert calls[0][2:] == ["zh-Hans", "en-US"]


def test_vision_helper_failure_raises_ocr_error_with_detail(env, monkeypatch):
    _vision_env(monkeypatch)
    _fake_helper(monkeypatch, stdout="", returncode=3)
    _no_network(monkeypatch)
    with pytest.raises(ocr.OcrError) as ex:
        ocr.recognize(PNG, "image/png")
    assert "exited 3" in str(ex.value)


def test_vision_helper_non_json_output_raises_ocr_error(env, monkeypatch):
    _vision_env(monkeypatch)
    _fake_helper(monkeypatch, stdout="Segmentation fault (core dumped)")
    _no_network(monkeypatch)
    with pytest.raises(ocr.OcrError) as ex:
        ocr.recognize(PNG, "image/png")
    assert "no JSON" in str(ex.value)


def test_vision_helper_json_without_text_raises_ocr_error(env, monkeypatch):
    _vision_env(monkeypatch)
    _fake_helper(monkeypatch, stdout=json.dumps({"lines": 0}))
    _no_network(monkeypatch)
    with pytest.raises(ocr.OcrError):
        ocr.recognize(PNG, "image/png")


def test_vision_helper_timeout_raises_ocr_error(env, monkeypatch):
    _vision_env(monkeypatch, vision={"languages": ["en-US"], "timeout": 1})
    monkeypatch.setattr(ocr, "vision_binary", lambda: "/nonexistent/vision-ocr")

    def _timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(ocr.subprocess, "run", _timeout)
    with pytest.raises(ocr.OcrError) as ex:
        ocr.recognize(PNG, "image/png")
    assert "timed out" in str(ex.value)


def test_vision_is_graceful_on_non_macos(env, monkeypatch):
    """No swiftc, no prebuilt helper, no compile: a clear error, not a crash."""
    monkeypatch.setattr(ocr.sys, "platform", "linux")
    monkeypatch.setattr(config, "OCR",
                        {**config.OCR, "engine": "vision", "vision": {"binary": ""}})

    ok, why = ocr.configured()
    assert ok is False
    assert "macOS" in why

    with pytest.raises(ocr.OcrError) as ex:
        ocr.recognize(PNG, "image/png")
    assert "macOS" in str(ex.value)

    d = ocr.describe()
    assert d["engine"] == "vision" and d["configured"] is False
    assert d["local"] is True and d["platform"] == "linux"
    assert "macOS" in (d["problem"] or "")


def test_vision_describe_reports_local_and_never_claims_a_key(env, monkeypatch):
    _vision_env(monkeypatch)
    _fake_helper(monkeypatch, stdout=json.dumps({"text": "ok"}))
    d = ocr.describe()
    assert d["local"] is True
    assert "has_key" not in d and "base_url" not in d
    assert d["configured"] is True
    assert d["languages"] == ["zh-Hans", "zh-Hant", "en-US"]


def test_vision_temp_file_is_removed_even_when_the_helper_fails(env, monkeypatch):
    _vision_env(monkeypatch)
    seen = []

    def _run(cmd, **kwargs):
        seen.append(Path(cmd[1]))
        return subprocess.CompletedProcess(cmd, 1, "", "boom")

    monkeypatch.setattr(ocr, "vision_binary", lambda: "/nonexistent/vision-ocr")
    monkeypatch.setattr(ocr.subprocess, "run", _run)
    with pytest.raises(ocr.OcrError):
        ocr.recognize(PNG, "image/png")
    assert seen and not seen[0].exists()


# ============================================================ local-origin guard

def _client():
    return TestClient(api.app)


FOREIGN = "http://evil.example.com"
LOCAL = "http://127.0.0.1:8799"

_OVERRIDE = {"day_key": DAY, "anchor_start": f"{DAY}T09:00:00+08:00",
             "field": "project", "value": "x"}


def _limits(**over):
    """Full limits dict: the readers index max_files/max_bytes/max_sessions
    directly, so a partial dict is a KeyError rather than a default."""
    return {**agent_sources.cfg(), **over}


def test_origin_guard_rejects_a_foreign_state_changing_request(env):
    r = _client().post("/api/v1/overrides", headers={"origin": FOREIGN}, json=_OVERRIDE)
    assert r.status_code == 403
    assert r.json()["detail"] == "cross-origin request refused"


def test_origin_guard_permits_a_local_post(env):
    r = _client().post("/api/v1/overrides", headers={"origin": LOCAL}, json=_OVERRIDE)
    assert r.status_code != 403, r.text


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8799", "http://localhost:8799",
                                    "http://[::1]:8799", "https://localhost:8799",
                                    "http://user:pw@localhost:8799"])
def test_origin_guard_permits_local_origins_on_the_configured_port(env, origin):
    r = _client().post("/api/v1/overrides", headers={"origin": origin}, json=_OVERRIDE)
    assert r.status_code != 403, r.text


@pytest.mark.parametrize("origin,why", [
    ("http://localhost:8798", "a different local port"),
    ("https://127.0.0.1", "no port at all"),
    ("http://0.0.0.0:8799", "0.0.0.0 is not an allowed host"),
    ("http://localhost.evil.com:8799", "suffix host confusion"),
])
def test_origin_guard_refuses_local_lookalikes_that_are_not_the_dashboard(env, origin, why):
    r = _client().post("/api/v1/overrides", headers={"origin": origin}, json=_OVERRIDE)
    assert r.status_code == 403, why


def test_origin_guard_port_matches_the_served_port(env):
    """The guard compares against config.PORT. `journal demo` serves on a
    different port (journal.py), so a demo browser sends an Origin the guard
    does not recognise. Documented as a known limitation, not endorsed."""
    assert api._origin_is_local(f"http://127.0.0.1:{config.PORT}") is True
    assert api._origin_is_local("http://127.0.0.1:8798") is (config.PORT == 8798)


def test_origin_guard_leaves_non_browser_clients_alone(env):
    """No Origin header means curl / CLI / scheduler, not a web page."""
    r = _client().post("/api/v1/overrides", json=_OVERRIDE)
    assert r.status_code != 403, r.text


def test_origin_guard_allows_a_foreign_origin_on_safe_methods(env):
    r = _client().get("/api/v1/health", headers={"origin": FOREIGN})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_origin_guard_allows_a_foreign_origin_on_a_delete_outside_the_guard(env):
    """DELETE is not in _SAFE_METHODS, so /api/ routes are covered too."""
    r = _client().delete("/api/v1/overrides/does-not-exist", headers={"origin": FOREIGN})
    assert r.status_code == 403


@pytest.mark.parametrize("origin,local", [
    ("http://evil.example.com", False),
    ("https://evil.example.com:8799", False),
    ("http://127.0.0.1:8799", True),
    ("http://localhost:8799", True),
    ("http://127.0.0.1:8799/path", True),
    ("http://user:pw@localhost:8799", True),
    ("http://[::1]:8799", True),
    ("http://[2001:db8::1]:8799", False),
    ("http://localhost.evil.com:8799", False),
    ("http://notlocalhost:8799", False),
    ("file:///etc/passwd", False),
    ("", False),
    ("not a url", False),
])
def test_origin_host_parsing(origin, local):
    assert api._origin_is_local(origin) is local


# ============================================================ opencode report LLM

def _events(*objs):
    return "\n".join(json.dumps(o) for o in objs) + "\n"


def test_llm_parser_reads_assistant_text_from_json_events():
    raw = _events({"type": "step-start"},
                  {"type": "text", "text": "## 今日报告\n"},
                  {"type": "text", "text": "- 做完了迁移验收"})
    assert llm_cli.parse_opencode_json(raw) == "## 今日报告\n- 做完了迁移验收"


def test_llm_parser_rejects_a_tool_call():
    raw = _events({"type": "text", "text": "looking"},
                  {"type": "tool_use", "name": "bash", "input": {"command": "rm -rf /"}})
    with pytest.raises(llm_cli.LlmCliError) as ex:
        llm_cli.parse_opencode_json(raw)
    assert "tools" in str(ex.value).lower()


def test_llm_parser_rejects_a_tool_result_event():
    with pytest.raises(llm_cli.LlmCliError) as ex:
        llm_cli.parse_opencode_json(_events({"type": "tool-result", "text": "ok"}))
    assert "tools" in str(ex.value).lower()


def test_llm_parser_rejects_a_service_error_event():
    with pytest.raises(llm_cli.LlmCliError) as ex:
        llm_cli.parse_opencode_json(_events({"type": "error", "message": "provider returned 503"}))
    assert "error" in str(ex.value).lower()
    assert "503" in str(ex.value)


def test_llm_parser_rejects_an_embedded_error_field():
    raw = _events({"type": "text", "text": "partial", "error": "context length exceeded"})
    with pytest.raises(llm_cli.LlmCliError) as ex:
        llm_cli.parse_opencode_json(raw)
    assert "context length exceeded" in str(ex.value)


def test_llm_parser_rejects_plain_invalid_output():
    with pytest.raises(llm_cli.LlmCliError) as ex:
        llm_cli.parse_opencode_json("Here is your report!\nSorry, I cannot help.")
    assert "no JSON events" in str(ex.value)


def test_llm_parser_rejects_empty_output():
    with pytest.raises(llm_cli.LlmCliError):
        llm_cli.parse_opencode_json("")
    with pytest.raises(llm_cli.LlmCliError):
        llm_cli.parse_opencode_json("\n\n")


def test_llm_parser_rejects_json_without_assistant_text():
    raw = _events({"type": "step-start"}, {"type": "step-finish", "tokens": 12})
    with pytest.raises(llm_cli.LlmCliError) as ex:
        llm_cli.parse_opencode_json(raw)
    assert "no assistant text" in str(ex.value)


def test_llm_parser_ignores_blank_and_unparsable_lines():
    raw = "\n\n" + json.dumps({"type": "text", "text": "kept"}) + "\nnot json at all\n"
    assert llm_cli.parse_opencode_json(raw) == "kept"


# ============================================================ native Codex reader

def _codex_line(ts, t, payload):
    return json.dumps({"timestamp": ts, "type": t, "payload": payload})


def _write_rollout(path: Path, lines, mtime_ts):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.utime(path, (mtime_ts, mtime_ts))


def _codex_meta(sid, cwd="/repo/app", forked=None, source=None, ts="2026-10-07T08:00:00Z"):
    payload = {"id": sid, "cwd": cwd}
    if forked is not None:
        payload["forked_from_id"] = forked
    if source is not None:
        payload["source"] = source
    return _codex_line(ts, "session_meta", payload)


def test_codex_native_id_comes_from_session_meta_not_the_filename(tmp_path):
    """The rollout filename is not the session id; session_meta carries it."""
    root = tmp_path / "sessions"
    root.mkdir()
    f = root / "rollout-2026-10-07T09-00-00-0199abcd.jsonl"
    _write_rollout(f, [
        _codex_meta("0199abcd-real-session-id"),
        _codex_line("2026-10-07T09:05:00Z", "response_item",
                    {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "finish the migration"}]}),
        _codex_line("2026-10-07T09:06:00Z", "response_item",
                    {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "done"}]}),
    ], DAY_START.timestamp() + 3600)

    out = agent_sources.collect_codex(DAY_START, DAY_END, _limits(codex_dir=str(root)))
    assert len(out) == 1
    s = out[0]
    assert s["native_id"] == "0199abcd-real-session-id"
    assert "0199abcd-real-session-id" in s["file"]
    assert s["agent"] == "codex"
    assert s["task"] == "finish the migration"


def test_codex_first_and_last_cover_only_work_on_the_requested_day(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    _write_rollout(root / "rollout-x.jsonl", [
        _codex_line("2026-10-06T23:50:00+08:00", "session_meta", {"id": "s-previous-day"}),
        _codex_line("2026-10-06T23:55:00+08:00", "response_item",
                    {"type": "message", "role": "user", "content": "yesterday"}),
        _codex_line("2026-10-07T10:00:00+08:00", "response_item",
                    {"type": "message", "role": "user", "content": "today only"}),
        _codex_line("2026-10-07T11:00:00+08:00", "response_item",
                    {"type": "message", "role": "assistant", "content": "answer"}),
        _codex_line("2026-10-08T00:30:00+08:00", "response_item",
                    {"type": "message", "role": "assistant", "content": "tomorrow"}),
    ], DAY_START.timestamp() + 3600)

    out = agent_sources.collect_codex(DAY_START, DAY_END, _limits(codex_dir=str(root)))
    assert len(out) == 1
    s = out[0]
    assert s["first"] == dt.datetime(2026, 10, 7, 10, 0, tzinfo=TZ8)
    assert s["last"] == dt.datetime(2026, 10, 7, 11, 0, tzinfo=TZ8)
    assert s["span"] == 3600
    texts = [t["x"] for t in s["turns"]]
    assert "yesterday" not in texts and "tomorrow" not in texts
    assert "today only" in texts


def test_codex_day_without_work_yields_nothing(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    _write_rollout(root / "rollout-y.jsonl", [
        _codex_line("2026-10-06T23:50:00+08:00", "session_meta", {"id": "s-old"}),
        _codex_line("2026-10-06T23:55:00+08:00", "response_item",
                    {"type": "message", "role": "user", "content": "old"}),
    ], DAY_START.timestamp() + 3600)
    assert agent_sources.collect_codex(DAY_START, DAY_END, _limits(codex_dir=str(root))) == []


def test_codex_root_session_has_no_parent(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    _write_rollout(root / "rollout-z.jsonl", [
        _codex_meta("s-root"),
        _codex_line("2026-10-07T10:00:00+08:00", "response_item",
                    {"type": "message", "role": "user", "content": "hi"}),
    ], DAY_START.timestamp() + 3600)
    s = agent_sources.collect_codex(DAY_START, DAY_END, _limits(codex_dir=str(root)))[0]
    assert s["native_parent_id"] is None
    assert s["native_kind"] == "root"


def test_codex_child_session_retains_its_parent_child_id(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    _write_rollout(root / "rollout-child.jsonl", [
        _codex_line("2026-10-07T08:00:00Z", "session_meta",
                    {"id": "s-child", "cwd": "/repo/app",
                     "source": {"subagent": {"thread_spawn": {"parent_thread_id": "s-parent"}}}}),
        _codex_line("2026-10-07T10:00:00+08:00", "response_item",
                    {"type": "message", "role": "user", "content": "subtask"}),
    ], DAY_START.timestamp() + 3600)
    s = agent_sources.collect_codex(DAY_START, DAY_END, _limits(codex_dir=str(root)))[0]
    assert s["native_id"] == "s-child"
    assert s["native_parent_id"] == "s-parent"
    assert s["native_kind"] == "child"
    assert s["cwd"] == "/repo/app"


def test_codex_fork_is_not_silently_a_continuation(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    _write_rollout(root / "rollout-fork.jsonl", [
        _codex_line("2026-10-07T08:00:00Z", "session_meta",
                    {"id": "s-fork", "forked_from_id": "s-origin"}),
        _codex_line("2026-10-07T10:00:00+08:00", "response_item",
                    {"type": "message", "role": "user", "content": "try another way"}),
    ], DAY_START.timestamp() + 3600)
    s = agent_sources.collect_codex(DAY_START, DAY_END, _limits(codex_dir=str(root)))[0]
    assert s["native_kind"] == "fork"
    assert s.get("forked_from_id") == "s-origin"


def test_codex_roles_are_explicit_and_never_claim_human_typing(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    _write_rollout(root / "rollout-roles.jsonl", [
        _codex_meta("s-roles"),
        _codex_line("2026-10-07T10:00:00+08:00", "event_msg",
                    {"type": "user_message", "message": "typed by me"}),
        _codex_line("2026-10-07T10:01:00+08:00", "response_item",
                    {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "echo of me"}]}),
        _codex_line("2026-10-07T10:02:00+08:00", "response_item",
                    {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "reply"}]}),
    ], DAY_START.timestamp() + 3600)

    s = agent_sources.collect_codex(DAY_START, DAY_END, _limits(codex_dir=str(root)))[0]
    assert s["user_msgs"] == 1 and s["assistant_msgs"] == 1
    roles = {t["x"]: t["role"] for t in s["turns"]}
    assert roles["typed by me"] == "user"
    assert roles["echo of me"] == "user"
    assert roles["reply"] == "assistant"
    assert all(t["typed"] is False for t in s["turns"])


def test_codex_counts_tool_calls_and_failed_tool_output(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    _write_rollout(root / "rollout-tools.jsonl", [
        _codex_meta("s-tools"),
        _codex_line("2026-10-07T10:00:00+08:00", "response_item",
                    {"type": "function_call", "name": "shell",
                     "arguments": json.dumps({"command": "pytest"})}),
        _codex_line("2026-10-07T10:00:05+08:00", "response_item",
                    {"type": "function_call_output", "call_id": "c1",
                     "output": '{"success":false,"error":"boom"}'}),
    ], DAY_START.timestamp() + 3600)
    s = agent_sources.collect_codex(DAY_START, DAY_END, _limits(codex_dir=str(root)))[0]
    assert s["tools"] == {"shell": 1}
    assert s["tool_errors"] == 1


def test_codex_source_is_skipped_when_not_configured():
    assert agent_sources.collect_codex(DAY_START, DAY_END, _limits(codex_dir="")) == []
    assert agent_sources.collect_codex(DAY_START, DAY_END, _limits(codex_dir="/no/such/dir")) == []


def test_codex_read_only_and_idempotent(tmp_path):
    """Nothing is written back, and two reads agree with each other."""
    root = tmp_path / "sessions"
    root.mkdir()
    f = root / "rollout-ro.jsonl"
    _write_rollout(f, [
        _codex_meta("s-ro"),
        _codex_line("2026-10-07T10:00:00+08:00", "response_item",
                    {"type": "message", "role": "user", "content": "read only"}),
    ], DAY_START.timestamp() + 3600)
    before = hashlib.sha256(f.read_bytes()).hexdigest()
    mtime = f.stat().st_mtime

    a = agent_sources.collect_codex(DAY_START, DAY_END, _limits(codex_dir=str(root)))
    b = agent_sources.collect_codex(DAY_START, DAY_END, _limits(codex_dir=str(root)))

    assert a == b, "re-reading the same rollout must be idempotent"
    assert hashlib.sha256(f.read_bytes()).hexdigest() == before
    assert f.stat().st_mtime == mtime


def test_codex_reads_are_bounded_by_max_bytes(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    _write_rollout(root / "rollout-big.jsonl", [
        _codex_meta("s-big"),
        _codex_line("2026-10-07T10:00:00+08:00", "response_item",
                    {"type": "message", "role": "user", "content": "never read"}),
    ], DAY_START.timestamp() + 3600)
    out = agent_sources.collect_codex(DAY_START, DAY_END,
                                      _limits(codex_dir=str(root), max_bytes=10))
    assert out == []


# ============================================================ native OpenCode reader

def _oc_db(tmp_path) -> Path:
    """Synthetic opencode.db. `session` has NO model column: the model lives in
    message.data as providerID / modelID."""
    p = tmp_path / "opencode.db"
    con = sqlite3.connect(p)
    con.executescript("""
        CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT,
                              title TEXT, time_created INTEGER, time_updated INTEGER);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT,
                              time_created INTEGER, data TEXT);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT,
                           time_created INTEGER, data TEXT);
    """)
    con.commit()
    con.close()
    return p


def _ms(ts):
    return int(ts.timestamp() * 1000)


def _add_session(p, sid, *, parent=None, title="", created=DAY_START, updated=None):
    con = sqlite3.connect(p)
    con.execute("INSERT INTO session VALUES(?,?,?,?,?,?)",
                (sid, parent, "/repo/app", title,
                 _ms(created), _ms(updated or created + dt.timedelta(hours=1))))
    con.commit()
    con.close()


def _add_message(p, mid, sid, ts, role, provider="opencode", model=None):
    con = sqlite3.connect(p)
    con.execute("INSERT INTO message VALUES(?,?,?,?)",
                (mid, sid, _ms(ts), json.dumps({"role": role, "providerID": provider,
                                                "modelID": model})))
    con.commit()
    con.close()


def _add_part(p, pid, mid, ts, data):
    con = sqlite3.connect(p)
    con.execute("INSERT INTO part VALUES(?,?,?,?)", (pid, mid, _ms(ts), json.dumps(data)))
    con.commit()
    con.close()


def test_opencode_native_id_roles_and_model_come_from_message_data(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_cli, "self_run_ids", lambda: set())
    monkeypatch.setattr(llm_cli, "self_run_marker", lambda: "ai-work-journal report")
    p = _oc_db(tmp_path)
    _add_session(p, "ses_1", title="migration work")
    _add_message(p, "msg_1", "ses_1", dt.datetime(2026, 10, 7, 9, 0, tzinfo=TZ8), "user")
    _add_part(p, "prt_1", "msg_1", dt.datetime(2026, 10, 7, 9, 0, tzinfo=TZ8),
              {"type": "text", "text": "please finish acceptance"})
    _add_message(p, "msg_2", "ses_1", dt.datetime(2026, 10, 7, 9, 5, tzinfo=TZ8),
                 "assistant", model="some-model")
    _add_part(p, "prt_2", "msg_2", dt.datetime(2026, 10, 7, 9, 5, tzinfo=TZ8),
              {"type": "text", "text": "all green"})

    out = agent_sources.collect_opencode(DAY_START, DAY_END, _limits(opencode_db=str(p)))
    assert len(out) == 1
    s = out[0]
    assert s["native_id"] == "ses_1" and s["agent"] == "opencode"
    assert s["task"] == "migration work"
    assert s["user_msgs"] == 1 and s["assistant_msgs"] == 1
    assert s["model"] == "some-model"
    roles = {t["x"]: t["role"] for t in s["turns"]}
    assert roles == {"please finish acceptance": "user", "all green": "assistant"}
    assert all(t["typed"] is False for t in s["turns"])


def test_opencode_day_filtering_uses_message_time(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_cli, "self_run_ids", lambda: set())
    monkeypatch.setattr(llm_cli, "self_run_marker", lambda: "ai-work-journal report")
    p = _oc_db(tmp_path)
    _add_session(p, "ses_day", title="spans days",
                 created=dt.datetime(2026, 10, 6, 22, 0, tzinfo=TZ8),
                 updated=dt.datetime(2026, 10, 7, 10, 0, tzinfo=TZ8))
    _add_message(p, "m_old", "ses_day", dt.datetime(2026, 10, 6, 22, 1, tzinfo=TZ8), "user")
    _add_part(p, "p_old", "m_old", dt.datetime(2026, 10, 6, 22, 1, tzinfo=TZ8),
              {"type": "text", "text": "yesterday"})
    _add_message(p, "m_new", "ses_day", dt.datetime(2026, 10, 7, 9, 0, tzinfo=TZ8), "user")
    _add_part(p, "p_new", "m_new", dt.datetime(2026, 10, 7, 9, 0, tzinfo=TZ8),
              {"type": "text", "text": "today"})

    out = agent_sources.collect_opencode(DAY_START, DAY_END, _limits(opencode_db=str(p)))
    assert len(out) == 1
    s = out[0]
    assert [t["x"] for t in s["turns"]] == ["today"]
    assert s["user_msgs"] == 1
    # KNOWN DEFECT: _finish() never assigns s["first"]/s["last"], so these stay
    # None for opencode sessions while codex sessions do carry them.
    assert s["first"] == dt.datetime(2026, 10, 7, 9, 0, tzinfo=TZ8)
    assert s["last"] == dt.datetime(2026, 10, 7, 9, 0, tzinfo=TZ8)
    assert s["span"] == 0


def test_opencode_session_with_no_messages_today_is_excluded(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_cli, "self_run_ids", lambda: set())
    monkeypatch.setattr(llm_cli, "self_run_marker", lambda: "ai-work-journal report")
    p = _oc_db(tmp_path)
    _add_session(p, "ses_quiet", title="idle",
                 created=dt.datetime(2026, 10, 6, 9, 0, tzinfo=TZ8))
    _add_message(p, "m_q", "ses_quiet", dt.datetime(2026, 10, 6, 9, 0, tzinfo=TZ8), "user")
    _add_part(p, "p_q", "m_q", dt.datetime(2026, 10, 6, 9, 0, tzinfo=TZ8),
              {"type": "text", "text": "old"})
    assert agent_sources.collect_opencode(DAY_START, DAY_END, _limits(opencode_db=str(p))) == []


def test_opencode_parent_and_fork_are_distinguished(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_cli, "self_run_ids", lambda: set())
    monkeypatch.setattr(llm_cli, "self_run_marker", lambda: "ai-work-journal report")
    p = _oc_db(tmp_path)
    _add_session(p, "ses_root", title="root")
    _add_session(p, "ses_child", parent="ses_root", title="child")
    for sid, mid, txt in (("ses_root", "m_r", "root work"), ("ses_child", "m_c", "child work")):
        ts = dt.datetime(2026, 10, 7, 9, 0, tzinfo=TZ8)
        _add_message(p, mid, sid, ts, "user")
        _add_part(p, "p_" + mid, mid, ts, {"type": "text", "text": txt})

    by_id = {s["native_id"]: s for s in
             agent_sources.collect_opencode(DAY_START, DAY_END, _limits(opencode_db=str(p)))}
    assert by_id["ses_root"]["native_parent_id"] is None
    assert by_id["ses_root"]["native_kind"] == "root"
    assert by_id["ses_child"]["native_parent_id"] == "ses_root"
    assert by_id["ses_child"]["native_kind"] == "child"


def test_opencode_journal_report_runs_are_excluded(tmp_path, monkeypatch):
    """Optional ingest must not report the journal's own report run back to itself."""
    monkeypatch.setattr(llm_cli, "self_run_ids", lambda: set())
    monkeypatch.setattr(llm_cli, "self_run_marker", lambda: "ai-work-journal report")
    p = _oc_db(tmp_path)
    _add_session(p, "ses_self", title="ai-work-journal report abc123")
    _add_session(p, "ses_real", title="real work")
    for sid, mid, txt in (("ses_self", "m_s", "self"), ("ses_real", "m_x", "real")):
        ts = dt.datetime(2026, 10, 7, 9, 0, tzinfo=TZ8)
        _add_message(p, mid, sid, ts, "user")
        _add_part(p, "p_" + mid, mid, ts, {"type": "text", "text": txt})

    out = agent_sources.collect_opencode(DAY_START, DAY_END, _limits(opencode_db=str(p)))
    assert [s["native_id"] for s in out] == ["ses_real"]


def test_opencode_self_run_id_from_the_registry_is_excluded(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_cli, "self_run_ids", lambda: {"ses_self"})
    monkeypatch.setattr(llm_cli, "self_run_marker", lambda: "ai-work-journal report")
    p = _oc_db(tmp_path)
    _add_session(p, "ses_self", title="anything")
    _add_message(p, "m_s", "ses_self", dt.datetime(2026, 10, 7, 9, 0, tzinfo=TZ8), "user")
    _add_part(p, "p_s", "m_s", dt.datetime(2026, 10, 7, 9, 0, tzinfo=TZ8),
              {"type": "text", "text": "self"})
    assert agent_sources.collect_opencode(DAY_START, DAY_END, _limits(opencode_db=str(p))) == []


def test_opencode_counts_tools_files_and_tool_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_cli, "self_run_ids", lambda: set())
    monkeypatch.setattr(llm_cli, "self_run_marker", lambda: "ai-work-journal report")
    p = _oc_db(tmp_path)
    _add_session(p, "ses_t", title="tooling")
    _add_message(p, "m_t", "ses_t", dt.datetime(2026, 10, 7, 9, 0, tzinfo=TZ8), "assistant")
    _add_part(p, "p_ok", "m_t", dt.datetime(2026, 10, 7, 9, 0, tzinfo=TZ8),
              {"type": "tool", "tool": "read",
               "state": {"status": "completed", "input": {"filePath": "/repo/app/a.py"},
                         "output": "file body"}})
    _add_part(p, "p_bad", "m_t", dt.datetime(2026, 10, 7, 9, 1, tzinfo=TZ8),
              {"type": "tool", "tool": "bash",
               "state": {"status": "error", "input": {"command": "false"},
                         "output": "command not found"}})

    s = agent_sources.collect_opencode(DAY_START, DAY_END, _limits(opencode_db=str(p)))[0]
    assert s["tools"] == {"read": 1, "bash": 1}
    assert s["tool_errors"] == 1
    assert s["files"] == ["/repo/app/a.py"]
    assert any(t.get("success") is True for t in s["turns"])
    assert any(t.get("success") is False for t in s["turns"])


def test_opencode_tool_output_is_recorded_with_its_outcome(tmp_path, monkeypatch):
    """A tool part carries its own success flag, so a failure is visible in the
    turn list and not only in the tool_errors counter."""
    monkeypatch.setattr(llm_cli, "self_run_ids", lambda: set())
    monkeypatch.setattr(llm_cli, "self_run_marker", lambda: "ai-work-journal report")
    p = _oc_db(tmp_path)
    _add_session(p, "ses_s", title="tool success flag")
    _add_message(p, "m_s", "ses_s", dt.datetime(2026, 10, 7, 9, 0, tzinfo=TZ8), "assistant")
    _add_part(p, "p_s", "m_s", dt.datetime(2026, 10, 7, 9, 0, tzinfo=TZ8),
              {"type": "tool", "tool": "bash",
               "state": {"status": "error", "input": {}, "output": "boom"}})
    s = agent_sources.collect_opencode(DAY_START, DAY_END, _limits(opencode_db=str(p)))[0]
    tool_turns = [t for t in s["turns"] if t.get("role") == "tool"]
    assert len(tool_turns) == 1
    assert tool_turns[0]["success"] is False
    assert tool_turns[0]["typed"] is False


def test_opencode_source_is_skipped_when_not_configured(tmp_path):
    assert agent_sources.collect_opencode(DAY_START, DAY_END, _limits(opencode_db="")) == []
    assert agent_sources.collect_opencode(DAY_START, DAY_END,
                                          _limits(opencode_db=str(tmp_path / "absent.db"))) == []


def test_opencode_opens_the_database_read_only_and_repeats_identically(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_cli, "self_run_ids", lambda: set())
    monkeypatch.setattr(llm_cli, "self_run_marker", lambda: "ai-work-journal report")
    p = _oc_db(tmp_path)
    _add_session(p, "ses_ro", title="read only")
    _add_message(p, "m_ro", "ses_ro", dt.datetime(2026, 10, 7, 9, 0, tzinfo=TZ8), "user")
    _add_part(p, "p_ro", "m_ro", dt.datetime(2026, 10, 7, 9, 0, tzinfo=TZ8),
              {"type": "text", "text": "hello"})

    before = hashlib.sha256(p.read_bytes()).hexdigest()
    con = agent_sources._opencode_ro(str(p))
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO session VALUES('x',NULL,'/','',1,1)")
    con.close()

    a = agent_sources.collect_opencode(DAY_START, DAY_END, _limits(opencode_db=str(p)))
    b = agent_sources.collect_opencode(DAY_START, DAY_END, _limits(opencode_db=str(p)))

    assert a == b, "re-reading the same database must be idempotent"
    assert hashlib.sha256(p.read_bytes()).hexdigest() == before


def test_opencode_reader_survives_a_broken_database(tmp_path):
    p = tmp_path / "broken.db"
    p.write_bytes(b"this is not a sqlite database at all")
    assert agent_sources.collect_opencode(DAY_START, DAY_END, _limits(opencode_db=str(p))) == []


def test_collect_runs_every_source_and_survives_one_being_broken(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_cli, "self_run_ids", lambda: set())
    monkeypatch.setattr(llm_cli, "self_run_marker", lambda: "ai-work-journal report")
    p = _oc_db(tmp_path)
    _add_session(p, "ses_c", title="combined")
    _add_message(p, "m_c", "ses_c", dt.datetime(2026, 10, 7, 9, 0, tzinfo=TZ8), "user")
    _add_part(p, "p_c", "m_c", dt.datetime(2026, 10, 7, 9, 0, tzinfo=TZ8),
              {"type": "text", "text": "combined"})

    def _explode(*a, **k):
        raise RuntimeError("codex source is broken")

    monkeypatch.setattr(agent_sources, "collect_codex", _explode)
    out = agent_sources.collect(DAY_START, DAY_END, _limits(opencode_db=str(p)))
    assert [s["agent"] for s in out] == ["opencode"]

def test_file_cap_keeps_old_but_currently_active_codex_session(tmp_path):
    root = tmp_path / 'sessions'
    root.mkdir()
    active = root / '000-old-but-active.jsonl'
    inactive = root / 'zzz-new-filename-but-idle.jsonl'
    _write_rollout(active, [_codex_meta('active'), _codex_line('2026-10-07T10:00:00+08:00', 'response_item', {'type':'message','role':'user','content':'new work'})], DAY_START.timestamp()+7200)
    _write_rollout(inactive, [_codex_meta('idle'), _codex_line('2026-10-06T10:00:00+08:00', 'response_item', {'type':'message','role':'user','content':'old work'})], DAY_START.timestamp()-3600)
    rows = agent_sources.collect_codex(DAY_START, DAY_END, _limits(codex_dir=str(root), max_files=1))
    assert [r['native_id'] for r in rows] == ['active']


def test_missing_optional_lark_cli_does_not_abort_collection(monkeypatch):
    import collect_feishu
    def unavailable(*args, **kwargs):
        raise FileNotFoundError('optional CLI absent')
    monkeypatch.setattr(collect_feishu.subprocess, 'run', unavailable)
    assert collect_feishu.run_lark(['im', '+chat-list']) is None
    assert collect_feishu.collect(DAY_START, DAY_END)['error']


def test_long_native_output_keeps_final_result_and_marks_omission():
    text = 'start of diagnostic\n' + 'long intermediate output ' * 400 + '\n178 passed; recovered earlier failure'
    snippet = agent_sources._clip(text, 500)
    assert 'start of diagnostic' in snippet
    assert '178 passed; recovered earlier failure' in snippet
    assert '中段省略' in snippet
    assert len(snippet) <= 500


def test_demo_optional_native_sources_stay_disabled(monkeypatch):
    monkeypatch.setenv('JOURNAL_DEMO', '/synthetic/demo')
    import osutil
    monkeypatch.setattr(osutil, 'load_config', lambda *args: {'codex_dir':'/synthetic/private', 'opencode_db':'/synthetic/private.db'})
    opts = agent_sources.cfg()
    assert opts['codex_dir'] == opts['opencode_db'] == ''
    assert agent_sources.collect(DAY_START, DAY_END) == []


def test_explicit_model_disable_never_invokes_report_backend(monkeypatch):
    monkeypatch.setenv('JOURNAL_LLM_DISABLE', '1')
    monkeypatch.setattr(llm_cli, '_run_opencode', lambda *args: pytest.fail('disabled model was called'))
    monkeypatch.setattr(llm_cli, '_run_pi', lambda *args: pytest.fail('disabled model was called'))
    with pytest.raises(llm_cli.LlmCliError, match='disabled'):
        llm_cli.generate('synthetic prompt')
