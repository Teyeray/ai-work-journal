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

import llm_cli  # noqa: E402
import osutil  # noqa: E402

CFG = osutil.load_config()
REPORTS = BASE / "reports"
REPORTS.mkdir(exist_ok=True)
LOCAL_TZ = dt.datetime.now().astimezone().tzinfo

# template lives in templates/daily.md; edit that file to change the report format
PROMPT = (BASE / "templates" / "daily.md").read_text(encoding="utf-8")



def run_pi(prompt, attachment, timeout=900):
    """Backwards-compatible name; the backend is whichever one is configured
    (pi by default, opencode when config.local.json says so). See llm_cli."""
    return llm_cli.generate(prompt, attachment, timeout=timeout)


# --- schedule (set from the web dashboard's 日报 page) ---------------------
# Honoured only when the scheduler starts us (--scheduled, see osutil.JOBS);
# a manual `journal daily` always runs.
SCHEDULE_FILE = osutil.config_dir() / "schedule.json"


def load_schedule() -> dict:
    import json as _json
    try:
        return {"daily_weekdays": [1, 2, 3, 4, 5, 6, 7], "weekly_enabled": True, "feishu_push": True,
                **_json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return {"daily_weekdays": [1, 2, 3, 4, 5, 6, 7], "weekly_enabled": True, "feishu_push": True}


def started_by_timer() -> bool:
    return osutil.started_by_scheduler()


def main():
    args = osutil.script_args()
    d = parse_date(args[0] if args else None)
    if started_by_timer() and d.isoweekday() not in load_schedule()["daily_weekdays"]:
        print(f"skipped by schedule: {d} is weekday {d.isoweekday()}")
        return
    digest = DATA / f"{d.isoformat()}.digest.md"
    # always re-collect: manual runs earlier in the day must not pin stale data
    r = subprocess.run([sys.executable, str(BASE / "collect.py"), d.isoformat()],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(r.stdout.strip() or r.stderr.strip())
    if r.returncode != 0:
        print("collection failed; previous reports and digests remain unchanged")
        sys.exit(1)
    if not digest.exists():
        print(f"no digest for {d}, abort")
        sys.exit(1)
    # best-effort LLM analysis of the day's conversations (dashboard section)
    try:
        an = subprocess.run([sys.executable, str(BASE / "analyze.py"), d.isoformat()],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1200)
        if an.stdout.strip():
            print(an.stdout.strip())
    except Exception as e:
        print(f"analysis skipped: {e}")
    report_path = REPORTS / f"{d.isoformat()}.md"
    prompt = PROMPT.replace("{date}", d.isoformat())
    # a previous *successful* report is never replaced by a fallback digest
    keep = report_path.exists() and "LLM 生成失败" not in report_path.read_text(encoding="utf-8")
    try:
        out = run_pi(prompt, digest)
    except Exception as e:
        if keep:
            print(f"report LLM failed: {e}\nkept the previous report: {report_path}")
            out = None
        else:
            print(f"report LLM failed: {e}\nfalling back to raw digest")
            out = f"> 警告: LLM 生成失败,以下为原始数据。\n\n" + digest.read_text(encoding="utf-8")
    if out is not None:
        header = (f"<!-- generated {dt.datetime.now(LOCAL_TZ).isoformat()} "
                  f"by ai-work-journal -->\n\n")
        # write-then-rename: a crash mid-write never truncates a good report
        tmp = report_path.with_suffix(".md.tmp")
        tmp.write_text(header + out + "\n", encoding="utf-8")
        tmp.replace(report_path)
        print(f"report written: {report_path}")
    # best-effort feishu push
    if load_schedule()["feishu_push"]:
        fe = subprocess.run([sys.executable, str(BASE / "feishu.py"), str(report_path)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if fe.stdout.strip():
            print(fe.stdout.strip())
    else:
        print("feishu push disabled in schedule")
    # best-effort dashboard refresh so the day lands in the charts
    try:
        db = subprocess.run([sys.executable, str(BASE / "dashboard.py")],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        if db.stdout.strip():
            print(db.stdout.strip())
    except Exception:
        pass


if __name__ == "__main__":
    main()
