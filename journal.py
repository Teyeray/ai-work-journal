#!/usr/bin/env python3
"""journal — the ai-work-journal command line (Linux · macOS · Windows).

    journal web                 start the dashboard and open it
    journal demo                try it on a synthetic two weeks (no collectors needed)
    journal daily [DATE]        daily report  (collect → analyze → LLM → Feishu)
    journal weekly [DATE]       weekly report
    journal collect [DATE]      collect one day's raw data only
    journal analyze [DATE]      LLM analysis of that day's agent sessions
    journal dashboard           static offline dashboard (reports/dashboard.html)
    journal feishu FILE         push one report to Feishu
    journal schedule install|remove|status
                                nightly daily/weekly jobs (systemd / launchd / Task Scheduler)
    journal doctor              check collectors, OCR, LLM keys and tools
    journal status              collectors, schedule, latest reports
    journal install             put `journal` on your PATH
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import osutil  # noqa: E402

WEBAPP = BASE / "webapp"


def _py(script: str, args):
    return subprocess.call([sys.executable, str(BASE / script), *args])


def _web_cfg() -> dict:
    return osutil.load_config(WEBAPP, "config.json")


def _web_url() -> str:
    c = _web_cfg()
    return f"http://{c.get('host', '127.0.0.1')}:{c.get('port', 8799)}"


def _get(url, timeout=3, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "null")


def _web_python() -> str:
    """The webapp venv's python if there is one, else this interpreter."""
    for rel in ("bin/python", "Scripts/python.exe"):
        p = WEBAPP / ".venv" / rel
        if p.exists():
            return str(p)
    return sys.executable


# ---------------------------------------------------------------- commands

def cmd_web(args):
    url = _web_url()
    try:
        _get(url + "/api/v1/health", 2)
        print(f"already running: {url}")
    except Exception:  # noqa: BLE001
        py = _web_python()
        try:
            subprocess.run([py, "-c", "import fastapi, uvicorn"], check=True, capture_output=True)
        except (subprocess.CalledProcessError, OSError):
            sys.exit("fastapi/uvicorn missing. Install with:\n"
                     f"  {py} -m pip install -r {WEBAPP / 'requirements.txt'}")
        if not (WEBAPP / "frontend" / "dist" / "index.html").exists():
            print("frontend not built — run: cd webapp/frontend && npm install && npm run build", file=sys.stderr)
        c = _web_cfg()
        log = osutil.cache_dir() / "web.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        cmd = [py, "-m", "uvicorn", "app.api:app", "--host", str(c.get("host", "127.0.0.1")),
               "--port", str(c.get("port", 8799))]
        if "--foreground" in args:
            sys.exit(subprocess.call(cmd, cwd=WEBAPP / "backend"))
        with open(log, "ab") as fh:
            subprocess.Popen(cmd, cwd=WEBAPP / "backend", stdout=fh, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, **osutil.detach_kwargs())
        for _ in range(30):
            time.sleep(0.5)
            try:
                _get(url + "/api/v1/health", 1)
                break
            except Exception:  # noqa: BLE001
                continue
        print(f"started: {url}  (log {log})")
    if "--no-open" not in args:
        osutil.open_url(url)


def cmd_demo(args):
    """Build a synthetic two weeks into <cache>/demo and serve it on port 8798.
    Your own data, sources and API keys are never touched."""
    demo = osutil.cache_dir() / "demo"
    port = 8798
    env = {**os.environ, "JOURNAL_DEMO": str(demo), "JOURNAL_AUTOSYNC": "0", "JOURNAL_LLM_DISABLE": "1"}
    py = _web_python()
    url = f"http://127.0.0.1:{port}"
    try:
        _get(url + "/api/v1/health", 1)
        print(f"demo already running: {url}")
    except Exception:  # noqa: BLE001
        print("building demo data …")
        r = subprocess.run([py, "-m", "app.demo"], cwd=WEBAPP / "backend", env=env)
        if r.returncode:
            return r.returncode
        log = osutil.cache_dir() / "demo.log"
        with open(log, "ab") as fh:
            subprocess.Popen([py, "-m", "uvicorn", "app.api:app", "--host", "127.0.0.1", "--port", str(port)],
                             cwd=WEBAPP / "backend", env=env, stdout=fh, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, **osutil.detach_kwargs())
        for _ in range(30):
            time.sleep(0.5)
            try:
                _get(url + "/api/v1/health", 1)
                break
            except Exception:  # noqa: BLE001
                continue
        print(f"demo: {url}  (data {demo})")
    if "--no-open" not in args:
        osutil.open_url(url)
    return 0


