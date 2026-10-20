"""Shared configuration: repo config.json + webapp/config.json, each with an
optional *.local.json overlay for this machine (gitignored)."""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
WEBAPP_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = Path(__file__).resolve().parent

sys.path.insert(0, str(REPO_ROOT))
import osutil  # noqa: E402

_repo_cfg = osutil.load_config(REPO_ROOT, "config.json")
_web_cfg = osutil.load_config(WEBAPP_DIR, "config.json")
CONFIG_DIR = osutil.config_dir()
CACHE_DIR = osutil.cache_dir()

DB_PATH = Path(os.environ.get(
    "JOURNAL_WEB_DB", _web_cfg.get("db_path", str(REPO_ROOT / "data" / "webapp.db"))))

HOST = _web_cfg.get("host", "127.0.0.1")
PORT = int(_web_cfg.get("port", 8799))
# IANA zone name; empty = the zone of this machine
TIMEZONE = os.environ.get("JOURNAL_TZ") or _web_cfg.get("timezone", "")

AW_URL = _repo_cfg["aw_url"].rstrip("/")
AW_HOST = _repo_cfg.get("aw_host", "")
PI_SESSIONS_DIR = _repo_cfg.get("pi_dir")
CLAUDE_SESSIONS_DIR = _repo_cfg.get("claude_dir")
PROJECT_ROOTS = _repo_cfg.get("project_roots", [])
GH_BIN = _repo_cfg.get("gh_bin", "gh")
SP_URL = _repo_cfg.get("sp_url", "http://localhost:3030").rstrip("/")
SP_TOKEN_FILE = _repo_cfg.get("sp_token_file", "")
# service the 截屏 restart button restarts: systemd --user unit (Linux), launchd
# label (macOS) or scheduled-task name (Windows); empty = no button
SP_SERVICE = _repo_cfg.get("sp_service", "")
DATA_DIR = REPO_ROOT / "data"          # collected daily JSON, feishu map
REPORTS_DIR = REPO_ROOT / "reports"    # generated daily/weekly Markdown
SP_DATA_DIR = Path(_repo_cfg.get("sp_data_dir") or Path.home() / ".screenpipe" / "data")

# LLM (pi headless) — explicit opt-out possible; journal pipeline already uses it
LLM = _web_cfg.get("llm", {"enabled": False})
LLM_ENABLED = bool(LLM.get("enabled", False))
PI_BIN = _repo_cfg.get("pi_bin")

# privacy
PRIVACY = _web_cfg.get("privacy", {})
SENSITIVE_APPS = [a.lower() for a in PRIVACY.get("sensitive_apps", [])]

# project classification (generic defaults; user settings in webapp/config.json
# "classification" overlay; per-project rules live in the projects table)
_CLS_DEFAULT = {
    # app id (lower-case) → activity category for windows that match no project
    "app_categories": {
        "zotero": "文献阅读", "google-chrome": "网页浏览", "firefox": "网页浏览",
        "com.mitchellh.ghostty": "终端·其他", "org.gnome.ptyxis": "终端·其他",
        "org.gnome.texteditor": "文本编辑", "org.gnome.nautilus": "文件管理",
        "jetbrains-pycharm": "PyCharm", "warp-taskbar": "系统", "org.gnome.settings": "系统",
        "slack": "通信", "lark": "通信", "feishu": "通信",
    },
    # category → title keywords (case-insensitive substrings)
    "category_keywords": {"个人事务": ["滴答清单", "ta-da"]},
    # windows of apps not listed above
    "other_app_category": "其他应用",
    # directory names under project_roots that are not projects
    "skip_dirs": ["llm", "work", "tmp", "playground", "notes", "projects", "kaggle", "ext"],
    # one-time seeds merged into the projects table: name → {aliases, patterns, parent, ignore}
    "seed_projects": {},
}
_cls_user = _web_cfg.get("classification", {})
CLASSIFICATION = {**_CLS_DEFAULT, **_cls_user,
                  "app_categories": {**_CLS_DEFAULT["app_categories"], **_cls_user.get("app_categories", {})}}

