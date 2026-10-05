#!/usr/bin/env python3
"""Feishu activity collector: chats, messages, files (with content preview).

Uses lark-cli with the user identity. Reads only what the authorized
account can see.

Called from collect.py: collect(start, end) -> dict
"""
import datetime as dt
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
DATA = BASE / "data"
LARK = CFG["lark_bin"]

TEXT_EXTS = {".txt", ".md", ".csv", ".json", ".py", ".log", ".ts", ".js",
             ".html", ".xml", ".yml", ".yaml", ".toml", ".sql", ".sh", ".rst"}


def run_lark(args, timeout=90):
    """Run one lark-cli command, return parsed JSON or None."""
    r = subprocess.run([LARK] + args, capture_output=True, text=True, timeout=timeout)
    try:
        return json.loads(r.stdout)
    except (json.JSONDecodeError, TypeError):
        return None


def plain_text(msg):
    """Best-effort plain text of one message."""
    mtype = msg.get("msg_type")
    content = msg.get("content") or ""
    if mtype == "text":
        try:
            return json.loads(content).get("text", "")
        except json.JSONDecodeError:
            return content
    if mtype == "post":
        try:
            post = json.loads(content)
            parts = []
            for lang_block in (post.values() if isinstance(post, dict) else []):
                if not isinstance(lang_block, dict):
                    continue
                for para in lang_block.get("content") or []:
                    for el in para or []:
                        if isinstance(el, dict) and el.get("tag") == "text":
                            parts.append(el.get("text", ""))
            return " ".join(parts)
        except json.JSONDecodeError:
            return ""
    return ""


def file_meta(msg):
    """Return (name, file_key, size) for file-type messages, else None."""
    if msg.get("msg_type") != "file":
        return None
    try:
        d = json.loads(msg.get("content") or "{}")
        return d.get("file_name") or "?", d.get("file_key"), d.get("size_in_byte") or 0
    except json.JSONDecodeError:
        return None


def read_preview(path):
    """Short text preview of a downloaded file, or None for binaries."""
    ext = path.suffix.lower()
    size = path.stat().st_size
    if size > 30 * 1024 * 1024:
        return "(文件过大,跳过内容)"
    if ext in TEXT_EXTS:
        text = path.read_text(encoding="utf-8", errors="replace")
        return re.sub(r"\s+", " ", text)[:1200] or "(空文件)"
    if ext == ".pdf":
        if shutil.which("pdftotext"):
            r = subprocess.run(["pdftotext", "-l", "3", str(path), "-"],
                               capture_output=True, text=True, timeout=60)
            text = re.sub(r"\s+", " ", (r.stdout or "").strip())
            return text[:1200] or "(PDF 无可提取文本)"
        return "(PDF,未装 pdftotext)"
    if ext == ".docx":
        try:
            with zipfile.ZipFile(path) as z:
                xml = z.read("word/document.xml").decode("utf-8", errors="replace")
            text = re.sub(r"<[^>]+>", " ", xml)
            return re.sub(r"\s+", " ", text)[:1200]
        except (zipfile.BadZipFile, KeyError):
            return "(docx 解析失败)"
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
        return "(图片)"
    return f"(二进制 {ext or '无扩展名'}, {size} bytes)"


def collect(start, end):
    out = {"chats": [], "files": [], "error": None}
    # 1. chat list
    d = run_lark(["im", "+chat-list", "--types=p2p,group"])
    chats = ((d or {}).get("data") or {}).get("chats") or []
    if not chats:
        out["error"] = "无法获取会话列表(lark-cli 未授权或无会话)"
        return out
    my_id = CFG.get("feishu_my_open_id", "")
    s, e = start.isoformat(), end.isoformat()
    # 2. per-chat messages in range
    for chat in chats[: CFG.get("feishu_max_chats", 30)]:
        cid = chat.get("chat_id")
        name = chat.get("name") or "(未命名会话)"
        if not cid:
            continue
        m = run_lark(["im", "+chat-messages-list", "--chat-id", cid,
                      "--start", s, "--end", e, "--order", "asc",
                      "--no-reactions", "--page-all"])
        msgs = ((m or {}).get("data") or {}).get("messages") or []
        if not msgs:
            continue
        entry = {"chat": name, "mode": chat.get("chat_mode"), "total": len(msgs),
                 "mine": 0, "excerpts": [], "doc_links": 0}
        for msg in msgs:
            sender = msg.get("sender") or {}
            mine = sender.get("id") == my_id
            who = "我" if mine else (sender.get("name") or "他人")
            if mine:
                entry["mine"] += 1
            body = plain_text(msg)
            if body and re.search(r"feishu\.cn/(docx|wiki|sheets|base|slides)", body):
                entry["doc_links"] += 1
            fm = file_meta(msg)
            if fm:
                fname, fkey, fsize = fm
                entry["excerpts"].append(
                    f"{msg.get('create_time','')[-5:]} {who}: [文件] {fname} ({fsize}B)")
                out["files"].append({"name": fname, "key": fkey, "size": fsize,
                                     "chat": name, "from": who,
                                     "message_id": msg.get("message_id"),
                                     "time": msg.get("create_time")})
            elif body and len(entry["excerpts"]) < 5:
                entry["excerpts"].append(
                    f"{msg.get('create_time','')[-5:]} {who}: {body[:80]}")
        out["chats"].append(entry)
    # 3. download + read newest files (most recent first)
    out["files"].reverse()
    fdir = DATA / "feishu-files" / start.date().isoformat()
    for f in out["files"][: CFG.get("feishu_max_files", 8)]:
        if not f.get("key") or not f.get("message_id"):
            f["preview"] = "(缺少下载参数)"
            continue
        fdir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w.\-\u4e00-\u9fff]", "_", f["name"])[:80] or "file"
        dest = fdir / safe
        r = run_lark(["im", "+messages-resources-download",
                      "--message-id", f["message_id"], "--file-key", f["key"],
                      "--type", "file", "--output", str(dest)])
        ok = dest.exists()
        if not ok and r is not None:
            # fallback: default filename in cwd of lark-cli
            found = list(Path.cwd().glob(f["key"] + "*"))
            if found:
                shutil.move(str(found[0]), dest)
                ok = True
        f["preview"] = read_preview(dest) if ok else "(下载失败)"
        f["local"] = str(dest) if ok else ""
    return out
