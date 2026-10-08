"""Session review: LLM (pi headless, gated) scores a session against the rubric.

Rubric (DESIGN_SPEC §5): prompt goal/context/constraints/acceptance 25 each;
execution planning 15 / tools 20 / error recovery 20 / verification 30 /
efficiency 15. Stored keyed by (session_id, analyzer_version,
input_fingerprint) so unchanged sessions return the cached review.
"""
import datetime as dt
from pathlib import Path
import hashlib
import json
import os
import re
import subprocess
import tempfile

from . import config, db
from .timeutil import now_utc

ANALYZER_VERSION = "v1"
TIMEOUT = 900

PROMPT = """你是代码 Agent 会话的质量评审员。以下是一个 Agent 会话的完整记录(已脱敏)。
按规则评分并输出严格 JSON。

评分维度(0 到满分):
- prompt_goal 目标明确 (0-25):用户是否说清要做什么
- prompt_context 上下文充分 (0-25):必要背景/文件/环境是否给出
- prompt_constraints 约束清晰 (0-25):边界条件、非目标、风格约束
- prompt_acceptance 验收标准 (0-25):完成判据是否可检验
- exec_planning 规划 (0-15):Agent 是否先分解任务
- exec_tools 工具使用 (0-20):工具选择与参数是否恰当
- exec_error_recovery 错误恢复 (0-20):报错后的诊断与修复质量
- exec_verification 验证 (0-30):是否实际运行/测试/核对结果
- exec_efficiency 效率 (0-15):有无明显浪费(重复尝试、过度输出)

仅输出一个 JSON 对象,不要 markdown 代码块:
{"prompt_goal":n,"prompt_context":n,"prompt_constraints":n,"prompt_acceptance":n,
 "exec_planning":n,"exec_tools":n,"exec_error_recovery":n,"exec_verification":n,
 "exec_efficiency":n,
 "summary":"3句话以内的会话内容与质量概述",
 "suggestions":["2到4条具体可执行的改进建议,每条<=80字"],
 "evidence_refs":[证据事件id列表,最多10个,数字]}

会话记录:
"""


def _redact(text: str) -> str:
    for prefix in ("sk-ant", "ghp_", "gho_", "sk_", "AKIA", "AIza", "xoxb-"):
        text = re.sub(prefix + r"[A-Za-z0-9_\-]{8,}", prefix + "***", text)
    return text


def _session_text(sid: int) -> tuple[str, str]:
    """Compact transcript + input fingerprint (content hash)."""
    evs = db.q("SELECT e.id, e.event_type, e.occurred_at, e.payload "
               "FROM session_event_links l JOIN source_events e ON e.id = l.event_id "
               "WHERE l.session_id = ? ORDER BY l.ordinal", (sid,))
    parts = []
    h = hashlib.sha256()
    for e in evs:
        p = json.loads(e["payload"])
        if e["event_type"] == "msg_user":
            t = _redact((p.get("text") or "")[:1200])
            parts.append(f"[U {e['occurred_at'][11:16]}] {t}")
        elif e["event_type"] == "msg_assistant":
            tools = [x.get("name", "?") for x in (p.get("parts") or [])
                     if x.get("kind") == "tool_call"]
            line = f"[A {e['occurred_at'][11:16]}]"
            if tools:
                line += f" 工具: {','.join(tools[:8])}"
            errs = [x for x in (p.get("parts") or []) if x.get("is_error")]
            if errs:
                line += f" (!{len(errs)}个错误结果)"
            parts.append(line)
        elif e["event_type"] == "session_meta":
            parts.append(f"[META] {(p.get('cwd') or '')[-60:]}")
        h.update(f"{e['id']}|".encode())
        h.update((e["payload"] or "")[:200].encode())
    return "\n".join(parts)[:60000], h.hexdigest()[:16]


