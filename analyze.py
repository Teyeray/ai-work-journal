#!/usr/bin/env python3
"""Per-day LLM analysis of agent conversations.

Reads data/<date>.json, sends each session's turns/tools/files to pi
headless, and stores the result back into the day JSON as d["analysis"]:
  {"sessions": [{"file", "sum", "out"}], "stats": str, "advice": [str]}

Skips the LLM call when analysis already exists (pass --force to redo).
Usage: analyze.py [YYYY-MM-DD] [--force]
"""
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from collect import DATA, parse_date, CFG  # noqa: E402
import llm_cli  # noqa: E402
import osutil  # noqa: E402

LOCAL_TZ = dt.datetime.now().astimezone().tzinfo


def run_pi(prompt, attachment, timeout=900):
    """Backwards-compatible name; the backend is whichever one is configured
    (pi by default, opencode when config.local.json says so). See llm_cli."""
    return llm_cli.generate(prompt, attachment, timeout=timeout)


PROMPT = """你是工作效能分析助手。附件是某天({date})的用户与 AI agent 的对话记录摘要。
每个会话含: 编号、agent、项目、消息数、任务首句、标明来源角色的对话/工具记录、工具统计、触及文件。用户侧通道不证明本人键入或编辑，Agent自述不证明独立验收。

请完成:
1. 给每个会话写"sum": 一句话概括,≤30字,说清实际做了什么(不要复述任务首句)。
2. 会话有明确产出/结果时写"out",≤30字;没有就省略。
3. 写"stats": 当日对话内容统计,2-3句。主题分布(时间花在哪些事)、协作模式(人机分工)、工具/token 特征。
4. 写"advice": 0-4 条改进意见,针对该用户使用 agent 的方式,具体可执行,每条≤50字。
   只提有依据的意见;没有明确改进空间就返回空数组,禁止编造。

严格只输出 JSON 对象,不要 markdown 围栏,不要多余文字:
{{"sessions":[{{"i":1,"sum":"...","out":"..."}}],"stats":"...","advice":["..."]}}
sessions 必须覆盖全部会话,i 用附件里的编号。"""


def session_text(i, s):
    lines = [f"#{i} {s['agent']} · 项目: {s.get('cwd', '?')} · 消息 {s.get('user_msgs', 0)}/"
             f"{s.get('assistant_msgs', 0)} · 时长 {round(s.get('span', 0) / 60)}分钟"]
    task = (s.get("task") or "").replace("\n", " ")
    if task:
        lines.append(f"  任务首句: {task[:120]}")
    turns = s.get("turns") or []
    if turns:
        for t in turns[-12:]:
            lines.append(f"  {t['t']} [{t.get('role', 'user_channel')}] {t['x']}")
    else:
        lines.append("  (自动会话, 无人工输入)")
    tools = sorted((s.get("tools") or {}).items(), key=lambda kv: -kv[1])
    if tools:
        lines.append("  工具: " + ", ".join(f"{k}×{v}" for k, v in tools[:6]))
    files = (s.get("files") or [])[:8]
    if files:
        lines.append("  文件: " + ", ".join(files))
    return "\n".join(lines)


def parse_json(text):
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in output")
    return json.loads(m.group(0))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    force = "--force" in sys.argv
    d = parse_date(args[0] if args else None)
    path = DATA / f"{d.isoformat()}.json"
    if not path.exists():
        print(f"no data for {d}, run collect.py first")
        sys.exit(1)
    day = json.loads(path.read_text(encoding="utf-8"))
    if day.get("analysis") and not force:
        print(f"analysis exists for {d}, skip (--force to redo)")
        return
    sessions = day.get("agent_sessions") or []
    if not sessions:
        day["analysis"] = {"sessions": [], "stats": "当日无 agent 会话。", "advice": [],
                           "generated_at": dt.datetime.now(LOCAL_TZ).isoformat()}
        path.write_text(json.dumps(day, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"no sessions for {d}, marked done")
        return

    body = "\n\n".join(session_text(i, s) for i, s in enumerate(sessions, 1))
    att = DATA / f"{d.isoformat()}.analysis-input.txt"
    att.write_text(body, encoding="utf-8")
    try:
        out = run_pi(PROMPT.replace("{date}", d.isoformat()), att)
        result = parse_json(out)
    except Exception as e:
        print(f"analysis failed for {d}: {e}")
        sys.exit(1)
    by_i = {s.get("i"): s for s in result.get("sessions", []) if isinstance(s, dict)}
    mapped = []
    for i, s in enumerate(sessions, 1):
        r = by_i.get(i) or {}
        mapped.append({"file": s["file"], "sum": str(r.get("sum", ""))[:60],
                       "out": str(r.get("out", ""))[:60]})
    day["analysis"] = {
        "sessions": mapped,
        "stats": str(result.get("stats", ""))[:300],
        "advice": [str(a)[:80] for a in (result.get("advice") or [])[:6]],
        "generated_at": dt.datetime.now(LOCAL_TZ).isoformat(),
    }
    path.write_text(json.dumps(day, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"analyzed {d}: {len(mapped)} sessions, {len(day['analysis']['advice'])} advice items")
    att.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
