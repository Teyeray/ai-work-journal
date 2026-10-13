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
SP_URL = _repo_cfg.get("sp_url", "http://localhost:3030").rstrip("/")
SP_TOKEN_FILE = _repo_cfg.get("sp_token_file", "")
# systemd --user unit the 截屏 restart button restarts (empty = no button)
SP_SERVICE = _repo_cfg.get("sp_service", "screenpipe.service")

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

def _load_env(path: str) -> dict:
    """KEY=VALUE lines (secrets live outside the repo, e.g. ~/.config/ai-work-journal/.env)."""
    out = {}
    try:
        for line in Path(path).expanduser().read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


# LLM classification of evidence-free window titles (OpenAI-compatible endpoint)
LLM_CLASSIFY = {"enabled": False, "model": "qwen3.8-max",
                "env_file": "~/.config/ai-work-journal/.env",
                "min_confidence": 0.7, "days": 30, "batch": 25, "workers": 4,
                **_web_cfg.get("llm_classify", {})}
_secrets = _load_env(LLM_CLASSIFY["env_file"])
LLM_API_KEY = os.environ.get("BAILIAN_API_KEY") or _secrets.get("BAILIAN_API_KEY", "")
LLM_BASE_URL = (os.environ.get("BAILIAN_BASE_URL") or _secrets.get("BAILIAN_BASE_URL", "")).rstrip("/")
LLM_CLASSIFY_ENABLED = bool(LLM_CLASSIFY["enabled"] and LLM_API_KEY and LLM_BASE_URL
                            and not os.environ.get("JOURNAL_LLM_DISABLE"))

GIT_AUTHORS = list(_web_cfg.get("git_authors", []))

RUNNING_THRESHOLD_SEC = int(_web_cfg.get("running_threshold_sec", 1800))

FRONTEND_DIST = WEBAPP_DIR / "frontend" / "dist"

# project classification: reuse the journal's own matcher module
import sys  # noqa: E402
sys.path.insert(0, str(REPO_ROOT))
