"""Shared configuration: repo config.json + webapp/config.json overlay."""
import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
WEBAPP_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = Path(__file__).resolve().parent

_repo_cfg = json.loads((REPO_ROOT / "config.json").read_text(encoding="utf-8"))
_web_cfg_path = WEBAPP_DIR / "config.json"
_web_cfg = json.loads(_web_cfg_path.read_text(encoding="utf-8")) if _web_cfg_path.exists() else {}

DB_PATH = Path(os.environ.get(
    "JOURNAL_WEB_DB", _web_cfg.get("db_path", str(REPO_ROOT / "data" / "webapp.db"))))

HOST = _web_cfg.get("host", "127.0.0.1")
PORT = int(_web_cfg.get("port", 8799))
TIMEZONE = _web_cfg.get("timezone", "Asia/Singapore")

AW_URL = _repo_cfg["aw_url"].rstrip("/")
AW_HOST = _repo_cfg.get("aw_host", "")
PI_SESSIONS_DIR = _repo_cfg.get("pi_dir")
CLAUDE_SESSIONS_DIR = _repo_cfg.get("claude_dir")
PROJECT_ROOTS = _repo_cfg.get("project_roots", [])
GH_BIN = _repo_cfg.get("gh_bin", "gh")

# LLM (pi headless) — explicit opt-out possible; journal pipeline already uses it
LLM = _web_cfg.get("llm", {"enabled": False})
LLM_ENABLED = bool(LLM.get("enabled", False))
PI_BIN = _repo_cfg.get("pi_bin")

# privacy
PRIVACY = _web_cfg.get("privacy", {})
SENSITIVE_APPS = [a.lower() for a in PRIVACY.get("sensitive_apps", [])]

RUNNING_THRESHOLD_SEC = int(_web_cfg.get("running_threshold_sec", 1800))

FRONTEND_DIST = WEBAPP_DIR / "frontend" / "dist"

# project classification: reuse the journal's own matcher module
import sys  # noqa: E402
sys.path.insert(0, str(REPO_ROOT))
