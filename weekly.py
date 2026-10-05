#!/usr/bin/env python3
"""Generate the weekly work report from daily reports.

Usage: weekly.py [YYYY-MM-DD]   (date inside the target ISO week, default today)
"""
import datetime as dt
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
CFG = __import__("json").loads((BASE / "config.json").read_text(encoding="utf-8"))
REPORTS = BASE / "reports"
WEEKLY = REPORTS / "weekly"
WEEKLY.mkdir(parents=True, exist_ok=True)

from daily import run_pi  # noqa: E402

PROMPT = """你是我的工作日志助手。我是 AI 工程师。下面附的是我本周每天的工作日报(Monday 到 Sunday,部分日期可能缺失)。

请汇总成一份中文周报,输出 Markdown,不要输出其他内容。结构:

# 工作周报 {label}

## 本周概览
4-6 句话总结本周整体情况。

## 项目进展
按项目归纳本周的推进,合并同一天多次出现的工作。

## 代码产出
本周 git 提交的汇总(数量、主题)。

## AI 协作统计
本周 agent 使用总量(会话数、涉及项目),协作中值得注意的模式。

## 问题与风险
本周遇到的阻碍、未完成事项。

## 下周建议
基于本周数据给出 2-4 条建议。

规则:只依据日报内容,不要编造。"""


def main():
    ref = dt.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else dt.date.today()
    iso = ref.isocalendar()
    monday = dt.date.fromisocalendar(iso[0], iso[1], 1)
    days = [monday + dt.timedelta(days=i) for i in range(7)]
    parts = []
    have = []
    for d in days:
        p = REPORTS / f"{d.isoformat()}.md"
        if p.exists():
            have.append(d)
            parts.append(f"==== {d.isoformat()} ({d.strftime('%a')}) ====\n\n" +
                         p.read_text(encoding="utf-8"))
    if not have:
        print("no daily reports this week, abort")
        sys.exit(1)
    label = f"{iso[0]}-W{iso[1]:02d}"
    print(f"weekly {label}: {len(have)}/7 daily reports found")
    combined = BASE / "data" / f"{label}.week-input.md"
    combined.parent.mkdir(exist_ok=True)
    combined.write_text("\n\n".join(parts), encoding="utf-8")
    out_path = WEEKLY / f"{label}.md"
    try:
        out = run_pi(PROMPT.replace("{label}", label), combined, timeout=1200)
    except Exception as e:
        print(f"pi failed: {e}\nfalling back to concatenated dailies")
        out = f"> 警告: LLM 生成失败,以下为日报拼接。\n\n" + combined.read_text(encoding="utf-8")
    out_path.write_text(out + "\n", encoding="utf-8")
    print(f"weekly report written: {out_path}")
    fe = subprocess.run([sys.executable, str(BASE / "feishu.py"), str(out_path)],
                        capture_output=True, text=True)
    if fe.stdout.strip():
        print(fe.stdout.strip())


if __name__ == "__main__":
    main()
