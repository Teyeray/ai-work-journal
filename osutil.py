"""Platform layer: config loading, per-OS paths, tool lookup, background
processes and the scheduler/service manager (systemd · launchd · Task Scheduler).

Everything OS-specific in the journal goes through here, so the rest of the
code never calls systemctl, xdg-open or bash directly.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import webbrowser
from pathlib import Path

APP = "ai-work-journal"
REPO_ROOT = Path(__file__).resolve().parent
SYSTEM = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
IS_WINDOWS = SYSTEM == "windows"

if IS_WINDOWS:  # Chinese output on a cp936/cp1252 console or pipe; children inherit
    os.environ.setdefault("PYTHONUTF8", "1")
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


# ---------------------------------------------------------------- paths

def _legacy_dir() -> Path:
    return Path.home() / ".config" / APP


def config_dir() -> Path:
    """Secrets (.env), schedule.json, screenpipe token. Linux: ~/.config/…;
    macOS: ~/Library/Application Support/…; Windows: %APPDATA%\\…
    An existing ~/.config/ai-work-journal wins everywhere (dotfile users)."""
    if os.environ.get("JOURNAL_CONFIG_DIR"):
        return Path(os.environ["JOURNAL_CONFIG_DIR"]).expanduser()
    if SYSTEM == "linux":
        return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP
    if _legacy_dir().is_dir():
        return _legacy_dir()
    if SYSTEM == "macos":
        return Path.home() / "Library" / "Application Support" / APP
    return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP


def cache_dir() -> Path:
    """Logs, extracted frames, captured screenshots."""
    if os.environ.get("JOURNAL_CACHE_DIR"):
        return Path(os.environ["JOURNAL_CACHE_DIR"]).expanduser()
    if SYSTEM == "macos":
        return Path.home() / "Library" / "Caches" / APP
    if IS_WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP / "cache"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / APP


def expand(p) -> str:
    """~ and $VARS / %VARS% in a configured path."""
    return os.path.expandvars(os.path.expanduser(str(p))) if p else p


def short_home(p: str) -> str:
    """/home/me/x → ~/x (for reports and prompts)."""
    home = str(Path.home())
    return "~" + p[len(home):] if p and p.startswith(home) else p


# ---------------------------------------------------------------- time zone

def local_zone(name: str = ""):
    """tzinfo for an IANA name, else this machine's zone: $TZ, /etc/localtime
    (Linux, macOS), tzlocal (Windows, optional), else the current fixed offset."""
    from zoneinfo import ZoneInfo
    cands = [name, os.environ.get("TZ", "").lstrip(":")]
    try:
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in link:
            cands.append(link.split("zoneinfo/", 1)[1])
    except OSError:
        pass
    try:
        cands.append(Path("/etc/timezone").read_text(encoding="utf-8").strip())
    except OSError:
        pass
    for c in cands:
        if c:
            try:
                return ZoneInfo(c)
            except Exception:  # noqa: BLE001 — unknown key / no tzdata
                continue
    try:
        import tzlocal  # type: ignore
        return tzlocal.get_localzone()
    except Exception:  # noqa: BLE001
        return dt.datetime.now().astimezone().tzinfo


# ---------------------------------------------------------------- config

def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


_PATH_KEYS = ("claude_dir", "pi_dir", "sp_token_file", "sp_data_dir")
_BIN_KEYS = {"gh_bin": "gh", "pi_bin": "pi", "lark_bin": "lark-cli"}


def load_config(base: Path = REPO_ROOT, name: str = "config.json") -> dict:
    """<name> (shared defaults, committed) deep-merged with <name stem>.local.json
    (your machine: paths, ids — gitignored). Paths are expanded; tool paths are
    resolved on PATH when not given absolutely."""
    local = {} if os.environ.get("JOURNAL_NO_LOCAL_CONFIG") else _read_json(base / name.replace(".json", ".local.json"))
    cfg = _merge(_read_json(base / name), local)
    if base == REPO_ROOT and name == "config.json":
        for k in _PATH_KEYS:
            if cfg.get(k):
                cfg[k] = expand(cfg[k])
        if not cfg.get("sp_token_file"):
            cfg["sp_token_file"] = str(config_dir() / "screenpipe-token")
        cfg["project_roots"] = [expand(r) for r in cfg.get("project_roots", [])]
        for k, default in _BIN_KEYS.items():
            cfg[k] = which(cfg.get(k) or default) or expand(cfg.get(k) or default)
    return cfg


def load_env(path=None) -> dict:
    """KEY=VALUE secrets file (default <config_dir>/.env), never in the repo."""
    out = {}
    try:
        p = Path(expand(path)) if path else config_dir() / ".env"
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def secret(name: str, env_file=None) -> str:
    return os.environ.get(name) or load_env(env_file).get(name, "")


# ---------------------------------------------------------------- tools & processes

def _extra_bin_dirs() -> list[str]:
    home = Path.home()
    dirs = [home / ".local" / "bin", home / ".bun" / "bin", home / ".cargo" / "bin"]
    if SYSTEM == "macos":
        dirs += [Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    elif SYSTEM == "linux":
        dirs += [Path("/usr/local/bin")]
    else:
        dirs += [home / "AppData" / "Roaming" / "npm", home / "scoop" / "shims"]
    return [str(d) for d in dirs if d.is_dir()]


def which(name: str):
    """Tool lookup on PATH plus the usual per-user bin dirs (timers and
    launchd start with a minimal PATH)."""
    if not name:
        return None
    p = expand(name)
    if os.path.isabs(p):
        return p if os.path.exists(p) else None
    return shutil.which(p) or shutil.which(p, path=os.pathsep.join(_extra_bin_dirs()))


def tool_env(**extra) -> dict:
    """Environment for headless CLI tools: inherited, with user bin dirs on
    PATH, UTF-8 output and no colour."""
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(_extra_bin_dirs() + [env.get("PATH", "")])
    env.setdefault("LANG", "C.UTF-8")
    env["TERM"] = "dumb"
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra)
    return env


def temp_dir() -> str:
    return tempfile.gettempdir()


def detach_kwargs() -> dict:
    """Popen kwargs: run in its own process group (survives, and can be
    stopped as a group)."""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def terminate(proc: subprocess.Popen):
    if proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass


def open_url(url: str):
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 — headless
        pass


def _run(cmd, timeout=30) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                          stdin=subprocess.DEVNULL)


# ---------------------------------------------------------------- services
# A "service" is a long-running collector (screenpipe, ActivityWatch). On
# Linux a systemd --user unit, on macOS a launchd agent label, on Windows a
# Task Scheduler task name.

def restart_service(name: str) -> tuple[bool, str]:
    try:
        if SYSTEM == "linux":
            r = _run(["systemctl", "--user", "restart", name], 60)
        elif SYSTEM == "macos":
            r = _run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{name}"], 60)
        else:
            _run(["schtasks", "/End", "/TN", name])
            r = _run(["schtasks", "/Run", "/TN", name])
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return r.returncode == 0, (r.stderr or r.stdout or "").strip()[:300]


def service_state(name: str) -> str:
    """active | inactive | missing | unknown"""
    try:
        if SYSTEM == "linux":
            r = _run(["systemctl", "--user", "is-active", name], 10)
            s = r.stdout.strip()
            return "active" if s == "active" else ("missing" if "could not be found" in r.stderr else "inactive")
        if SYSTEM == "macos":
            r = _run(["launchctl", "print", f"gui/{os.getuid()}/{name}"], 10)
            if r.returncode != 0:
                return "missing"
            return "active" if re.search(r"state = running", r.stdout) else "inactive"
        r = _run(["schtasks", "/Query", "/TN", name, "/FO", "LIST"], 10)
        if r.returncode != 0:
            return "missing"
        return "active" if re.search(r":\s*Running", r.stdout) else "inactive"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


# ---------------------------------------------------------------- report scheduler
# daily: every day at HH:MM · weekly: one weekday at HH:MM. Jobs run the
# script with --scheduled so daily.py / weekly.py honour the web page's
# weekday switches (a manual run always runs).

JOBS = {"daily": {"label": "ai-journal-daily", "script": "daily.py", "at": "23:00", "weekday": None},
        "weekly": {"label": "ai-journal-weekly", "script": "weekly.py", "at": "23:30", "weekday": 5}}
_WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _job_cmd(kind: str) -> list[str]:
    return [sys.executable, str(REPO_ROOT / JOBS[kind]["script"]), "--scheduled"]


def _launchd_plist(kind: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"org.{JOBS[kind]['label']}.plist"


def _systemd_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"


def install_schedule(kinds=("daily", "weekly")) -> list[str]:
    """Install (or overwrite) the daily/weekly report jobs. Returns what was done."""
    done = []
    for kind in kinds:
        j = JOBS[kind]
        hh, mm = (int(x) for x in j["at"].split(":"))
        cmd = _job_cmd(kind)
        if SYSTEM == "linux":
            d = _systemd_dir()
            d.mkdir(parents=True, exist_ok=True)
            cal = (f"{_WD[j['weekday'] - 1]} *-*-* {j['at']}:00" if j["weekday"] else f"*-*-* {j['at']}:00")
            (d / f"{j['label']}.service").write_text(
                f"[Unit]\nDescription=ai-work-journal {kind} report\n\n[Service]\nType=oneshot\n"
                f"WorkingDirectory={REPO_ROOT}\nExecStart={' '.join(cmd)}\nTimeoutStartSec=1800\n", encoding="utf-8")
            (d / f"{j['label']}.timer").write_text(
                f"[Unit]\nDescription=ai-work-journal {kind} report timer\n\n[Timer]\nOnCalendar={cal}\n"
                f"Persistent=true\n\n[Install]\nWantedBy=timers.target\n", encoding="utf-8")
            _run(["systemctl", "--user", "daemon-reload"])
            r = _run(["systemctl", "--user", "enable", "--now", f"{j['label']}.timer"])
            done.append(f"systemd timer {j['label']}.timer ({cal})" + ("" if r.returncode == 0 else f" FAILED: {r.stderr.strip()}"))
        elif SYSTEM == "macos":
            p = _launchd_plist(kind)
            p.parent.mkdir(parents=True, exist_ok=True)
            when = f"<key>Hour</key><integer>{hh}</integer><key>Minute</key><integer>{mm}</integer>"
            if j["weekday"]:
                when += f"<key>Weekday</key><integer>{j['weekday'] % 7}</integer>"
            log = cache_dir() / f"{kind}.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            args = "".join(f"<string>{a}</string>" for a in cmd)
            p.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                f'<plist version="1.0"><dict><key>Label</key><string>org.{j["label"]}</string>'
                f"<key>ProgramArguments</key><array>{args}</array>"
                f"<key>WorkingDirectory</key><string>{REPO_ROOT}</string>"
                f"<key>StartCalendarInterval</key><dict>{when}</dict>"
                f"<key>StandardOutPath</key><string>{log}</string><key>StandardErrorPath</key><string>{log}</string>"
                "</dict></plist>\n", encoding="utf-8")
            uid = os.getuid()
            _run(["launchctl", "bootout", f"gui/{uid}", str(p)])
            r = _run(["launchctl", "bootstrap", f"gui/{uid}", str(p)])
            done.append(f"launchd agent org.{j['label']} ({j['at']})" + ("" if r.returncode == 0 else f" FAILED: {r.stderr.strip()}"))
        else:
            tr = subprocess.list2cmdline(cmd)
            sc = ["/SC", "WEEKLY", "/D", _WD[j["weekday"] - 1].upper()[:3]] if j["weekday"] else ["/SC", "DAILY"]
            r = _run(["schtasks", "/Create", "/F", "/TN", j["label"], "/TR", tr, *sc, "/ST", j["at"]])
            done.append(f"scheduled task {j['label']} ({j['at']})" + ("" if r.returncode == 0 else f" FAILED: {r.stderr.strip()}"))
    return done


def remove_schedule(kinds=("daily", "weekly")) -> list[str]:
    done = []
    for kind in kinds:
        j = JOBS[kind]
        if SYSTEM == "linux":
            _run(["systemctl", "--user", "disable", "--now", f"{j['label']}.timer"])
            for ext in ("timer", "service"):
                (_systemd_dir() / f"{j['label']}.{ext}").unlink(missing_ok=True)
            _run(["systemctl", "--user", "daemon-reload"])
        elif SYSTEM == "macos":
            p = _launchd_plist(kind)
            _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(p)])
            p.unlink(missing_ok=True)
        else:
            _run(["schtasks", "/Delete", "/F", "/TN", j["label"]])
        done.append(f"removed {j['label']}")
    return done


def _stamp(v: str):
    m = re.search(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?)", v or "")
    return m.group(1).replace("T", " ") if m else None


def _next_local(kind: str):
    """Next run computed from the job's calendar (launchd has no 'next run')."""
    j = JOBS[kind]
    hh, mm = (int(x) for x in j["at"].split(":"))
    now = dt.datetime.now()
    t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    for _ in range(8):
        if t > now and (not j["weekday"] or t.isoweekday() == j["weekday"]):
            return t.strftime("%Y-%m-%d %H:%M:%S")
        t += dt.timedelta(days=1)
    return None


