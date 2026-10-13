"""OCR engines: turn a screenshot into text.

    screenpipe  text comes from screenpipe's own on-device OCR (nothing to do here)
    openai      any OpenAI-compatible vision model: Alibaba Bailian qwen-vl-ocr,
                OpenAI gpt-4o-mini, a local Ollama / vLLM / LM Studio server …
    http        your own OCR service (PaddleOCR, RapidOCR, Tesseract behind a
                small HTTP wrapper …): POST the image, read the text from JSON
    none        no OCR

Configured in webapp/config.json "ocr" (see config.OCR). Keys are read from the
environment or <config dir>/.env, never from the repo.
"""
import base64
import json
import re
import time
import urllib.error
import urllib.request

from . import config

API_ENGINES = ("openai", "http")
PROMPT = ("Read all text visible in this screenshot. Output plain text only, line by line in "
          "reading order, keeping the original language (Chinese, English, code …). "
          "No descriptions, no markdown, no commentary.")


# some OCR models prefix each line with its box: "196,158,89,301,90,text"
_BOX = re.compile(r"^\s*(?:-?\d+(?:\.\d+)?,){4,8}\s*", re.M)


class OcrError(RuntimeError):
    pass


_FENCE = re.compile(r"^```[\w-]*\s*$", re.M)
_HTML = re.compile(r"</?(?:html|body|head|p|h[1-6]|li|ol|ul|div|span|table|thead|tbody|tr|td|th|br|pre|code)\b[^>]*>", re.I)
_BLOCK = re.compile(r"<(?:br\s*/?|/p|/h[1-6]|/li|/div|/tr|/pre)>", re.I)


def clean(text: str) -> str:
    """Plain text from what OCR models return: drops box coordinates, code
    fences and the HTML/markdown layout some models wrap the text in."""
    import html
    t = _FENCE.sub("", _BOX.sub("", text or ""))
    if _HTML.search(t):
        t = _BLOCK.sub("\n", t)
        t = re.sub(r"<td[^>]*>", " ", t, flags=re.I)
        t = html.unescape(re.sub(r"<[^>]+>", "", t))
    lines = [re.sub(r"[ \t]+", " ", l).strip() for l in t.splitlines()]
    return "\n".join(l for l in lines if l)


def engine() -> str:
    return (config.OCR.get("engine") or "none").lower()


def is_api() -> bool:
    return engine() in API_ENGINES


def _openai_target() -> tuple[str, str, str]:
    """(base_url, api_key, model). Falls back to the Bailian key the rest of
    the journal uses, so one key covers chat, classification and OCR."""
    o = config.OCR.get("openai", {})
    sec = config.osutil.secret
    key = sec(o.get("api_key_env") or "OCR_API_KEY") or config.LLM_API_KEY
    base = (o.get("base_url") or sec(o.get("base_url_env") or "OCR_BASE_URL") or config.LLM_BASE_URL).rstrip("/")
    return base, key, o.get("model") or "qwen3.5-ocr"


def configured() -> tuple[bool, str]:
    """Is the selected engine usable? (ok, what is missing)"""
    e = engine()
    if e == "openai":
        base, key, _ = _openai_target()
        if not base:
            return False, "ocr.openai.base_url 未设置(或环境变量 OCR_BASE_URL)"
        if not key and "localhost" not in base and "127.0.0.1" not in base:
            return False, "缺少 OCR_API_KEY(环境变量或 .env)"
        return True, ""
    if e == "http":
        return (True, "") if config.OCR.get("http", {}).get("url") else (False, "ocr.http.url 未设置")
    return e in ("screenpipe", "none"), ""


def _post(url: str, body: bytes, headers: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise OcrError(f"HTTP {e.code}: {detail}") from e
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise OcrError(f"{type(e).__name__}: {e}") from e


def _openai(image: bytes, mime: str) -> str:
    o = config.OCR.get("openai", {})
    base, key, model = _openai_target()
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    data_url = f"data:{mime};base64," + base64.b64encode(image).decode("ascii")
    body = {"model": model, "temperature": 0, "max_tokens": int(o.get("max_tokens", 2048)),
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": o.get("prompt") or PROMPT}]}]}
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    r = _post(url, json.dumps(body).encode("utf-8"), headers, float(o.get("timeout", 60)))
    try:
        content = r["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise OcrError(f"unexpected response: {str(r)[:200]}") from e
    if isinstance(content, list):   # some servers return content parts
        content = "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    return clean(content)


def _dig(obj, path: str):
    """'data.0.text' → obj['data'][0]['text']; a list of strings/objects at the
    end is joined line by line (PaddleOCR-style results)."""
    for part in [p for p in path.split(".") if p]:
        if isinstance(obj, list) and part.isdigit():
            obj = obj[int(part)] if int(part) < len(obj) else None
        elif isinstance(obj, list):
            obj = [x.get(part) if isinstance(x, dict) else None for x in obj]
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    if isinstance(obj, list):
        return "\n".join(str(x) for x in obj if x not in (None, ""))
    return obj


def _http(image: bytes, mime: str) -> str:
    h = config.OCR.get("http", {})
    headers = {k: config.osutil.expand(v) for k, v in (h.get("headers") or {}).items()}
    if h.get("mode", "json") == "raw":
        body, headers["Content-Type"] = image, mime
    else:
        body = json.dumps({"image": base64.b64encode(image).decode("ascii"), "mime": mime}).encode("utf-8")
        headers["Content-Type"] = "application/json"
    r = _post(h["url"], body, headers, float(h.get("timeout", 60)))
    text = _dig(r, h.get("text_path") or "text")
    if text is None:
        raise OcrError(f"no text at '{h.get('text_path')}' in {str(r)[:200]}")
    return str(text).strip()


def recognize(image: bytes, mime: str = "image/jpeg") -> dict:
    """{text, engine, ms}. Raises OcrError when the engine fails or is not an API one."""
    e = engine()
    ok, why = configured()
    if e not in API_ENGINES:
        raise OcrError(f"OCR engine '{e}' does not read images (set ocr.engine to openai or http)")
    if not ok:
        raise OcrError(why)
    t0 = time.monotonic()
    text = _openai(image, mime) if e == "openai" else _http(image, mime)
    return {"text": text, "engine": e, "ms": int((time.monotonic() - t0) * 1000)}


def describe() -> dict:
    """For /api/v1/screen/status and `journal doctor`."""
    e = engine()
    ok, why = configured()
    out = {"engine": e, "configured": ok, "problem": why or None}
    if e == "openai":
        base, key, model = _openai_target()
        out.update(model=model, base_url=base, has_key=bool(key))
    elif e == "http":
        out["url"] = config.OCR.get("http", {}).get("url")
    return out
