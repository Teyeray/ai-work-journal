"""SQLite (WAL) with simple schema migrations keyed by meta.user_version."""
import sqlite3
import threading
from pathlib import Path

from . import config
from .timeutil import now_iso

_LOCAL = threading.local()
_LOCK = threading.Lock()
SCHEMA = (Path(__file__).parent.parent / "schema.sql").read_text(encoding="utf-8")


def connect() -> sqlite3.Connection:
    conn = getattr(_LOCAL, "conn", None)
    if conn is None:
        config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(config.DB_PATH), timeout=30, isolation_level=None,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA foreign_keys=ON")
        _LOCAL.conn = conn
        migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    with _LOCK:
        v = conn.execute("PRAGMA user_version").fetchone()[0]
        if v < 1:
            # executescript manages its own transactions; script is idempotent
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA user_version = 1")
        if v < 2:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_sessions)")}
            if "project_label" not in cols:
                conn.execute("ALTER TABLE agent_sessions ADD COLUMN project_label TEXT")
            conn.execute("PRAGMA user_version = 2")
        if v < 3:
            # semantic labels for window titles that carry no keyword/path evidence
            conn.execute("""CREATE TABLE IF NOT EXISTS llm_title_labels (
                key TEXT PRIMARY KEY,          -- sha1(app \\n cleaned title)
                app TEXT, title TEXT,
                project TEXT,                  -- NULL = model found no project
                confidence REAL, why TEXT, model TEXT,
                projects_sig TEXT NOT NULL,    -- project set the answer was given against
                created_at TEXT NOT NULL)""")
            conn.execute("PRAGMA user_version = 3")
        if v < 4:
            # "问 AI" conversations; messages keep the full OpenAI turn (incl. tool calls)
            conn.execute("""CREATE TABLE IF NOT EXISTS chat_threads (
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id INTEGER NOT NULL REFERENCES chat_threads(id) ON DELETE CASCADE,
                turn INTEGER NOT NULL,         -- one user question + everything it caused
                role TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL)""")
            conn.execute("CREATE INDEX IF NOT EXISTS chat_messages_thread ON chat_messages(thread_id, id)")
            conn.execute("PRAGMA user_version = 4")


def q(sql, params=()):
    return connect().execute(sql, params).fetchall()


def q1(sql, params=()):
    return connect().execute(sql, params).fetchone()


def ex(sql, params=()):
    return connect().execute(sql, params)


class Tx:
    """Immediate write transaction; nested calls join the outer one."""

    def __enter__(self):
        conn = connect()
        self.depth = getattr(_LOCAL, "tx_depth", 0)
        if self.depth == 0:
            conn.execute("BEGIN IMMEDIATE")
        _LOCAL.tx_depth = self.depth + 1
        return conn

    def __exit__(self, et, ev, tb):
        conn = connect()
        _LOCAL.tx_depth = self.depth
        if self.depth == 0:
            if et is None:
                conn.execute("COMMIT")
            else:
                conn.execute("ROLLBACK")
        return False


def get_meta(key, default=None):
    row = q1("SELECT value FROM meta WHERE key=?", (key,))
    return row["value"] if row else default


def set_meta(key, value):
    ex("INSERT INTO meta(key,value) VALUES(?,?) "
       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def log_sync(source_key, new_events, detail=None):
    ex("INSERT INTO sync_log(at, source_key, new_events, detail) VALUES(?,?,?,?)",
       (now_iso(), source_key, new_events, detail))