def schedule_status(kind: str) -> dict:
    """{installed, active, calendar, next_run, last_trigger, last_result, last_exit, running}"""
    j = JOBS[kind]
    out = {"installed": False, "timer_active": False, "calendar": None, "next_run": None,
           "last_trigger": None, "last_result": None, "last_exit": None, "running": False,
           "backend": {"linux": "systemd", "macos": "launchd", "windows": "schtasks"}[SYSTEM]}
    try:
        if SYSTEM == "linux":
            def show(unit, props):
                r = _run(["systemctl", "--user", "show", unit, "-p", props], 5)
                return dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
            t = show(f"{j['label']}.timer", "NextElapseUSecRealtime,LastTriggerUSec,ActiveState,TimersCalendar,LoadState")
            sv = show(f"{j['label']}.service", "Result,ExecMainExitTimestamp,ActiveState")
            cal = re.search(r"OnCalendar=([^;]+)", t.get("TimersCalendar", ""))
            out.update(installed=t.get("LoadState") == "loaded", timer_active=t.get("ActiveState") == "active",
                       calendar=cal.group(1).strip() if cal else None,
                       next_run=_stamp(t.get("NextElapseUSecRealtime")), last_trigger=_stamp(t.get("LastTriggerUSec")),
                       last_result=sv.get("Result"), last_exit=_stamp(sv.get("ExecMainExitTimestamp")),
                       running=sv.get("ActiveState") == "activating")
        elif SYSTEM == "macos":
            r = _run(["launchctl", "print", f"gui/{os.getuid()}/org.{j['label']}"], 5)
            if r.returncode == 0:
                code = re.search(r"last exit code = (\d+)", r.stdout)
                out.update(installed=True, timer_active=True, next_run=_next_local(kind),
                           calendar=(f"{_WD[j['weekday'] - 1]} " if j["weekday"] else "daily ") + j["at"],
                           last_result=None if not code else ("success" if code.group(1) == "0" else f"exit {code.group(1)}"),
                           running="state = running" in r.stdout)
        else:
            r = _run(["schtasks", "/Query", "/TN", j["label"], "/V", "/FO", "LIST"], 10)
            if r.returncode == 0:
                f = dict((k.strip(), v.strip()) for k, v in
                         (l.split(":", 1) for l in r.stdout.splitlines() if ":" in l))
                res = f.get("Last Result", "")
                out.update(installed=True, timer_active=f.get("Scheduled Task State", "Enabled") == "Enabled",
                           calendar=(f"{_WD[j['weekday'] - 1]} " if j["weekday"] else "daily ") + j["at"],
                           next_run=_stamp(f.get("Next Run Time")) or _next_local(kind),
                           last_trigger=_stamp(f.get("Last Run Time")),
                           last_result=None if not res else ("success" if res == "0" else f"exit {res}"),
                           running=f.get("Status") == "Running")
    except (OSError, subprocess.TimeoutExpired):
        pass
    return out


def started_by_scheduler() -> bool:
    """daily.py/weekly.py: started by the timer (honour the weekday switches)?
    --scheduled is passed by the jobs above; INVOCATION_ID covers systemd
    units written before that flag existed."""
    if "--scheduled" in sys.argv:
        return True
    return bool(os.environ.get("INVOCATION_ID")) and len(sys.argv) == 1


def script_args() -> list[str]:
    """sys.argv[1:] without our own flags."""
    return [a for a in sys.argv[1:] if a != "--scheduled"]
