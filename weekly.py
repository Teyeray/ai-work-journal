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

PROMPT = (BASE / "templates" / "weekly.md").read_text(encoding="utf-8")


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