# LLM classification of evidence-free window titles (OpenAI-compatible endpoint)
LLM_CLASSIFY = {"enabled": False, "model": "qwen3.8-max",
                "env_file": "",
                "min_confidence": 0.7, "days": 30, "batch": 25, "workers": 4,
                **_web_cfg.get("llm_classify", {})}
_secrets = osutil.load_env(LLM_CLASSIFY["env_file"] or None)
LLM_API_KEY = os.environ.get("BAILIAN_API_KEY") or _secrets.get("BAILIAN_API_KEY", "")
LLM_BASE_URL = (os.environ.get("BAILIAN_BASE_URL") or _secrets.get("BAILIAN_BASE_URL", "")).rstrip("/")
LLM_CLASSIFY_ENABLED = bool(LLM_CLASSIFY["enabled"] and LLM_API_KEY and LLM_BASE_URL
                            and not os.environ.get("JOURNAL_LLM_DISABLE"))

GIT_AUTHORS = list(_web_cfg.get("git_authors", []))

# 问 AI: local files / shell (read-only runs directly, writes need approval) and the web
CHAT = {"local_tools": True, "web": True, **_web_cfg.get("chat", {})}
# this machine's overlay; the chat's "set as default" writes chat.default here
WEB_LOCAL_CONFIG = WEBAPP_DIR / "config.local.json"
# AFK shorter than this (reading, watching an agent work) still counts as your time
AFK_MIN_SEC = int(_web_cfg.get("afk_min_minutes", 20)) * 60
RUNNING_THRESHOLD_SEC = int(_web_cfg.get("running_threshold_sec", 1800))

FRONTEND_DIST = WEBAPP_DIR / "frontend" / "dist"

# screen text: which OCR engine reads screenshots, and the built-in sampler
# that takes them where screenpipe is not used (see app/ocr.py, app/capture.py)
OCR = osutil._merge({"engine": "screenpipe", "reocr_screenpipe": False,
                     "openai": {"base_url": "", "model": "qwen3.5-ocr",
                                "api_key_env": "OCR_API_KEY", "base_url_env": "OCR_BASE_URL",
                                "timeout": 60, "max_tokens": 2048},
                     "http": {"url": "", "mode": "json", "text_path": "text", "headers": {}, "timeout": 60}},
                    _web_cfg.get("ocr", {}))
CAPTURE = {"enabled": False, "interval_sec": 60, "keep_days": 14, "max_width": 1600,
           "quality": 70, "skip_unchanged": True, **_web_cfg.get("capture", {})}
CAPTURE_DIR = Path(osutil.expand(CAPTURE.get("dir") or CACHE_DIR / "captures"))

# project classification reuses the journal's own matcher module (REPO_ROOT is on sys.path)

# ---- demo mode (`journal demo`): synthetic data in its own folder; never
# reads your sources, never calls a model or OCR API
DEMO_DIR = os.environ.get("JOURNAL_DEMO")
if DEMO_DIR:
    _demo = Path(DEMO_DIR)
    DB_PATH = _demo / "demo.db"
    DATA_DIR, REPORTS_DIR = _demo / "data", _demo / "reports"
    AW_URL, SP_URL, SP_SERVICE = "http://127.0.0.1:9", "", ""
    PI_SESSIONS_DIR, CLAUDE_SESSIONS_DIR = str(_demo / "pi"), str(_demo / "claude")
    PROJECT_ROOTS = [str(_demo / "projects")]
    GIT_AUTHORS = ["Demo Dev"]
    LLM_API_KEY = LLM_BASE_URL = ""
    LLM_ENABLED = LLM_CLASSIFY_ENABLED = False
    CLASSIFICATION = {**CLASSIFICATION, "seed_projects": {}}
    CAPTURE = {**CAPTURE, "enabled": False}
    OCR = {**OCR, "engine": "none"}