def cmd_dashboard(args):
    rc = _py("dashboard.py", args)
    if rc == 0:
        osutil.open_url((BASE / "reports" / "dashboard.html").as_uri())
    return rc


def cmd_install(args):
    """Linux/macOS: ~/.local/bin/journal → ./journal. Windows: a journal.cmd
    shim in %LOCALAPPDATA%\\Microsoft\\WindowsApps (on PATH by default)."""
    if osutil.IS_WINDOWS:
        d = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Microsoft" / "WindowsApps"
        d.mkdir(parents=True, exist_ok=True)
        (d / "journal.cmd").write_text(f'@"{sys.executable}" "{BASE / "journal.py"}" %*\r\n', encoding="utf-8")
        print(f"installed: {d / 'journal.cmd'}")
        return 0
    d = Path.home() / ".local" / "bin"
    d.mkdir(parents=True, exist_ok=True)
    link = d / "journal"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(BASE / "journal")
    print(f"installed: {link} -> {BASE / 'journal'}")
    if str(d) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"note: add {d} to your PATH")
    return 0


def cmd_schedule(args):
    action = (args or ["status"])[0]
    if action == "install":
        for line in osutil.install_schedule():
            print(line)
    elif action == "remove":
        for line in osutil.remove_schedule():
            print(line)
    else:
        for kind in osutil.JOBS:
            s = osutil.schedule_status(kind)
            state = "not installed" if not s["installed"] else ("on" if s["timer_active"] else "off")
            print(f"{kind:7} {state:13} next {s['next_run'] or '-':20} last {s['last_trigger'] or '-':20} "
                  f"{s['last_result'] or ''}  [{s['backend']}]")
    return 0


# ---------------------------------------------------------------- doctor / status

OK, WARN, BAD = "✓", "!", "✗"


def _check(rows, mark, name, detail=""):
    rows.append((mark, name, detail))


def _doctor_rows() -> list:
    rows = []
    cfg = osutil.load_config()
    web = _web_cfg()
    _check(rows, OK if sys.version_info >= (3, 10) else BAD, "python", f"{sys.version.split()[0]} ({osutil.SYSTEM})")
    _check(rows, OK, "config dir", str(osutil.config_dir()))
    # web app
    py = _web_python()
    r = subprocess.run([py, "-c", "import fastapi, uvicorn"], capture_output=True)
    _check(rows, OK if r.returncode == 0 else BAD, "web deps", py if r.returncode == 0 else
           f"pip install -r {WEBAPP / 'requirements.txt'}")
    built = (WEBAPP / "frontend" / "dist" / "index.html").exists()
    _check(rows, OK if built else WARN, "frontend build", "dist/ ok" if built else "cd webapp/frontend && npm i && npm run build")
    # collectors
    try:
        info = _get(cfg["aw_url"].rstrip("/") + "/api/0/info")
        _check(rows, OK, "ActivityWatch", f"{cfg['aw_url']} host={info.get('hostname')}")
    except Exception as e:  # noqa: BLE001
        _check(rows, BAD, "ActivityWatch", f"{cfg['aw_url']} unreachable ({type(e).__name__}) — install ActivityWatch")
    sp = cfg.get("sp_url")
    cap = web.get("capture", {}).get("enabled")
    if sp:
        try:
            tok = Path(cfg["sp_token_file"]).read_text(encoding="utf-8").strip() if Path(cfg["sp_token_file"]).exists() else ""
            try:
                h = _get(sp.rstrip("/") + "/health", 5, {"Authorization": f"Bearer {tok}"} if tok else {})
            except urllib.error.HTTPError as e:  # 503 = degraded, body has details
                h = json.loads(e.read().decode("utf-8") or "{}")
            last = h.get("last_frame_timestamp")
            _check(rows, OK if last else WARN, "screenpipe", f"{sp} last frame {last or 'none'}")
        except Exception as e:  # noqa: BLE001
            _check(rows, WARN, "screenpipe", f"{sp} unreachable ({type(e).__name__})")
    elif not cap:
        _check(rows, WARN, "screen text", "off — enable screenpipe (sp_url) or the built-in sampler (capture.enabled)")
    # OCR + LLM
    os.environ.setdefault("JOURNAL_AUTOSYNC", "0")
    sys.path.insert(0, str(WEBAPP / "backend"))
    try:
        from app import capture, config as wcfg, ocr  # type: ignore
        d = ocr.describe()
        mark = OK if d["configured"] else BAD
        detail = d["engine"] + (f" model={d.get('model')}" if d.get("model") else "") + (f" url={d['url']}" if d.get("url") else "")
        _check(rows, mark, "OCR engine", detail + (f" — {d['problem']}" if d["problem"] else ""))
        if cap:
            ok, why = capture.support()
            _check(rows, OK if ok else BAD, "built-in capture", "supported" if ok else why)
        _check(rows, OK if wcfg.LLM_API_KEY and wcfg.LLM_BASE_URL else WARN, "LLM key (问 AI, 归类)",
               "BAILIAN_API_KEY set" if wcfg.LLM_API_KEY else f"add BAILIAN_API_KEY / BAILIAN_BASE_URL to {osutil.config_dir() / '.env'}")
    except Exception as e:  # noqa: BLE001
        _check(rows, WARN, "OCR engine", f"cannot load webapp ({type(e).__name__}: {e})")
    # agent sources and tools
    for k, name in (("claude_dir", "Claude Code sessions"), ("pi_dir", "pi sessions")):
        p = Path(cfg.get(k) or "")
        _check(rows, OK if p.is_dir() else WARN, name, str(p) + ("" if p.is_dir() else " (not found)"))
    roots = [r for r in cfg.get("project_roots", []) if Path(r).is_dir()]
    _check(rows, OK if roots else WARN, "project roots", ", ".join(roots) or "none found — edit project_roots")
    for k, why in (("pi_bin", "reports & analysis"), ("gh_bin", "PR stats"), ("lark_bin", "Feishu push")):
        p = cfg.get(k)
        _check(rows, OK if p and Path(p).exists() else WARN, k.replace("_bin", ""), (p if p and Path(p).exists() else "not found") + f" ({why})")
    for tool, why in (("git", "commits"), ("ffmpeg", "screenpipe video frames")):
        p = osutil.which(tool)
        _check(rows, OK if p else WARN, tool, (p or "not found") + f" ({why})")
    for kind in osutil.JOBS:
        s = osutil.schedule_status(kind)
        _check(rows, OK if s["installed"] else WARN, f"schedule {kind}",
               f"next {s['next_run']}" if s["installed"] else "not installed — journal schedule install")
    return rows


