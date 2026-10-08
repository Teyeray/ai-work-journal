-- webapp SQLite schema. Migrations: bump meta.user_version in db.py.
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- durable per-source cursors
CREATE TABLE IF NOT EXISTS source_cursors (
  source_key TEXT PRIMARY KEY,
  cursor_json TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1,
  last_scan_at TEXT NOT NULL
);

-- immutable normalized events
CREATE TABLE IF NOT EXISTS source_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_key TEXT NOT NULL,
  event_key TEXT NOT NULL,
  event_type TEXT NOT NULL,
  occurred_at TEXT NOT NULL,          -- UTC ISO, when it happened
  observed_at TEXT,                   -- UTC ISO, when we ingested it
  day_key TEXT,                       -- local calendar day
  payload TEXT,                       -- JSON (raw, local trusted store)
  created_at TEXT NOT NULL,
  UNIQUE (source_key, event_key)
);
CREATE INDEX IF NOT EXISTS idx_events_occurred ON source_events (occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_day ON source_events (day_key, event_type);

CREATE TABLE IF NOT EXISTS agent_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,               -- pi | claude-code
  external_id TEXT NOT NULL,          -- jsonl file path
  cwd TEXT,
  start_at TEXT NOT NULL,
  end_at TEXT,
  status TEXT NOT NULL DEFAULT 'ended',  -- running | ended | stale
  project_id INTEGER,
  UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_sessions_start ON agent_sessions (start_at);

CREATE TABLE IF NOT EXISTS session_event_links (
  session_id INTEGER NOT NULL,
  event_id INTEGER NOT NULL,
  ordinal INTEGER NOT NULL,
  UNIQUE (session_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_links_session ON session_event_links (session_id, ordinal);

CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  color TEXT,
  priority INTEGER DEFAULT 3,          -- 1..5, user-owned; 1 = highest
  active INTEGER NOT NULL DEFAULT 1,
  goals_json TEXT,
  rules_json TEXT,
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- append-only human corrections; never overwritten by re-ingest/re-analysis
CREATE TABLE IF NOT EXISTS manual_overrides (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target_type TEXT NOT NULL,           -- 'activity_segment' | 'session' | ...
  day_key TEXT NOT NULL,
  anchor_start TEXT NOT NULL,          -- segment start (UTC) the correction targets
  field TEXT NOT NULL,                 -- project | activity_type | start | end
  value TEXT NOT NULL,
  original_value TEXT,
  note TEXT,
  created_at TEXT NOT NULL,
  superseded_by INTEGER REFERENCES manual_overrides (id)
);
CREATE INDEX IF NOT EXISTS idx_overrides_day ON manual_overrides (day_key, target_type);

CREATE TABLE IF NOT EXISTS planned_blocks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  day_key TEXT NOT NULL,
  start_at TEXT NOT NULL,
  end_at TEXT NOT NULL,
  project_id INTEGER,
  activity_type TEXT,
  description TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_planned_day ON planned_blocks (day_key);

CREATE TABLE IF NOT EXISTS outcomes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER,
  day_key TEXT NOT NULL,
  title TEXT NOT NULL,
  verification_state TEXT NOT NULL DEFAULT 'inferred',  -- verified | user_reported | inferred
  evidence_json TEXT,
  source_type TEXT NOT NULL DEFAULT 'manual',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outcomes_day ON outcomes (day_key, project_id);

CREATE TABLE IF NOT EXISTS session_reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  analyzer_version TEXT NOT NULL,
  input_fingerprint TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed | unreviewed
  review_json TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (session_id, analyzer_version, input_fingerprint)
);

CREATE TABLE IF NOT EXISTS review_feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  review_id INTEGER NOT NULL,
  category TEXT NOT NULL,              -- helpful | not_helpful | inaccurate
  note TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_type TEXT NOT NULL,
  target_key TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'queued',  -- queued | running | done | failed
  attempt INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (job_type, target_key, fingerprint)
);

CREATE TABLE IF NOT EXISTS daily_projections (
  day_key TEXT NOT NULL,
  projection_version INTEGER NOT NULL,
  input_fingerprint TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (day_key, projection_version, input_fingerprint)
);

CREATE TABLE IF NOT EXISTS sync_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  source_key TEXT,
  new_events INTEGER NOT NULL DEFAULT 0,
  detail TEXT
);
