"""Language of generated text (insights, status reasons, 问 AI answers).

The dashboard mirrors its 中/EN toggle into a `lang` cookie; a middleware in
api.py sets it per request. Data keys (category labels such as 网页浏览) stay
Chinese in the database — only text written for people is localized.
"""
import contextvars

_LANG = contextvars.ContextVar("lang", default="zh")


def set_lang(v: str):
    return _LANG.set("en" if (v or "").lower().startswith("en") else "zh")


def lang() -> str:
    return _LANG.get()


def tr(zh: str, en: str) -> str:
    return en if _LANG.get() == "en" else zh


# built-in category/lane names (data keys) → English, mirrored in frontend/src/i18n.ts
LABELS = {
    "其他活动": "Other activity", "其他应用": "Other apps", "终端·其他": "Terminal · other",
    "网页浏览": "Web browsing", "文献阅读": "Paper reading", "文本编辑": "Text editing",
    "文件管理": "File manager", "个人事务": "Personal", "系统": "System", "通信": "Chat",
    "阅读": "Reading", "其他": "Other", "未知应用": "Unknown app",
}


def label(s: str) -> str:
    """Display name of a data label; project names pass through."""
    return LABELS.get(s, s) if _LANG.get() == "en" else s
