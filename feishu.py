#!/usr/bin/env python3
"""Push a markdown report to Feishu as a docx document.

Skips silently (exit 0) when lark-cli is not authorized, so timers stay green.

Usage: feishu.py <report.md>
"""
import json
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import osutil  # noqa: E402

CFG = osutil.load_config()
LARK = CFG["lark_bin"]
MAP = BASE / "data" / "feishu-map.json"


def auth_ok():
    try:
        r = subprocess.run([LARK, "auth", "status"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=20)
        d = json.loads(r.stdout)
    except Exception:
        return False
    if d.get("ok"):
        return True
    ids = d.get("identities") or {}
    user = ids.get("user") or {}
    bot = ids.get("bot") or {}
    return bool(user.get("available") or bot.get("available"))


def main():
    if len(sys.argv) < 2:
        print("usage: feishu.py <report.md>")
        sys.exit(2)
    f = Path(sys.argv[1]).resolve()
    if not f.exists():
        print(f"not found: {f}")
        sys.exit(2)
    if not auth_ok():
        print("feishu: lark-cli 未授权,跳过推送。"
              "授权方法: 终端运行 lark-cli config init --new 并按提示打开浏览器。")
        sys.exit(0)
    name = f"AI 工作日志 {f.stem}"
    # replace semantics: delete the previous doc for the same report file
    maps = json.loads(MAP.read_text(encoding="utf-8")) if MAP.exists() else {}
    old = (maps.get(f.name) or {}).get("token")
    if old:
        subprocess.run([LARK, "drive", "+delete", "--file-token", old,
                        "--type", "docx", "--yes"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    r = subprocess.run([LARK, "drive", "+import", "--file", str(f),
                        "--type", "docx", "--name", name],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        d = None
    url = ""
    if isinstance(d, dict) and d.get("ok"):
        url = (d.get("data") or {}).get("url") or ""
        token = (d.get("data") or {}).get("token") or url.rsplit("/", 1)[-1]
        maps[f.name] = {"url": url, "token": token,
                        "pushed_at": __import__("datetime").datetime.now().isoformat()}
        MAP.write_text(json.dumps(maps, ensure_ascii=False, indent=1))
        print(f"feishu: pushed {f.name} -> {url}")
    else:
        print(f"feishu: import failed: {(r.stdout or r.stderr)[-300:]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
