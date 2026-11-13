"""历史压缩的纯逻辑：切分历史回合、压缩提醒块、工具结果的摘要替换与规则截取。

不依赖 AstrBot，便于离线回放和单元测试。所有改写都是确定性的：同一段历史无论压缩几次，
结果都一样；已压缩的内容带标记，不会被二次处理。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any

# 工具结果被改写后的统一前缀；带上原文字数，之后每次请求都能据此估算节省量。
TOOL_MARK = "[历史工具结果·"
DIGEST_HEAD = TOOL_MARK + "摘要｜{n} 条结果原文共 {chars} 字，插件已提炼，细节需重新调用工具]\n"
MERGED_HEAD = TOOL_MARK + "已并入｜原文 {chars} 字，内容见本轮第一条工具结果的摘要]"
TRIM_HEAD = TOOL_MARK + "节选｜原文 {chars} 字，插件按规则截取，细节需重新调用工具]\n"
_MARK_CHARS = re.compile(r"原文(?:共)? (\d+) 字")

REMINDER_RE = re.compile(r"<system_reminder>(.*?)</system_reminder>", re.S)
_WHO_RE = re.compile(r"^User ID: (.*?), Nickname: (.*)$")
_WHEN_RE = re.compile(r"^Current datetime: (\d{4}-\d{2}-\d{2} \d{2}:\d{2})")
# 压缩后的提醒块：[昵称(ID) 2026-11-11 14:55 …]，用于估算已压缩提醒块的节省量。
_SHORT_REMINDER_RE = re.compile(r"\[[^\[\]\n]*\(\S+\) \d{4}-\d{2}-\d{2} \d{2}:\d{2}[^\[\]\n]*\]")
# 宿主提醒块的固定部分（不含昵称、ID、时间）大约这么长，减去压缩后的括号。
REMINDER_OVERHEAD = len(
    "<system_reminder>User ID: , Nickname: \nCurrent datetime:  (CST), Weekday: Wednesday"
    "</system_reminder>"
) - len("[() ]")

KB_HEAD = "以下是相关的知识库内容"
_KB_SECTION_RE = re.compile(
    r"【知识 \d+】\n来源: (?P<src>[^\n]*)\n内容: (?P<body>.*?)(?:\n相关度: (?P<score>[\d.]+))?"
    r"(?=\n+【知识 \d+】|\s*$)",
    re.S,
)
_FRONT_MATTER_RE = re.compile(r"^---\n(?P<yaml>.*?)\n---\n?", re.S)
_YAML_TITLE_RE = re.compile(r"^title:\s*(.+)$", re.M)


# ---------- 消息读写 ----------


def content_text(content: Any) -> str:
    """消息内容里的全部文本；图片等非文本部分忽略。"""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                out.append(str(part.get("text") or ""))
        return "".join(out)
    return ""


def as_dict(message: Any) -> dict[str, Any] | None:
    """运行时的 Message 对象转字典；本身是字典的原样返回，其他类型返回 None。

    model_dump 出错不在这里吞掉，交给钩子边界记录。
    """

    if isinstance(message, dict):
        return message
    dump = getattr(message, "model_dump", None)
    if callable(dump):
        data = dump()
        return data if isinstance(data, dict) else None
    return None


def _tool_call_fields(call: Any) -> tuple[str, str, str]:
    if not isinstance(call, dict):
        call = as_dict(call) or {}
    fn = call.get("function") or {}
    if not isinstance(fn, dict):
        fn = as_dict(fn) or {}
    args = fn.get("arguments")
    if not isinstance(args, str):
        args = json.dumps(args, ensure_ascii=False) if args is not None else ""
    return str(call.get("id") or ""), str(fn.get("name") or ""), args


# ---------- 回合切分 ----------


@dataclass
class ToolItem:
    msg: dict[str, Any]
    name: str
    args: str
    text: str


@dataclass
class Block:
    """一条用户消息及其后的助手消息与工具结果。"""

    user_index: int
    question: str = ""
    answer: str = ""
    tools: list[ToolItem] = field(default_factory=list)


def _question_of(content: Any) -> str:
    """用户原话：去掉插件注入的 <…> 块和图片目录块。"""

    parts: list[str] = []
    if isinstance(content, str):
        parts = [REMINDER_RE.sub("", content)]
    elif isinstance(content, list):
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "text":
                continue
            text = str(part.get("text") or "").strip()
            if not text or text.startswith("<") or text.startswith("[ContextAware"):
                continue
            parts.append(REMINDER_RE.sub("", text))
    return " ".join(p.strip() for p in parts if p.strip())


def split_blocks(messages: list[Any]) -> list[Block]:
    blocks: list[Block] = []
    current: Block | None = None
    names: dict[str, tuple[str, str]] = {}
    for index, raw in enumerate(messages):
        msg = as_dict(raw)
        if msg is None:
            continue
        role = msg.get("role")
        if role == "user":
            current = Block(user_index=index, question=_question_of(msg.get("content")))
            blocks.append(current)
            continue
        if current is None:
            current = Block(user_index=-1)
            blocks.append(current)
        if role == "assistant":
            for call in msg.get("tool_calls") or []:
                call_id, name, args = _tool_call_fields(call)
                names[call_id] = (name, args)
            text = content_text(msg.get("content")).strip()
            if text and not msg.get("tool_calls"):
                current.answer = text
        elif role == "tool":
            name, args = names.get(str(msg.get("tool_call_id") or ""), ("", ""))
            current.tools.append(ToolItem(msg, name, args, content_text(msg.get("content"))))
    return blocks


# ---------- 选择与键 ----------


@dataclass(frozen=True)
class ToolRules:
    min_chars: int = 600
    block_min_chars: int = 1200
    exclude: tuple[str, ...] = ()
    snippet_chars: int = 150


def is_marked(text: str) -> bool:
    return text.startswith(TOOL_MARK)


def targets_of(block: Block, rules: ToolRules) -> list[ToolItem]:
    """本回合里值得压缩的工具结果；合计不够长时整回合跳过。"""

    picked = [
        t
        for t in block.tools
        if t.text
        and not is_marked(t.text)
        and len(t.text) >= rules.min_chars
        and not any(fnmatchcase(t.name, p) for p in rules.exclude)
    ]
    if sum(len(t.text) for t in picked) < rules.block_min_chars:
        return []
    return picked


def block_key(targets: list[ToolItem]) -> str:
    h = hashlib.sha1()
    for t in targets:
        h.update(t.name.encode("utf-8", "ignore"))
        h.update(b"\x1e")
        h.update(t.text.encode("utf-8", "ignore"))
        h.update(b"\x1f")
    return h.hexdigest()


# ---------- 改写 ----------


def digest_contents(targets: list[ToolItem], digest: str) -> list[str]:
    """摘要放进第一条结果，其余标记为已并入；条数与顺序不变，工具调用配对不受影响。"""

    total = sum(len(t.text) for t in targets)
    out = [DIGEST_HEAD.format(n=len(targets), chars=total) + digest.strip()]
    out += [MERGED_HEAD.format(chars=len(t.text)) for t in targets[1:]]
    return out


def _cut(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _trim_search(data: dict[str, Any], limit: int) -> str | None:
    results = data.get("results")
    if not isinstance(results, list) or not results:
        return None
    lines = []
    for i, item in enumerate(results, 1):
        if not isinstance(item, dict):
            continue
        title = _cut(str(item.get("title") or ""), 60)
        url = str(item.get("url") or item.get("link") or "")
        body = str(item.get("snippet") or item.get("content") or item.get("summary") or "")
        lines.append(f"{i}. {title} {url}".rstrip())
        if body:
            lines.append("   " + _cut(body, limit))
    return "\n".join(lines) if lines else None


def _strip_front_matter(body: str) -> str:
    m = _FRONT_MATTER_RE.match(body.lstrip())
    if not m:
        return body
    rest = body.lstrip()[m.end() :].strip()
    if rest:
        return rest
    title = _YAML_TITLE_RE.search(m.group("yaml"))
    return f"（文档头：{title.group(1).strip()}）" if title else ""


def _trim_kb(text: str, limit: int, seen: set[str]) -> str | None:
    if not text.startswith(KB_HEAD):
        return None
    lines = []
    for m in _KB_SECTION_RE.finditer(text):
        src = m.group("src").strip()
        body = _strip_front_matter(m.group("body"))
        sig = src + "\x1f" + _cut(body, 80)
        if sig in seen:
            lines.append(f"- {src}（与前面重复，略）")
            continue
        seen.add(sig)
        score = f" 相关度 {m.group('score')}" if m.group("score") else ""
        lines.append(f"- {src}{score}：{_cut(body, limit * 2)}")
    return "\n".join(lines) if lines else None


def trim_content(item: ToolItem, snippet_chars: int, seen: set[str]) -> str:
    """没有摘要可用时的规则截取；结果不比原文短就保持原样。"""

    text = item.text
    body = _trim_kb(text, snippet_chars, seen)
    if body is None and text.lstrip().startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            body = _trim_search(data, snippet_chars)
    if body is None:
        keep = snippet_chars * 3
        body = _cut(text, keep) if len(text) > keep else text
    new = TRIM_HEAD.format(chars=len(text)) + body
    return new if len(new) < len(text) else text


# ---------- 提醒块 ----------


def compress_reminder(body: str) -> str | None:
    """把宿主的 <system_reminder> 正文压成 [昵称(ID) 日期 时间]；认不出时返回 None。"""

    who = when = ""
    rest: list[str] = []
    for line in body.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        m = _WHO_RE.match(line)
        if m:
            who = f"{m.group(2).strip()}({m.group(1).strip()})"
            continue
        m = _WHEN_RE.match(line)
        if m:
            when = m.group(1)
            continue
        if line.startswith("Group name: "):
            continue
        rest.append(line)
    if not who or not when:
        return None
    return "[" + " ".join([who, when, *rest]) + "]"


def _shrink_reminders(text: str) -> str:
    def repl(m: re.Match) -> str:
        short = compress_reminder(m.group(1))
        return short if short is not None else m.group(0)

    return REMINDER_RE.sub(repl, text)


def reminder_edits(msg: dict[str, Any]) -> tuple[Any, int, int] | None:
    """返回 (新内容, 原字数, 新字数)；没有可压缩的提醒块返回 None。"""

    content = msg.get("content")
    if isinstance(content, str):
        if "<system_reminder>" not in content:
            return None
        new = _shrink_reminders(content)
        return (new, len(content), len(new)) if new != content else None
    if not isinstance(content, list):
        return None
    changed = False
    before = after = 0
    parts = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            text = str(part.get("text") or "")
            if "<system_reminder>" in text:
                new = _shrink_reminders(text)
                if new != text:
                    part = {**part, "text": new}
                    before += len(text)
                    after += len(new)
                    changed = True
        parts.append(part)
    return (parts, before, after) if changed else None


# ---------- 整体计划 ----------


@dataclass
class Edit:
    msg: dict[str, Any]
    content: Any


@dataclass
class Plan:
    """一次请求的压缩计划；dry-run 时只计算不应用。"""

    edits: list[Edit] = field(default_factory=list)
    reminders: int = 0
    reminder_saved: int = 0
    digest_blocks: int = 0
    trim_items: int = 0
    tool_saved: int = 0
    # 已经压缩过（写进历史）的内容，在本次请求里相对原文少发的字数。
    standing_reminders: int = 0
    standing_tool_saved: int = 0
    keys_used: list[str] = field(default_factory=list)
    keys_trimmed: list[str] = field(default_factory=list)

    @property
    def new_saved(self) -> int:
        return self.reminder_saved + self.tool_saved

    @property
    def total_saved(self) -> int:
        return (
            self.new_saved + self.standing_tool_saved + self.standing_reminders * REMINDER_OVERHEAD
        )

    def apply(self) -> int:
        for edit in self.edits:
            edit.msg["content"] = edit.content
        return len(self.edits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reminders": self.reminders,
            "reminder_saved": self.reminder_saved,
            "digest_blocks": self.digest_blocks,
            "trim_items": self.trim_items,
            "tool_saved": self.tool_saved,
            "standing_reminders": self.standing_reminders,
            "standing_tool_saved": self.standing_tool_saved,
            "new_saved": self.new_saved,
            "total_saved": self.total_saved,
        }


def standing_tool_saving(text: str) -> int:
    if not is_marked(text):
        return 0
    m = _MARK_CHARS.search(text[:120])
    return max(0, int(m.group(1)) - len(text)) if m else 0


def plan_compaction(
    messages: list[Any],
    *,
    reminders: bool,
    tools: bool,
    rules: ToolRules,
    digests: dict[str, str],
) -> Plan:
    """对整段历史做压缩计划。digests 为 回合键 → 摘要 的已完成摘要。"""

    plan = Plan()
    blocks = split_blocks(messages)
    if reminders:
        for raw in messages:
            msg = as_dict(raw)
            if msg is None or msg.get("role") != "user" or msg is not raw:
                continue
            text = content_text(msg.get("content"))
            plan.standing_reminders += len(_SHORT_REMINDER_RE.findall(text))
            edit = reminder_edits(msg)
            if edit is not None:
                new, before, after = edit
                plan.edits.append(Edit(msg, new))
                plan.reminders += 1
                plan.reminder_saved += before - after
    for block in blocks:
        for t in block.tools:
            plan.standing_tool_saved += standing_tool_saving(t.text)
        if not tools:
            continue
        targets = targets_of(block, rules)
        if not targets:
            continue
        key = block_key(targets)
        digest = digests.get(key)
        before = sum(len(t.text) for t in targets)
        if digest:
            contents = digest_contents(targets, digest)
            after = sum(len(c) for c in contents)
            if after < before:
                plan.edits += [Edit(t.msg, c) for t, c in zip(targets, contents, strict=True)]
                plan.digest_blocks += 1
                plan.tool_saved += before - after
                plan.keys_used.append(key)
                continue
        seen: set[str] = set()
        trimmed = False
        for t in targets:
            new = trim_content(t, rules.snippet_chars, seen)
            if new != t.text:
                plan.edits.append(Edit(t.msg, new))
                plan.trim_items += 1
                plan.tool_saved += len(t.text) - len(new)
                trimmed = True
        if trimmed:
            plan.keys_trimmed.append(key)
    return plan


def pending_keys(messages: list[Any], rules: ToolRules, known: set[str]) -> list[str]:
    """历史里待压缩、但还没有摘要的回合键（用于等待进行中的提炼）。"""

    keys = []
    for block in split_blocks(messages):
        targets = targets_of(block, rules)
        if targets:
            key = block_key(targets)
            if key not in known:
                keys.append(key)
    return keys
