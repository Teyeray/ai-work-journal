"""Built-in screen sampler: a screenshot of every monitor every N seconds, read
by an OCR API (app/ocr.py), stored in screen_captures. For machines without
screenpipe — or where you would rather send screenshots to a cloud OCR model
than run one locally.

Works on Windows, macOS (grant Screen Recording to the terminal / Python) and
Linux X11. Wayland does not let ordinary programs read the screen; use
screenpipe there (it goes through the screen-share portal).

Cheap by design: unchanged screens are skipped (32×18 thumbnail diff), the
focused window of a sensitive app (privacy.sensitive_apps) is never captured,
and old captures are deleted after keep_days.

Needs:  pip install mss pillow
"""
import datetime as dt
import io
import json
import os
import threading
import time
import urllib.request

from . import config, db, ocr
from .timeutil import iso, now_utc, now_iso

_S = {"thread": None, "stop": None, "last_at": None, "last_error": None,
      "captured": 0, "skipped": 0, "sig": {}, "cleaned_at": 0.0, "aw_bucket": None}
_LOCK = threading.Lock()


# ---------------------------------------------------------------- support

def support() -> tuple[bool, str]:
    sysname = config.osutil.SYSTEM
    if sysname == "linux":
        if os.environ.get("XDG_SESSION_TYPE") == "wayland" or (
                os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY")):
            return False, "Wayland 不允许普通程序读屏幕,请改用 screenpipe(走系统的屏幕共享授权)"
        if not os.environ.get("DISPLAY"):
            return False, "没有图形显示(DISPLAY 未设置)"
    try:
        import mss  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        return False, "缺少依赖:pip install mss pillow"
    return True, ""


# ---------------------------------------------------------------- one round

def _grab() -> list[tuple[int, bytes, bytes]]:
    """[(monitor, jpeg, signature)] for every monitor."""
    import mss
    from PIL import Image
    width = int(config.CAPTURE.get("max_width", 1600))
    quality = int(config.CAPTURE.get("quality", 70))
    out = []
    with mss.mss() as s:
        for i, mon in enumerate(s.monitors[1:], 1):
            shot = s.grab(mon)
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            sig = img.convert("L").resize((32, 18)).tobytes()
            if img.width > width:
                img = img.resize((width, round(img.height * width / img.width)))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=quality)
            out.append((i, buf.getvalue(), sig))
    return out


def _changed(prev: bytes, sig: bytes) -> bool:
    if not prev or len(prev) != len(sig):
        return True
    return sum(abs(a - b) for a, b in zip(prev, sig)) / len(sig) > 2.0


def _focus() -> tuple[str, str]:
    """(app, window title) in focus now, from ActivityWatch; ('', '') if unknown."""
    try:
        if not _S["aw_bucket"]:
            with urllib.request.urlopen(f"{config.AW_URL}/api/0/info", timeout=3) as r:
                _S["aw_bucket"] = f"aw-watcher-window_{json.load(r).get('hostname', config.AW_HOST)}"
        with urllib.request.urlopen(f"{config.AW_URL}/api/0/buckets/{_S['aw_bucket']}/events?limit=1", timeout=3) as r:
            ev = (json.load(r) or [{}])[0].get("data", {})
        return ev.get("app", ""), ev.get("title", "")
    except Exception:  # noqa: BLE001 — AW not running is fine
        return "", ""


def _sensitive(app: str, title: str) -> bool:
    s = f"{app} {title}".lower()
    return any(a and a in s for a in config.SENSITIVE_APPS)


def capture_once() -> dict:
    """Take, OCR and store one round of screenshots. Returns counts."""
    app, title = _focus()
    if _sensitive(app, title):
        _S["skipped"] += 1
        return {"captured": 0, "skipped": "sensitive"}
    shots = _grab()
    kept = 0
    for mon, jpg, sig in shots:
        if config.CAPTURE.get("skip_unchanged", True) and not _changed(_S["sig"].get(mon), sig):
            _S["skipped"] += 1
            continue
        _S["sig"][mon] = sig
        ts = now_utc()
        rel = f"{ts:%Y-%m-%d}/{ts:%H%M%S}_m{mon}.jpg"
        path = config.CAPTURE_DIR / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(jpg)
        text = err = eng = ms = None
        if ocr.is_api():
            try:
                r = ocr.recognize(jpg)
                text, eng, ms = r["text"], r["engine"], r["ms"]
            except ocr.OcrError as e:
                err = str(e)[:300]
        db.ex("INSERT INTO screen_captures (captured_at, monitor, app, window, image, text, engine, ocr_ms, error) "
              "VALUES (?,?,?,?,?,?,?,?,?)", (iso(ts), mon, app or None, title or None, rel, text, eng, ms, err))
        kept += 1
        _S["last_error"] = err
    _S["captured"] += kept
    _S["last_at"] = now_iso()
    return {"captured": kept, "monitors": len(shots)}


