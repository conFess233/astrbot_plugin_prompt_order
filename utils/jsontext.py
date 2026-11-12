"""文本里的 JSON 块压缩：去掉缩进与多余空白，内容和中文保持不变。"""

from __future__ import annotations

import json
import re

_DECODER = json.JSONDecoder()
# 只从行首的 [ 或 { 开始尝试解析，避免把正文里的 [图片] 之类当成 JSON。
_JSON_START = re.compile(r"(?m)^[ \t]*([\[{])")
# 太短的 JSON 片段多半是正文里顺带的，不值得动。
MIN_JSON_CHARS = 80


def minify_json(text: str) -> str:
    """压缩文本里从行首开始、能完整解析、至少 80 字的 JSON 块；其余文字原样保留。

    解析失败是正常情况（行首的 [ 多半只是普通文字），直接跳过，不视为错误。
    """

    out: list[str] = []
    pos = 0
    for m in _JSON_START.finditer(text):
        start = m.start(1)
        if start < pos:
            continue
        try:
            value, end = _DECODER.raw_decode(text, start)
        except ValueError:
            continue
        if not isinstance(value, (list, dict)) or end - start < MIN_JSON_CHARS:
            continue
        compact = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if len(compact) >= end - start:
            continue
        out.append(text[pos:start])
        out.append(compact)
        pos = end
    if not out:
        return text
    out.append(text[pos:])
    return "".join(out)
