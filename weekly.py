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

请严格按下面的固定模板汇总成中文周报,输出 Markdown,不要输出其他内容:

# 工作周报 {label}

## 本周概览
4-6 句。

## 项目进展
按项目组织全部内容,每个项目一个三级标题,顺序按本周投入降序。

### {项目名}
- **进展**: 本周推进,按时间线归纳,合并同一天重复项。
- **代码产出**: 提交数量与主题。
- **AI 协作**: 会话数与值得注意的模式。
- **风险/待办**: 有则写。

### 其他
无法归入项目的活动,合并简述。

## 综合统计
- AI 会话总量与分布。
- 提交总量。
简短,2-4 行。

## 问题与风险
跨项目的问题与风险。

## 下周建议
3-5 条。

硬性规则:
1. 一切内容归入项目小节,不按数据来源或材料类型分节。
2. 项目名与日报保持一致,全篇同一写法。
3. 补充材料(历史汇报、飞书文件等)归入所属项目;该项目本周无日报记录时,新开项目小节写入。
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
