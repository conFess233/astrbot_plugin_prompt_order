"""片段签名：取第一行并抹掉每轮都会变的数字和 ID，用于跨请求识别同一片段。"""

from __future__ import annotations

import json
import re
from typing import Any

_VOLATILE = re.compile(r"[0-9a-fA-F]{8,}|\d+")
_SPACES = re.compile(r"\s+")
MAX_LEN = 32


def signature(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            line = _VOLATILE.sub("#", line)
            line = _SPACES.sub(" ", line)
            return line[:MAX_LEN]
    return "(空)"


def make_key(owner: str, sig: str) -> str:
    return f"{owner}/{sig}"


def tool_definition(tool: Any) -> str:
    """工具的完整定义（名字、说明、参数），用来发现每轮都在变的工具。"""

    data = {
        "name": str(getattr(tool, "name", "")),
        "description": str(getattr(tool, "description", "") or ""),
        "parameters": getattr(tool, "parameters", None),
    }
    return json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
