#!/usr/bin/env python3
"""Generate the weekly work report from daily reports.

Usage: weekly.py [YYYY-MM-DD]   (date inside the target ISO week, default today)
"""
import datetime as dt
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import osutil  # noqa: E402

CFG = osutil.load_config()
REPORTS = BASE / "reports"
WEEKLY = REPORTS / "weekly"
WEEKLY.mkdir(parents=True, exist_ok=True)

from daily import run_pi  # noqa: E402

PROMPT = (BASE / "templates" / "weekly.md").read_text(encoding="utf-8")


# --- schedule (set from the web dashboard's 日报 page) ---------------------
# Honoured only when the scheduler starts us (--scheduled, see osutil.JOBS);
# a manual `journal weekly` always runs.
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
    ref = dt.date.fromisoformat(args[0]) if args else dt.date.today()
    if started_by_timer() and not load_schedule()["weekly_enabled"]:
        print("skipped by schedule: weekly report disabled")
        return
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
    if load_schedule()["feishu_push"]:
        fe = subprocess.run([sys.executable, str(BASE / "feishu.py"), str(out_path)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if fe.stdout.strip():
            print(fe.stdout.strip())
    else:
        print("feishu push disabled in schedule")


if __name__ == "__main__":
    main()
