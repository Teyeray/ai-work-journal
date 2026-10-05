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

PROMPT = """你是我的工作日志助手。我是 AI 工程师。下面附了一份自动采集的原始活动数据,包含四部分:
1. 电脑使用统计(应用与窗口标题时长)
2. AI 编程助手会话(claude-code / pi 的任务、工具调用、文件)
3. Git 提交
4. 屏幕 OCR 采样

请基于数据写今天的中文工作日报,输出 Markdown,不要输出其他内容。结构:

# 工作日报 {date}

## 今日概览
3-5 句话总结今天做了什么。

## 主要工作
按主题/项目分组描述。每条注明证据来源(会话/提交/浏览)。不要编造数据里没有的事。
没有数据的部分写"无数据"。

## AI 协作情况
今天用了哪些 agent,各跑了多少会话、大概什么任务。

## 其他活动
值得注意的浏览内容、长时间使用的非编程应用等。

规则:只依据数据;时间用 HH:MM;简明准确。"""


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
