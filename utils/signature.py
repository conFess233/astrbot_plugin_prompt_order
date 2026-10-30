"""片段签名：取第一行并抹掉每轮都会变的数字和 ID，用于跨请求识别同一片段。"""

from __future__ import annotations

import re

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
