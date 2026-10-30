"""AstrBot 内置流程生成的提示词：用宿主常量和固定标题细分成可排序的片段。"""

from __future__ import annotations

from ..models.segment import BUILTIN_OWNER, Run

# (标记, 片段开头)。常量缺失（宿主版本差异）时跳过对应项。
_CONSTANT_MARKS = (
    ("safety_mode", "LLM_SAFETY_MODE_SYSTEM_PROMPT"),
    ("sandbox", "SANDBOX_MODE_PROMPT"),
    ("tool_call", "TOOL_CALL_PROMPT"),
    ("tool_call_skills", "TOOL_CALL_PROMPT_SKILLS_LIKE_MODE"),
    ("chatui_persona", "CHATUI_SPECIAL_DEFAULT_PERSONA_PROMPT"),
    ("chatui_genui", "CHATUI_INLINE_GENUI_SYSTEM_PROMPT"),
    ("live_mode", "LIVE_MODE_SYSTEM_PROMPT"),
    ("cron_woke", "PROACTIVE_AGENT_CRON_WOKE_SYSTEM_PROMPT"),
    ("background_woke", "BACKGROUND_TASK_RESULT_WOKE_SYSTEM_PROMPT"),
)
_HEADING_MARKS = (
    ("persona", "# Persona Instructions"),
    ("skills", "## Skills"),
    ("computer_use", "User has not enabled the Computer Use"),
)
# 本轮附加内容里由宿主加入的块。
TAIL_PREFIXES = (
    ("quote", "<Quoted Message>"),
    ("kb", "[Related Knowledge Base Results]"),
    ("image_caption_failed", "[Image Captioning Failed]"),
    ("system_reminder", "<system_reminder>"),
)
MARK_LABELS = {
    "safety_mode": "安全模式",
    "sandbox": "沙箱模式",
    "tool_call": "工具调用提示",
    "tool_call_skills": "工具调用提示（精简）",
    "chatui_persona": "ChatUI 默认人格",
    "chatui_genui": "ChatUI 内联 GenUI",
    "live_mode": "Live 模式",
    "cron_woke": "定时任务唤醒",
    "background_woke": "后台任务唤醒",
    "web_search_citation": "联网搜索引用规范",
    "persona": "人格",
    "skills": "技能",
    "computer_use": "Computer Use 说明",
    "builtin": "内置（未细分）",
    "quote": "引用消息",
    "kb": "知识库结果",
    "image_caption_failed": "图片描述失败",
    "system_reminder": "系统提醒",
}

_ANCHORS: list[tuple[str, str]] | None = None


def anchors() -> list[tuple[str, str]]:
    """返回 (标记, 开头片段) 列表；首次调用时从宿主常量加载。"""

    global _ANCHORS
    if _ANCHORS is not None:
        return _ANCHORS
    found: list[tuple[str, str]] = []
    try:
        from astrbot.core import astr_main_agent_resources as resources

        for mark, name in _CONSTANT_MARKS:
            text = getattr(resources, name, None)
            if isinstance(text, str) and len(text.strip()) >= 24:
                found.append((mark, text.strip()[:48]))
    except Exception:
        pass
    try:
        from astrbot.core.astr_main_agent import WEB_SEARCH_CITATION_PROMPT

        if isinstance(WEB_SEARCH_CITATION_PROMPT, str) and WEB_SEARCH_CITATION_PROMPT.strip():
            found.append(("web_search_citation", WEB_SEARCH_CITATION_PROMPT.strip()[:48]))
    except Exception:
        pass
    found.extend(_HEADING_MARKS)
    _ANCHORS = found
    return found


def split_builtin(text: str) -> list[Run]:
    """把宿主生成的基线 system 按已知开头切开，认不出的部分标为 builtin。"""

    hits: list[tuple[int, str]] = []
    for mark, head in anchors():
        idx = text.find(head)
        if idx >= 0:
            hits.append((idx, mark))
    if not hits:
        return [Run(BUILTIN_OWNER, text, "builtin")] if text else []
    hits.sort()
    runs: list[Run] = []
    # 片段从锚点所在行的行首开始，把前导换行一并带上。
    starts: list[tuple[int, str]] = []
    for idx, mark in hits:
        line_start = text.rfind("\n", 0, idx)
        pos = 0 if line_start < 0 else line_start
        if not starts or starts[-1][0] != pos:
            starts.append((pos, mark))
    if starts[0][0] > 0:
        runs.append(Run(BUILTIN_OWNER, text[: starts[0][0]], "builtin"))
    else:
        starts[0] = (0, starts[0][1])
    for pos, (start, mark) in enumerate(starts):
        end = starts[pos + 1][0] if pos + 1 < len(starts) else len(text)
        if end > start:
            runs.append(Run(BUILTIN_OWNER, text[start:end], mark))
    return runs


def tail_mark(text: str) -> str:
    """识别宿主加入的附加块；不是已知块时返回空字符串。"""

    head = text.lstrip()[:64]
    for mark, prefix in TAIL_PREFIXES:
        if head.startswith(prefix):
            return mark
    return ""
