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

PROMPT = """你是我的工作日志助手。我是 AI 工程师。下面附的是本周每天的工作日报(Monday 到 Sunday,部分日期可能缺失)。

请严格按下面的通用工作周报模板(百度文库/飞书模板中心标准结构)汇总成中文周报,输出 Markdown,不要输出其他内容:

# 工作周报 {label}

## 一、本周工作总结
按项目分组,投入降序。每个项目分三档描述:
- **已完成**: 本周完成的成果。
- **进行中**: 未收尾的工作。
- **未完成**: 计划内但没做的。
无法归入项目的活动合并到末尾“其他”。

## 二、量化产出
- 提交数、合并 PR 数、AI 会话数及分布。
简短,3-5 行。

## 三、问题与风险
跨项目的问题与风险,含需协助事项。没有则写“无”。

## 四、下周计划
按项目列 3-5 条,标优先级。从本周进行中/未完成工作和日报线索推断;无可靠线索则写“(待人工补充)”,不要编造。

硬性规则:
1. 一切内容归入项目分组,不按数据来源或材料类型分节。
2. 项目名与日报保持一致,全篇同一写法。
3. 补充材料(历史汇报、飞书文件等)归入所属项目;该项目本周无日报记录时,新开项目分组写入。
4. 只依据附件内容,不编造。"""


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