def cmd_doctor(args):
    rows = _doctor_rows()
    w = max(len(r[1]) for r in rows)
    for mark, name, detail in rows:
        print(f" {mark} {name:<{w}}  {detail}")
    bad = sum(1 for r in rows if r[0] == BAD)
    print(f"\n{len(rows)} checks · {bad} problems · {sum(1 for r in rows if r[0] == WARN)} warnings")
    return 1 if bad else 0


def cmd_status(args):
    url = _web_url()
    try:
        st = _get(url + "/api/v1/screen/status", 5)
        print(f"web        {url}  screen: {st.get('state')} {st.get('last_frame') or ''}")
    except Exception:  # noqa: BLE001
        print(f"web        not running ({url}) — journal web")
    cfg = osutil.load_config()
    try:
        info = _get(cfg["aw_url"].rstrip("/") + "/api/0/info")
        ev = _get(f"{cfg['aw_url'].rstrip('/')}/api/0/buckets/aw-watcher-window_{info['hostname']}/events?limit=1")
        e = (ev or [{}])[0]
        print(f"activity   {e.get('data', {}).get('app', '-')} | {e.get('data', {}).get('title', '')[:50]} @ {e.get('timestamp', '')[:19]}")
    except Exception:  # noqa: BLE001
        print("activity   ActivityWatch unreachable")
    if cfg.get("sp_service"):
        print(f"screenpipe service {cfg['sp_service']}: {osutil.service_state(cfg['sp_service'])}")
    cmd_schedule(["status"])
    for d in (BASE / "reports", BASE / "reports" / "weekly"):
        files = sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:3] if d.is_dir() else []
        if files:
            print(f"{d.name:10} " + "  ".join(f.name for f in files))
    return 0


COMMANDS = {
    "collect": lambda a: _py("collect.py", a), "daily": lambda a: _py("daily.py", a),
    "weekly": lambda a: _py("weekly.py", a), "analyze": lambda a: _py("analyze.py", a),
    "feishu": lambda a: _py("feishu.py", a), "dashboard": cmd_dashboard, "web": cmd_web, "demo": cmd_demo,
    "install": cmd_install, "schedule": cmd_schedule, "doctor": cmd_doctor, "status": cmd_status,
}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help", "help") or argv[0] not in COMMANDS:
        print(__doc__.strip())
        return 0 if not argv or argv[0] in ("-h", "--help", "help") else 2
    return COMMANDS[argv[0]](argv[1:]) or 0


if __name__ == "__main__":
    sys.exit(main())
