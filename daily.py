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

# template lives in templates/daily.md; edit that file to change the report format
PROMPT = (BASE / "templates" / "daily.md").read_text(encoding="utf-8")



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
    # best-effort LLM analysis of the day's conversations (dashboard section)
    try:
        an = subprocess.run([sys.executable, str(BASE / "analyze.py"), d.isoformat()],
                            capture_output=True, text=True, timeout=1200)
        if an.stdout.strip():
            print(an.stdout.strip())
    except Exception as e:
        print(f"analysis skipped: {e}")
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
    # best-effort dashboard refresh so the day lands in the charts
    try:
        db = subprocess.run([sys.executable, str(BASE / "dashboard.py")],
                            capture_output=True, text=True, timeout=120)
        if db.stdout.strip():
            print(db.stdout.strip())
    except Exception:
        pass


if __name__ == "__main__":
    main()
