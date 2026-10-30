"""排序布局及其校验。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

UNPLACED = "@unplaced"
MEDIA = "@media"
TOOL_MODES = ("keep", "by_name", "custom")
LAYOUT_ID_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
MAX_ENTRIES = 200
MAX_TEXT = 200


class LayoutError(ValueError):
    """布局数据不合法。"""


@dataclass(slots=True)
class SplitRule:
    """把某个来源的一段文本再细分的锚点规则。"""

    owner: str
    anchor: str
    line_start: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"owner": self.owner, "anchor": self.anchor, "line_start": self.line_start}


@dataclass(slots=True)
class Layout:
    """一组会话共用的片段顺序。"""

    id: str
    name: str
    umo_globs: list[str] = field(default_factory=list)
    system: list[str] = field(default_factory=lambda: [UNPLACED])
    tail: list[str] = field(default_factory=lambda: [UNPLACED, MEDIA])
    tools_mode: str = "keep"
    tools_order: list[str] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)
    split_rules: list[SplitRule] = field(default_factory=list)
    separator: str = "\n\n"
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "umo_globs": list(self.umo_globs),
            "system": list(self.system),
            "tail": list(self.tail),
            "tools": {"mode": self.tools_mode, "order": list(self.tools_order)},
            "aliases": dict(self.aliases),
            "split_rules": [rule.to_dict() for rule in self.split_rules],
            "separator": self.separator,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Layout:
        """从外部数据构造并校验布局；不合法时抛出 LayoutError。"""

        if not isinstance(data, dict):
            raise LayoutError("布局必须是对象")
        layout_id = str(data.get("id", "")).strip()
        if not LAYOUT_ID_RE.match(layout_id):
            raise LayoutError(f"布局 ID 不合法：{layout_id or '（空）'}")
        name = _text(data.get("name"), "布局名称") or layout_id
        tools = data.get("tools") or {}
        if not isinstance(tools, dict):
            raise LayoutError("tools 必须是对象")
        mode = str(tools.get("mode", "keep"))
        if mode not in TOOL_MODES:
            raise LayoutError(f"工具排序方式不支持：{mode}")
        aliases_raw = data.get("aliases") or {}
        if not isinstance(aliases_raw, dict) or len(aliases_raw) > MAX_ENTRIES:
            raise LayoutError("别名必须是不超过 200 项的对象")
        aliases = {_text(k, "别名"): _text(v, "别名") for k, v in aliases_raw.items()}
        rules_raw = data.get("split_rules") or []
        if not isinstance(rules_raw, list) or len(rules_raw) > MAX_ENTRIES:
            raise LayoutError("细分规则必须是不超过 200 项的列表")
        rules = []
        for item in rules_raw:
            if not isinstance(item, dict):
                raise LayoutError("细分规则必须是对象")
            anchor = str(item.get("anchor", ""))
            if not anchor.strip() or len(anchor) > MAX_TEXT:
                raise LayoutError("细分锚点不能为空且不超过 200 字")
            rules.append(
                SplitRule(
                    owner=_text(item.get("owner"), "细分规则来源"),
                    anchor=anchor,
                    line_start=bool(item.get("line_start", True)),
                )
            )
        separator = str(data.get("separator", "\n\n"))
        if len(separator) > 10:
            raise LayoutError("分隔符不超过 10 个字符")
        return cls(
            id=layout_id,
            name=name,
            umo_globs=_text_list(data.get("umo_globs"), "会话范围"),
            system=_entry_list(data.get("system"), [UNPLACED]),
            tail=_entry_list(data.get("tail"), [UNPLACED, MEDIA]),
            tools_mode=mode,
            tools_order=_text_list(tools.get("order"), "工具顺序"),
            aliases=aliases,
            split_rules=rules,
            separator=separator,
            enabled=bool(data.get("enabled", True)),
        )


def _text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if len(text) > MAX_TEXT:
        raise LayoutError(f"{label}不超过 200 字")
    return text


def _text_list(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_ENTRIES:
        raise LayoutError(f"{label}必须是不超过 200 项的列表")
    items = [_text(item, label) for item in value]
    return [item for item in items if item]


def _entry_list(value: Any, default: list[str]) -> list[str]:
    if value is None:
        return list(default)
    entries = _text_list(value, "片段顺序")
    if len(set(entries)) != len(entries):
        raise LayoutError("同一个区里的片段不能重复")
    return entries


def default_layout() -> Layout:
    """未匹配任何布局时使用的兜底布局：保持原顺序。"""

    return Layout(id="default", name="默认", umo_globs=[])