def cleanup() -> int:
    """Delete captures older than keep_days (rows and image files)."""
    keep = int(config.CAPTURE.get("keep_days", 14))
    cut = iso(now_utc() - dt.timedelta(days=keep))
    rows = db.q("SELECT id, image FROM screen_captures WHERE captured_at < ?", (cut,))
    base = config.CAPTURE_DIR.resolve()
    for r in rows:
        if r["image"]:
            p = (base / r["image"]).resolve()
            if base in p.parents:
                p.unlink(missing_ok=True)
    db.ex("DELETE FROM screen_captures WHERE captured_at < ?", (cut,))
    for d in sorted(base.glob("*"), reverse=True) if base.is_dir() else []:
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    return len(rows)


# ---------------------------------------------------------------- background loop

def _loop(stop: threading.Event):
    interval = max(10, int(config.CAPTURE.get("interval_sec", 60)))
    while not stop.wait(interval):
        try:
            capture_once()
            if time.time() - _S["cleaned_at"] > 3600:
                cleanup()
                _S["cleaned_at"] = time.time()
        except Exception as e:  # noqa: BLE001 — a bad round must not end the loop
            _S["last_error"] = f"{type(e).__name__}: {e}"[:300]


def start() -> bool:
    with _LOCK:
        if not config.CAPTURE.get("enabled") or (_S["thread"] and _S["thread"].is_alive()):
            return False
        ok, why = support()
        if not ok:
            _S["last_error"] = why
            return False
        _S["stop"] = threading.Event()
        _S["thread"] = threading.Thread(target=_loop, args=(_S["stop"],), name="screen-capture", daemon=True)
        _S["thread"].start()
        return True


def stop():
    with _LOCK:
        if _S["stop"]:
            _S["stop"].set()


def running() -> bool:
    return bool(_S["thread"] and _S["thread"].is_alive())


def status() -> dict:
    """ok | stalled | down | off, in the same shape as the screenpipe status."""
    if not config.CAPTURE.get("enabled"):
        return {"state": "off", "source": "builtin"}
    ok, why = support()
    last = db.q1("SELECT captured_at FROM screen_captures ORDER BY captured_at DESC LIMIT 1")
    v = {"source": "builtin", "state": "ok", "last_frame": None, "restartable": False,
         "monitors": [], "ocr": ocr.describe()}
    if last:
        from .timeutil import parse_ts, tz
        v["last_frame"] = parse_ts(last["captured_at"]).astimezone(tz()).isoformat()
    if not ok:
        v.update(state="down", reason=why, hint="在 webapp/config.json 关闭 capture.enabled,或换用 screenpipe")
    elif not running():
        v.update(state="down", reason="截屏线程没有运行", hint="重启 journal web")
    elif not ocr.is_api():
        v.update(state="stalled", reason="截图在保存,但 OCR 引擎不是 API(ocr.engine)",
                 hint="把 ocr.engine 设为 openai 或 http,并配置密钥")
    elif _S["last_error"]:
        v.update(state="stalled", reason=f"OCR 失败:{_S['last_error']}", hint="检查 OCR 接口地址和密钥")
    return v


# ---------------------------------------------------------------- queries

def search(query=None, start=None, end=None, limit=8, offset=0) -> tuple[int, list[dict]]:
    """Captures newest first; start/end are UTC iso bounds."""
    where, args = ["1=1"], []
    if query:
        where.append("(text LIKE ? OR window LIKE ?)")
        args += [f"%{query}%"] * 2
    if start:
        where.append("captured_at >= ?")
        args.append(start)
    if end:
        where.append("captured_at < ?")
        args.append(end)
    w = " AND ".join(where)
    total = db.q1(f"SELECT COUNT(*) n FROM screen_captures WHERE {w}", args)["n"]
    rows = db.q(f"SELECT * FROM screen_captures WHERE {w} ORDER BY captured_at DESC LIMIT ? OFFSET ?",
                args + [limit, offset])
    return total, [dict(r) for r in rows]


def count(start: str, end: str) -> int:
    return db.q1("SELECT COUNT(*) n FROM screen_captures WHERE captured_at >= ? AND captured_at < ?",
                 (start, end))["n"]


def image_file(cid: int):
    r = db.q1("SELECT image FROM screen_captures WHERE id = ?", (cid,))
    if not r or not r["image"]:
        return None
    base = config.CAPTURE_DIR.resolve()
    p = (base / r["image"]).resolve()
    return p if base in p.parents and p.is_file() else None


def coverage() -> dict:
    r = db.q1("SELECT COUNT(*) n, MIN(captured_at) a, MAX(captured_at) b FROM screen_captures")
    return {"frames": r["n"], "first": r["a"], "last": r["b"]}


def frame_url(cid: int) -> str:
    return f"/api/v1/screen/capture/{cid}"

