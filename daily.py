#!/usr/bin/env python3
"""Generate the daily work report from collected data.

Flow:
  1. run collect.py for the date if digest missing
  2. call pi headless with the digest to write a Chinese daily report
  3. save to reports/<date>.md
  4. push to Feishu (best effort, skipped when lark-cli is not authorized)

Usage: daily.py [YYYY-MM-DD]
"""
import datetime as dt
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from collect import DATA, parse_date  # noqa: E402

CFG = __import__("json").loads((BASE / "config.json").read_text(encoding="utf-8"))
REPORTS = BASE / "reports"
REPORTS.mkdir(exist_ok=True)
LOCAL_TZ = dt.datetime.now().astimezone().tzinfo

PROMPT = """你是我的工作日志助手。我是 AI 工程师。下面附了一份自动采集的原始活动数据,包含:电脑使用统计、AI 编程助手会话、Git 提交、屏幕 OCR、飞书协作。

请严格按下面的固定模板写今天的中文日报,输出 Markdown,不要输出其他内容:

# 工作日报 {date}

## 概览
3-5 句,今天整体情况。

## 项目进展
按项目组织今天的全部工作,每个项目一个三级标题,顺序按投入时间降序。

### {项目名}
- **进展**: 今天在该项目做了什么,1-3 条。
- **产出**: 提交、文件、报告等,有则写。
- **协作**: 相关沟通与 agent 使用,有则写。

### 其他
无法归入项目的环境配置、工具调试、行政事务,合并简述。

## 数据备注
采集缺失或异常,一句话,无则省略整节。

硬性规则:
1. 一切内容归入项目小节,不按数据来源(会话/提交/飞书/屏幕)分节。
2. 项目名从数据推断(目录名、仓库名),全篇同一写法。
3. 飞书消息、文件、汇报材料归入所属项目,没有对应项目就新开项目小节。
4. 只依据数据,不编造;时间用 HH:MM;简明准确,不堆砌技术细节。"""


def run_pi(prompt, attachment, timeout=900):
    cmd = [CFG["pi_bin"], "-p", "--no-tools", "--no-session",
           "--mode", "text", f"@{attachment}", prompt]
    env = {"PATH": f"{Path.home() / '.local/bin'}:/usr/local/bin:/usr/bin:/bin",
           "HOME": str(Path.home()), "LANG": "C.UTF-8", "TERM": "dumb"}
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, cwd=str(BASE))
    if r.returncode != 0:
        raise RuntimeError(f"pi exited {r.returncode}: {r.stderr[-500:]}")
    return r.stdout.strip()


def main():
    d = parse_date(sys.argv[1] if len(sys.argv) > 1 else None)
    digest = DATA / f"{d.isoformat()}.digest.md"
    # always re-collect: manual runs earlier in the day must not pin stale data
    r = subprocess.run([sys.executable, str(BASE / "collect.py"), d.isoformat()],
                       capture_output=True, text=True)
    print(r.stdout.strip() or r.stderr.strip())
    if not digest.exists():
        print(f"no digest for {d}, abort")
        sys.exit(1)
    report_path = REPORTS / f"{d.isoformat()}.md"
    prompt = PROMPT.replace("{date}", d.isoformat())
    try:
        out = run_pi(prompt, digest)
    except Exception as e:
        print(f"pi failed: {e}\nfalling back to raw digest")
        out = f"> 警告: LLM 生成失败,以下为原始数据。\n\n" + digest.read_text(encoding="utf-8")
    header = (f"<!-- generated {dt.datetime.now(LOCAL_TZ).isoformat()} "
              f"by ai-work-journal -->\n\n")
    report_path.write_text(header + out + "\n", encoding="utf-8")
    print(f"report written: {report_path}")
    # best-effort feishu push
    fe = subprocess.run([sys.executable, str(BASE / "feishu.py"), str(report_path)],
                        capture_output=True, text=True)
    if fe.stdout.strip():
        print(fe.stdout.strip())


if __name__ == "__main__":
    main()
