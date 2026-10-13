"""Time helpers. Storage is UTC; day_key uses the configured local timezone."""
import datetime as dt

from . import config

_TZ = config.osutil.local_zone(config.TIMEZONE)


def tz():
    return _TZ


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def to_utc(ts: dt.datetime) -> dt.datetime:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return ts.astimezone(dt.timezone.utc)


def parse_ts(s):
    """Parse ISO timestamps (AW/pi/claude/git). Returns aware datetime or None."""
    if isinstance(s, dt.datetime):
        return to_utc(s) if s.tzinfo else s.replace(tzinfo=dt.timezone.utc)
    if not isinstance(s, str) or not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return to_utc(dt.datetime.fromisoformat(s))
    except ValueError:
        return None


def iso(ts: dt.datetime) -> str:
    return to_utc(ts).isoformat()


def local_day_key(ts_utc: dt.datetime) -> str:
    return ts_utc.astimezone(_TZ).strftime("%Y-%m-%d")


def day_bounds(day_key: str):
    """Local-day [start, end) as aware UTC datetimes."""
    d = dt.date.fromisoformat(day_key)
    start = dt.datetime(d.year, d.month, d.day, tzinfo=_TZ)
    return start.astimezone(dt.timezone.utc), (start + dt.timedelta(days=1)).astimezone(dt.timezone.utc)


def fmt_local(ts_utc: dt.datetime) -> str:
    return ts_utc.astimezone(_TZ).strftime("%H:%M")