def get_review(sid: int) -> dict:
    row = db.q1("SELECT * FROM session_reviews WHERE session_id=? "
                "AND analyzer_version=? ORDER BY id DESC", (sid, ANALYZER_VERSION))
    if row and row["status"] == "analyzed":
        return {"status": "analyzed", **json.loads(row["review_json"])}
    if row:
        return {"status": row["status"]}
    return {"status": "unreviewed"}


def run_review(sid: int) -> dict:
    session = db.q1("SELECT * FROM agent_sessions WHERE id=?", (sid,))
    if not session:
        return {"status": "error", "message": "no such session"}
    text, fingerprint = _session_text(sid)
    if not text.strip():
        return {"status": "error", "message": "empty session"}

    if not config.LLM_ENABLED:
        return {"status": "llm_disabled",
                "message": "LLM 评审未启用 (webapp/config.json llm.enabled)"}

    # cached by fingerprint?
    row = db.q1("SELECT * FROM session_reviews WHERE session_id=? AND "
                "analyzer_version=? AND input_fingerprint=?", (sid, ANALYZER_VERSION, fingerprint))
    if row and row["status"] == "analyzed":
        return {"status": "analyzed", **json.loads(row["review_json"])}

    _record(sid, fingerprint, "pending", None)
    try:
        out = _call_pi(text)
        scores = _parse(out)
        if not scores:
            _record(sid, fingerprint, "failed", None, error="unparseable LLM output")
            return {"status": "failed", "message": "LLM 输出无法解析"}
        _record(sid, fingerprint, "analyzed", scores)
        return {"status": "analyzed", **scores}
    except Exception as ex:
        _record(sid, fingerprint, "failed", None, error=ex)
        return {"status": "failed", "message": str(ex)[:200]}


def _record(sid, fp, status, scores, error=None):
    db.ex("DELETE FROM session_reviews WHERE session_id=? AND analyzer_version=? "
          "AND input_fingerprint=?", (sid, ANALYZER_VERSION, fp))
    db.ex("INSERT INTO session_reviews(session_id,analyzer_version,input_fingerprint,"
          "status,review_json,error,created_at) VALUES(?,?,?,?,?,?,?)",
          (sid, ANALYZER_VERSION, fp, status,
           json.dumps(scores, ensure_ascii=False) if scores else None,
           str(error or "")[:300], now_utc().isoformat()))


def _call_pi(text: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(PROMPT + text)
        path = f.name
    env = {"PATH": f"{Path.home() / '.local/bin'}:/usr/local/bin:/usr/bin:/bin",
           "HOME": str(Path.home()), "LANG": "C.UTF-8", "TERM": "dumb"}
    try:
        r = subprocess.run(
            [str(config.PI_BIN), "-p", "--no-tools", "--no-session", "--mode", "text",
             "评审文件中的会话,严格按给定 schema 输出一个 JSON 对象,不要其他内容。", "@" + path],
            capture_output=True, text=True, timeout=TIMEOUT, env=env, cwd="/tmp",
            stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            raise RuntimeError(f"pi exited {r.returncode}: {r.stderr[-300:]}")
        return r.stdout
    finally:
        os.unlink(path)


def _parse(raw: str) -> dict | None:
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    caps = {"prompt_goal": 25, "prompt_context": 25, "prompt_constraints": 25,
            "prompt_acceptance": 25, "exec_planning": 15, "exec_tools": 20,
            "exec_error_recovery": 20, "exec_verification": 30, "exec_efficiency": 15}
    out = {}
    for k, cap in caps.items():
        v = d.get(k)
        if isinstance(v, (int, float)) and 0 <= v <= cap:
            out[k] = int(v)
    if len(out) != len(caps):
        return None
    out["summary"] = str(d.get("summary") or "")[:300]
    sg = d.get("suggestions")
    out["suggestions"] = [str(x)[:80] for x in sg[:4]] if isinstance(sg, list) else []
    ev = d.get("evidence_refs")
    out["evidence_refs"] = [int(x) for x in ev[:10]
                            if isinstance(x, (int, str)) and str(x).isdigit()] if isinstance(ev, list) else []
    return out
