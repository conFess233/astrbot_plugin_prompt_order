"""单次请求的来源追踪：基线快照、每个钩子前后的差异归属，以及切分成片段。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..models.layout import Layout
from ..models.segment import (
    BUILTIN_OWNER,
    EXTERNAL_OWNER,
    HookChange,
    Run,
    Segment,
)
from ..utils.signature import make_key, signature
from ..utils.textdiff import apply_change, runs_text, split_by_anchors
from .builtin_marks import split_builtin, tail_mark


@dataclass(slots=True)
class Snapshot:
    """钩子执行前的请求状态；只保存引用和摘要，不复制内容。"""

    system: str
    parts: list[Any]
    tools: list[str]
    prompt: str
    contexts_id: int
    contexts_len: int


@dataclass(slots=True)
class PartInfo:
    owner: str
    mark: str = ""


@dataclass
class RequestState:
    """一次主对话请求在构建阶段的追踪状态。"""

    umo: str
    runs: list[Run] = field(default_factory=list)
    parts: dict[int, PartInfo] = field(default_factory=dict)
    # 保留 part 引用，防止对象被回收后 id 被复用。
    part_refs: list[Any] = field(default_factory=list)
    tools: dict[str, str] = field(default_factory=dict)
    changes: list[HookChange] = field(default_factory=list)
    arranged: bool = False
    degraded: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    before_system: str = ""
    before_tail: list[dict[str, Any]] = field(default_factory=list)
    after_system: str = ""
    before_runs: list[Run] = field(default_factory=list)
    after_tail: list[dict[str, Any]] = field(default_factory=list)
    arrange_note: str = ""
    layout_id: str = ""
    trace: Any = None


def part_text(part: Any) -> str:
    text = getattr(part, "text", None)
    if isinstance(text, str):
        return text
    if isinstance(part, dict) and isinstance(part.get("text"), str):
        return part["text"]
    return ""


def part_is_media(part: Any) -> bool:
    kind = getattr(part, "type", None)
    if kind is None and isinstance(part, dict):
        kind = part.get("type")
    return kind not in (None, "text")


def part_is_temp(part: Any) -> bool:
    if isinstance(part, dict):
        return bool(part.get("_no_save"))
    return bool(getattr(part, "_no_save", False))


def tool_names(req: Any) -> list[str]:
    func_tool = getattr(req, "func_tool", None)
    tools = getattr(func_tool, "tools", None) or []
    return [str(getattr(tool, "name", "")) for tool in tools]


def take_snapshot(req: Any) -> Snapshot:
    contexts = getattr(req, "contexts", None)
    return Snapshot(
        system=req.system_prompt or "",
        parts=list(req.extra_user_content_parts or []),
        tools=tool_names(req),
        prompt=req.prompt or "",
        contexts_id=id(contexts),
        contexts_len=len(contexts) if isinstance(contexts, list) else 0,
    )


class Tracker:
    """维护来源表；所有方法都不抛出异常给调用方之外的钩子。"""

    def __init__(self, owner_of_tool, max_chars: int = 50000) -> None:
        self._owner_of_tool = owner_of_tool
        self.max_chars = max_chars

    # ---------- 基线 ----------

    def begin(self, umo: str, req: Any) -> RequestState:
        state = RequestState(umo=umo)
        state.runs = split_builtin(req.system_prompt or "")
        for part in list(req.extra_user_content_parts or []):
            self.own_part(state, part, BUILTIN_OWNER, mark=tail_mark(part_text(part)))
        func_tool = getattr(req, "func_tool", None)
        for tool in getattr(func_tool, "tools", None) or []:
            name = str(getattr(tool, "name", ""))
            state.tools[name] = self._owner_of_tool(tool) or BUILTIN_OWNER
        return state

    def own_part(self, state: RequestState, part: Any, owner: str, mark: str = "") -> None:
        state.parts[id(part)] = PartInfo(owner, mark)
        state.part_refs.append(part)

    # ---------- 钩子前后对比 ----------

    def record(
        self, state: RequestState, before: Snapshot, req: Any, owner: str, handler: str
    ) -> None:
        after_arrange = state.arranged
        system = req.system_prompt or ""
        if system != before.system:
            if runs_text(state.runs) != before.system:
                # 上一个钩子结束到这个钩子开始之间有人改过，先把那部分记到钩子外。
                state.runs, _, _ = apply_change(
                    state.runs, before.system, EXTERNAL_OWNER, max_chars=self.max_chars
                )
            self._record_system(state, system, owner, handler, after_arrange)
        parts = list(req.extra_user_content_parts or [])
        if [id(p) for p in parts] != [id(p) for p in before.parts]:
            self._record_parts(state, before.parts, parts, owner, handler, after_arrange)
        tools = tool_names(req)
        if tools != before.tools:
            self._record_tools(state, before.tools, req, owner, handler, after_arrange)
        if (req.prompt or "") != before.prompt:
            state.changes.append(
                HookChange(owner, handler, "prompt", "改写了用户原话", after_arrange)
            )
        contexts = getattr(req, "contexts", None)
        ctx_len = len(contexts) if isinstance(contexts, list) else 0
        if id(contexts) != before.contexts_id or ctx_len != before.contexts_len:
            state.changes.append(
                HookChange(
                    owner,
                    handler,
                    "contexts",
                    f"改写了历史：{before.contexts_len} → {ctx_len} 条",
                    after_arrange,
                )
            )

    def _record_system(
        self, state: RequestState, system: str, owner: str, handler: str, late: bool
    ) -> None:
        runs, touched, path = apply_change(state.runs, system, owner, max_chars=self.max_chars)
        state.runs = runs
        if path == "whole":
            state.degraded.append(f"{owner} 整体改写了 system，超过对比上限，未细分")
        if touched:
            detail = "、".join(f"{who} {n} 字" for who, n in touched.items())
            summary = f"删改了 {detail}"
        elif path == "diff":
            summary = "在 system 中间插入了内容"
        else:
            summary = "在 system 开头或末尾追加了内容"
        state.changes.append(HookChange(owner, handler, "system", summary, late))

    def _record_parts(
        self,
        state: RequestState,
        old: list[Any],
        new: list[Any],
        owner: str,
        handler: str,
        late: bool,
    ) -> None:
        old_ids = {id(p) for p in old}
        new_ids = {id(p) for p in new}
        removed = [p for p in old if id(p) not in new_ids]
        added = [p for p in new if id(p) not in old_ids]
        # 文本相同的“替换”保留原来源，记为改写。
        pool: dict[str, list[Any]] = {}
        for part in removed:
            pool.setdefault(part_text(part), []).append(part)
        rewritten = 0
        fresh = 0
        for part in added:
            twins = pool.get(part_text(part))
            if twins and part_text(part):
                origin = state.parts.get(id(twins.pop(0)), PartInfo(EXTERNAL_OWNER))
                self.own_part(state, part, origin.owner, origin.mark)
                rewritten += 1
            else:
                self.own_part(state, part, owner, tail_mark(part_text(part)))
                fresh += 1
        dropped = sum(len(v) for v in pool.values())
        bits = []
        if fresh:
            bits.append(f"新增 {fresh} 块")
        if rewritten:
            bits.append(f"改写 {rewritten} 块")
        if dropped:
            victims = sorted(
                {
                    state.parts.get(id(p), PartInfo(EXTERNAL_OWNER)).owner
                    for v in pool.values()
                    for p in v
                }
            )
            bits.append(f"删除 {dropped} 块（来自 {'、'.join(victims)}）")
        if not bits:
            bits.append("调整了顺序")
        state.changes.append(HookChange(owner, handler, "tail", "，".join(bits), late))

    def _record_tools(
        self, state: RequestState, before: list[str], req: Any, owner: str, handler: str, late: bool
    ) -> None:
        func_tool = getattr(req, "func_tool", None)
        now = {str(getattr(t, "name", "")): t for t in getattr(func_tool, "tools", None) or []}
        added = [name for name in now if name not in before]
        removed = [name for name in before if name not in now]
        for name in added:
            state.tools[name] = self._owner_of_tool(now[name]) or owner
        bits = []
        if added:
            bits.append("新增工具 " + "、".join(added))
        if removed:
            bits.append("移除工具 " + "、".join(removed))
        if not bits:
            bits.append("调整了工具顺序")
        state.changes.append(HookChange(owner, handler, "tools", "；".join(bits), late))

    def external_check(self, state: RequestState, req: Any) -> None:
        """在重排前确认来源表与当前请求一致；不一致说明有钩子外的改动。"""

        system = req.system_prompt or ""
        if runs_text(state.runs) != system:
            state.runs, _, _ = apply_change(
                state.runs, system, EXTERNAL_OWNER, max_chars=self.max_chars
            )
            state.changes.append(
                HookChange(EXTERNAL_OWNER, "-", "system", "在钩子之外修改了 system")
            )
        for part in list(req.extra_user_content_parts or []):
            if id(part) not in state.parts:
                self.own_part(state, part, EXTERNAL_OWNER, tail_mark(part_text(part)))

    # ---------- 切分成片段 ----------

    def raw_items(self, state: RequestState, req: Any) -> RawItems:
        """把当前请求整理成与布局无关的原始条目，供切分与样本保存共用。"""

        tail = []
        for part in list(req.extra_user_content_parts or []):
            info = state.parts.get(id(part), PartInfo(EXTERNAL_OWNER))
            media = part_is_media(part)
            text = f"[{getattr(part, 'type', None) or 'media'}]" if media else part_text(part)
            tail.append(TailItem(info.owner, info.mark, text, part_is_temp(part), media, part))
        func_tool = getattr(req, "func_tool", None)
        tools = [
            ToolItem(
                str(getattr(tool, "name", "")),
                state.tools.get(str(getattr(tool, "name", "")), BUILTIN_OWNER),
                tool,
            )
            for tool in getattr(func_tool, "tools", None) or []
        ]
        return RawItems(list(state.runs), tail, tools)


@dataclass(slots=True)
class TailItem:
    owner: str
    mark: str
    text: str
    temp: bool
    media: bool
    ref: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "mark": self.mark,
            "text": self.text,
            "temp": self.temp,
            "media": self.media,
        }


@dataclass(slots=True)
class ToolItem:
    name: str
    owner: str
    ref: Any = None


@dataclass(slots=True)
class RawItems:
    runs: list[Run]
    tail: list[TailItem]
    tools: list[ToolItem]

    def to_dict(self) -> dict[str, Any]:
        return {
            "runs": [{"owner": r.owner, "mark": r.mark, "text": r.text} for r in self.runs],
            "tail": [item.to_dict() for item in self.tail],
            "tools": [{"name": t.name, "owner": t.owner} for t in self.tools],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RawItems:
        return cls(
            runs=[Run(r["owner"], r["text"], r.get("mark", "")) for r in data.get("runs", [])],
            tail=[
                TailItem(
                    t["owner"],
                    t.get("mark", ""),
                    t["text"],
                    bool(t["temp"]),
                    bool(t["media"]),
                    dict(t),
                )
                for t in data.get("tail", [])
            ],
            tools=[ToolItem(t["name"], t["owner"], t["name"]) for t in data.get("tools", [])],
        )


def _line_blocks(runs: list[Run], rules: dict[str, list[tuple[str, bool]]]) -> list[Run]:
    """把来源片合并成只在换行处断开的块。

    插件在别人段落的行内改字（例如在人设句子里插入几个字）时，这几个字不能单独成段被挪走；
    合并后的块归属其中字数最多的来源。
    """

    pieces: list[Run] = []
    for run in runs:
        if run.owner in rules:
            texts = split_by_anchors(run.text, rules[run.owner])
            mark = run.mark if len(texts) == 1 else ""
            pieces.extend(Run(run.owner, text, mark) for text in texts)
        else:
            pieces.append(run)
    groups: list[list[Run]] = []
    for piece in pieces:
        if groups:
            prev = groups[-1][-1].text
            # 断点在行内（前一片不以换行结尾，这一片也不以换行开头）时并入上一块。
            if prev and not prev.endswith("\n") and not piece.text.startswith("\n"):
                groups[-1].append(piece)
                continue
        groups.append([piece])
    blocks: list[Run] = []
    for group in groups:
        if len(group) == 1:
            blocks.append(group[0])
            continue
        sizes: dict[tuple[str, str], int] = {}
        for piece in group:
            who = (piece.owner, piece.mark)
            sizes[who] = sizes.get(who, 0) + len(piece.text)
        owner, mark = max(sizes, key=lambda k: sizes[k])
        blocks.append(Run(owner, "".join(p.text for p in group), mark))
    return blocks


def build_segments(items: RawItems, layout: Layout) -> list[Segment]:
    """按布局的别名和细分规则把原始条目切成片段。"""

    rules: dict[str, list[tuple[str, bool]]] = {}
    for rule in layout.split_rules:
        rules.setdefault(rule.owner, []).append((rule.anchor, rule.line_start))
    result: list[Segment] = []
    seen: dict[str, int] = {}

    def add(owner: str, zone: str, text: str, key: str, **extra: Any) -> None:
        key = layout.aliases.get(key, key)
        count = seen.get(key, 0) + 1
        seen[key] = count
        if count > 1:
            key = f"{key}#{count}"
        result.append(
            Segment(key=key, owner=owner, zone=zone, text=text, order=len(result), **extra)
        )

    for block in _line_blocks(items.runs, rules):
        # 只有空白的碎片不单独成段；重排时空白会被分隔符统一替换。
        if block.text.strip():
            key = make_key(block.owner, block.mark or signature(block.text))
            add(block.owner, "system", block.text, key)
    for item in items.tail:
        if item.media:
            add(item.owner, "tail", item.text, "@media", temp=item.temp, media=True, ref=item.ref)
        else:
            key = make_key(item.owner, item.mark or signature(item.text))
            add(item.owner, "tail", item.text, key, temp=item.temp, ref=item.ref)
    for tool in items.tools:
        result.append(
            Segment(
                key=f"tool:{tool.name}",
                owner=tool.owner,
                zone="tools",
                text=tool.name,
                order=len(result),
                ref=tool.ref,
            )
        )
    return result
