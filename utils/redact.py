"""检查器落盘前的脱敏、媒体折叠与截断。"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

SECRET_HEADERS = {
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "api-key",
    "x-goog-api-key",
    "cookie",
}
KEPT_RESPONSE_HEADERS = (
    "x-request-id",
    "request-id",
    "x-ratelimit-",
    "anthropic-ratelimit-",
    "retry-after",
    "content-type",
    "openai-processing-ms",
)
_DATA_URI = re.compile(r"^data:([\w.+/-]+);base64,(.*)$", re.S)
# 含 URL 安全字母表（- 与 _）：Gemini SDK 发出的 inlineData.data 用的就是它。
_LONG_B64 = re.compile(r"^[A-Za-z0-9+/=_\-\s]{4096,}$")


def redact_headers(headers: Any) -> dict[str, str]:
    """密钥类请求头一律替换为 ***，不受任何开关影响。"""

    result: dict[str, str] = {}
    for key, value in _items(headers):
        name = str(key)
        result[name] = "***" if name.lower() in SECRET_HEADERS else str(value)
    return result


def keep_response_headers(headers: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in _items(headers):
        name = str(key).lower()
        if any(name.startswith(prefix) for prefix in KEPT_RESPONSE_HEADERS):
            result[name] = str(value)
    return result


def _items(headers: Any) -> list[tuple[Any, Any]]:
    if headers is None:
        return []
    try:
        return list(headers.items())
    except Exception:
        return []


def fold_media(value: Any) -> Any:
    """把 data URI 和超长 base64 字符串替换为带大小与摘要的占位文本。"""

    if isinstance(value, str):
        match = _DATA_URI.match(value)
        if match:
            return _placeholder(match.group(1), match.group(2))
        if len(value) >= 4096 and _LONG_B64.match(value):
            return _placeholder("base64", value)
        return value
    if isinstance(value, list):
        return [fold_media(item) for item in value]
    if isinstance(value, dict):
        return {key: fold_media(item) for key, item in value.items()}
    return value


def _placeholder(kind: str, payload: str) -> str:
    size = len(payload) * 3 // 4
    digest = hashlib.sha1(payload.encode("utf-8", "ignore")).hexdigest()[:10]
    return f"[{kind} {size // 1024}KB sha1:{digest}]"


def decode_body(raw: bytes | None, *, fold: bool, limit: int) -> tuple[Any, bool]:
    """把请求体解析成 JSON（失败时保留文本），超过上限时截断。

    Returns:
        (内容, 是否截断)
    """

    if not raw:
        return None, False
    try:
        data: Any = json.loads(raw)
    except Exception:
        text = raw[:limit].decode("utf-8", "replace")
        return text, len(raw) > limit
    if fold:
        data = fold_media(data)
    if len(raw) <= limit:
        # 折叠只会让内容变短，原始字节没超限就不必再序列化一遍检查长度。
        return data, False
    encoded = json.dumps(data, ensure_ascii=False)
    if len(encoded) > limit:
        return encoded[:limit], True
    return data, False


def to_jsonable(value: Any, depth: int = 0) -> Any:
    """把 SDK 对象尽量转成可序列化结构，用于记录原始 usage。"""

    if depth > 6:
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): to_jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v, depth + 1) for v in value]
    for attr in ("model_dump", "to_dict", "dict"):
        method = getattr(value, attr, None)
        if callable(method):
            try:
                return to_jsonable(method(), depth + 1)
            except Exception:
                continue
    data = getattr(value, "__dict__", None)
    if isinstance(data, dict):
        return {k: to_jsonable(v, depth + 1) for k, v in data.items() if not k.startswith("_")}
    return str(value)
