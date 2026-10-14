"""Password login for when the dashboard is reachable from outside this machine.

Off unless <config dir>/.env holds JOURNAL_PASSWORD_HASH (make one with
`python -m app.auth`). Requests made on this machine straight to 127.0.0.1 /
localhost stay open; everything else — e.g. arriving through a Cloudflare
tunnel, which adds Cf-Connecting-IP — needs the session cookie set by /login.
The password itself is never stored, only a salted PBKDF2 hash.
"""
import hashlib
import hmac
import html
import secrets
import time
from urllib.parse import quote

from . import config
from .i18n import tr

COOKIE = "jw_session"
TTL = 30 * 86400
ITERATIONS = 310_000
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
_PROXY_HEADERS = ("cf-connecting-ip", "x-forwarded-for", "x-real-ip", "forwarded")
_FAILS: dict[str, list[float]] = {}     # client -> recent failure times
MAX_FAILS, FAIL_WINDOW = 8, 15 * 60


def make_hash(password: str, salt: str | None = None, iterations: int = ITERATIONS) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${dk.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        algo, it, salt, _ = stored.split("$")
    except ValueError:
        return False
    return algo == "pbkdf2_sha256" and hmac.compare_digest(make_hash(password, salt, int(it)), stored)


def _stored() -> str:
    return config.osutil.secret("JOURNAL_PASSWORD_HASH")


def enabled() -> bool:
    return bool(_stored())


def _key() -> bytes:
    # tied to the password hash: changing the password signs everyone out
    return hashlib.sha256(b"jw-session|" + _stored().encode()).digest()


def issue() -> str:
    exp = str(int(time.time()) + TTL)
    return exp + "." + hmac.new(_key(), exp.encode(), hashlib.sha256).hexdigest()


def valid(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp, sig = token.split(".", 1)
    good = hmac.new(_key(), exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and exp.isdigit() and int(exp) > time.time()


def is_local(request) -> bool:
    """A request made on this machine directly, not relayed by a proxy or tunnel."""
    h = request.headers
    host = h.get("host") or ""
    host = host.split("]")[0] + "]" if host.startswith("[") else host.rsplit(":", 1)[0]
    client = request.client.host if request.client else ""
    return client in _LOCAL_HOSTS and host in _LOCAL_HOSTS and not any(k in h for k in _PROXY_HEADERS)


def client_id(request) -> str:
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")


def throttled(who: str) -> bool:
    now = time.time()
    _FAILS[who] = [t for t in _FAILS.get(who, []) if now - t < FAIL_WINDOW]
    return len(_FAILS[who]) >= MAX_FAILS


def failed(who: str):
    _FAILS.setdefault(who, []).append(time.time())


def succeeded(who: str):
    _FAILS.pop(who, None)


def safe_next(nxt: str | None) -> str:
    # same-site paths only: "/x" but not "//evil" or "/\evil"
    return nxt if nxt and nxt.startswith("/") and not nxt.startswith(("//", "/\\")) else "/"


def login_url(path: str) -> str:
    return "/login?next=" + quote(path, safe="/")


def page(nxt: str, error: str = "") -> str:
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="{tr('zh-CN', 'en')}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{tr('登录 · 工作日志', 'Sign in · Work Journal')}</title>
<style>
:root {{ --bg:#f7f7f8; --card:#fff; --fg:#1f2023; --muted:#6b6f76; --line:#e2e3e6; --accent:#5e6ad2; --soft:#eef0fb; --err:#c2410c; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#111214; --card:#1a1b1e; --fg:#e8e9eb; --muted:#9a9ea6; --line:#2c2e33; --accent:#8b93ff; --soft:#25284a; --err:#fb923c; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; min-height:100vh; display:grid; place-items:center; padding:16px; background:var(--bg); color:var(--fg);
  font:14px/1.5 -apple-system, "Segoe UI", "PingFang SC", "Noto Sans CJK SC", sans-serif; }}
form {{ width:100%; max-width:340px; background:var(--card); border:1px solid var(--line); border-radius:14px; padding:28px 24px; }}
h1 {{ margin:0 0 4px; font-size:18px; }}
p.sub {{ margin:0 0 20px; color:var(--muted); font-size:13px; }}
input {{ width:100%; padding:10px 12px; border:1px solid var(--line); border-radius:9px; background:transparent; color:inherit; font:inherit; }}
input:focus {{ outline:none; border-color:var(--accent); box-shadow:0 0 0 3px var(--soft); }}
button {{ width:100%; margin-top:12px; padding:10px; border:0; border-radius:9px; background:var(--accent); color:#fff; font:inherit; font-weight:600; cursor:pointer; }}
.err {{ margin:10px 0 0; color:var(--err); font-size:13px; }}
</style></head>
<body><form method="post" action="/login">
<h1>{tr('工作日志', 'Work Journal')}</h1>
<p class="sub">{tr('请输入密码访问', 'Enter the password to continue')}</p>
<input type="hidden" name="next" value="{html.escape(nxt)}">
<input type="password" name="password" autocomplete="current-password" autofocus required
  placeholder="{tr('密码', 'Password')}" aria-label="{tr('密码', 'Password')}">
{err}<button type="submit">{tr('登录', 'Sign in')}</button>
</form></body></html>"""


if __name__ == "__main__":
    import getpass
    pw = getpass.getpass("Dashboard password: ")
    if pw != getpass.getpass("Again: "):
        raise SystemExit("passwords differ")
    print("JOURNAL_PASSWORD_HASH=" + make_hash(pw))
